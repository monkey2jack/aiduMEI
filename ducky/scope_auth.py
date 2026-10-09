"""Request scope authorization shared by tenant aware HTTP routes.

The API still accepts the historical ``user_id`` and ``bank_id`` fields, but
every route that reads or mutates scoped data must pass the caller through the
same Pantheon policy point.  A caller may access its own hall; cross hall
access requires an explicit grant.  Writes use ``action='write'`` and
therefore cannot be enabled by a read grant.
"""
from __future__ import annotations

from functools import wraps
import inspect
from typing import get_args

from fastapi import HTTPException


def scoped_model_type(annotation):
    """Recognize a scoped model, including Optional/Union body contracts."""
    for kind in (annotation, *get_args(annotation)):
        if {"user_id", "owner"} & getattr(kind, "model_fields", {}).keys():
            return kind
    return None


def model_type(annotation):
    return next((kind for kind in (annotation, *get_args(annotation))
                 if hasattr(kind, "model_fields")), None)


class ScopeRegistrar:
    """Attach explicit resource policy to every registered business route.

    Existing route guards remain active.  This wrapper closes older secondary
    routes in deployments that configure credential bindings; the unbound
    loopback/single-owner mode retains its existing API contract.
    """

    def __init__(self, app):
        self._app = app

    def __getattr__(self, name):
        return getattr(self._app, name)

    def _register(self, method, path, kwargs):
        def decorate(fn):
            signature = inspect.signature(fn, eval_str=True)
            models = {
                name: model_type(param.annotation)
                for name, param in signature.parameters.items()
                if model_type(param.annotation) is not None
            }
            from ducky.route_policy import POLICIES
            policy = POLICIES.get((method, path))
            if policy is None:
                raise RuntimeError(f"Unclassified HTTP resource: {method} {path}")
            parameters = list(signature.parameters.values())
            caller_key = next((key for key in ("caller_user_id", "caller_agent_id", "caller")
                               if key in signature.parameters), "caller_user_id")
            added_caller = caller_key not in signature.parameters
            if added_caller and policy.resource != "public":
                parameters.append(inspect.Parameter(caller_key, inspect.Parameter.KEYWORD_ONLY,
                                                    default="", annotation=str))
            wire_signature = signature.replace(parameters=parameters)

            def prepare(args, values):
                bound = wire_signature.bind(*args, **values)
                bound.apply_defaults()
                call = dict(bound.arguments)
                from ducky.security.auth import binding_policy_active, resolve_bound_caller
                if policy.resource != "public" and binding_policy_active():
                    model_key = next(iter(models), None)
                    model = call.get(model_key) if model_key else None
                    if model_key and model is None:
                        model = models[model_key]()
                        call[model_key] = model
                    # /facts/inject-context has a historical untyped JSON body.
                    if path == "/facts/inject-context":
                        model_key, model = "req", call["req"]
                    data = model if isinstance(model, dict) else (
                        model.model_dump() if model is not None else call)
                    caller = resolve_bound_caller(data.get("caller_user_id", "") or call.get(caller_key, ""),
                                                  f"route:{method}:{path}")
                    normalized = authorize_route_resource(policy, data, caller)
                    updates = {"caller_user_id": caller, **normalized}
                    if isinstance(model, dict):
                        call[model_key] = {**model, **updates}
                    elif model is not None:
                        call[model_key] = model.model_copy(update={
                            k: v for k, v in updates.items() if k in type(model).model_fields})
                    else:
                        call.update({k: v for k, v in normalized.items() if k in signature.parameters})
                    if not added_caller:
                        call[caller_key] = caller
                if added_caller:
                    call.pop(caller_key, None)
                return call

            if inspect.iscoroutinefunction(fn):
                @wraps(fn)
                async def guarded(*args, **values):
                    return await fn(**prepare(args, values))
            else:
                @wraps(fn)
                def guarded(*args, **values):
                    return fn(**prepare(args, values))

            guarded.__signature__ = wire_signature
            guarded.__aidumei_scoped__ = True
            return getattr(self._app, method)(path, **kwargs)(guarded)
        return decorate

    def get(self, path, **kwargs):
        return self._register("get", path, kwargs)

    def post(self, path, **kwargs):
        return self._register("post", path, kwargs)

    def put(self, path, **kwargs):
        return self._register("put", path, kwargs)

    def delete(self, path, **kwargs):
        return self._register("delete", path, kwargs)

    def patch(self, path, **kwargs):
        return self._register("patch", path, kwargs)


def authorize_route_resource(policy, data, caller):
    """Authorize the declared resource; never infer permission from the verb."""
    from ducky.bank_contract import make_scope
    from ducky.utils import DEFAULT_USER_ID
    if policy.resource == "manual":
        return {}  # handler loads and checks the canonical resource
    if policy.resource == "admin":
        require_instance_admin(caller)
        return {}
    target = data.get("user_id", data.get("owner", DEFAULT_USER_ID))
    bank = data.get("bank_id", "default")
    if policy.resource == "optional_scope" and (not target or not bank):
        require_instance_admin(caller)
        return {}
    if policy.resource == "default_owner":
        target = DEFAULT_USER_ID
    scope = make_scope(target, bank)
    require_scope_access(scope.user_id, caller,
                         bank_id="*" if policy.resource == "owner" else scope.bank_id,
                         action=policy.action)
    # Authorization and SQL must see the same normalized scope. Several legacy
    # functions interpret empty strings as ALL, so checking only a default scope
    # without forwarding it would still leave a bypass.
    return {"user_id" if "user_id" in data or "owner" not in data else "owner": scope.user_id,
            "bank_id": scope.bank_id}


def require_instance_admin(caller):
    """Owner sessions stay administrative; bearer admins must be bound."""
    import os
    from ducky.security.auth import current_request_auth_kind, enforce_caller_binding
    if current_request_auth_kind() in ("", "session"):
        return
    enforce_caller_binding(caller, "instance:admin")
    admins = {x.strip() for x in os.environ.get("AIDUMEI_FEDERATION_ADMINS", "").split(",") if x.strip()}
    if caller not in admins:
        raise HTTPException(403, "instance-wide operation requires admin")


def sanitize_memory_or_raise(content: str) -> str:
    """Apply the shared injection policy and turn rejection into HTTP 400.

    Keeping this small policy adapter outside route registration functions keeps
    the route assembly code readable and prevents the same validation branch
    from inflating its complexity score in every endpoint.
    """
    from ducky.security.injection_guard import validate_and_sanitize_memory_content

    is_safe, sanitized, rejection = validate_and_sanitize_memory_content(content)
    if not is_safe:
        raise HTTPException(status_code=400, detail=f"Memory content rejected: {rejection}")
    return sanitized


def sanitize_memory_fields(*values: str) -> tuple[str, ...]:
    """One shared policy for every persisted text field in a memory record."""
    return tuple(sanitize_memory_or_raise(value) if value else value for value in values)


def sanitize_memory_structure(value, depth=0):
    """Validate metadata text without stringifying away its JSON structure."""
    if depth > 8:
        raise HTTPException(400, "memory metadata nesting exceeds limit")
    if isinstance(value, str):
        return sanitize_memory_or_raise(value) if value else value
    if isinstance(value, dict):
        return {sanitize_memory_structure(k, depth + 1): sanitize_memory_structure(v, depth + 1)
                for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_memory_structure(v, depth + 1) for v in value]
    return value


def authorize_governance_view(
    user_id: str,
    scope_user_id: str,
    bank_id: str,
    caller_user_id: str,
) -> tuple[str, str]:
    """Authorize a scoped or full governance-candidate view.

    A full-instance view remains an administrative operation.  Direct route
    unit tests and loopback no-auth development retain the historical owner
    semantics; authenticated bearer callers must name an admin explicitly.
    """
    target = (scope_user_id or user_id or "").strip()
    if target:
        bank = bank_id or "default"
        require_scope_access(target, caller_user_id, bank_id=bank, action="read")
        # user_id is the writer filter; it must never stand in for SQL owner scope.
        return target, bank
    require_instance_admin(caller_user_id)
    return "", bank_id


def require_scope_access(
    user_id: str,
    caller_user_id: str = "",
    *,
    bank_id: str = "default",
    action: str = "read",
) -> None:
    """Raise HTTP 403 unless the caller may use the requested scope."""
    from ducky.pantheon import HallError, authorize_cross_hall

    try:
        authorize_cross_hall(
            user_id,
            caller_user_id,
            bank_id=bank_id,
            action=action,
        )
    except HallError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
