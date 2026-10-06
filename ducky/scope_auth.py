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


def scoped_model_type(annotation):
    """Recognize a scoped model, including Optional/Union body contracts."""
    for kind in (annotation, *get_args(annotation)):
        if "user_id" in getattr(kind, "model_fields", {}):
            return kind
    return None

from fastapi import HTTPException


class ScopeRegistrar:
    """Attach the common policy to every route declaring a memory user scope.

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
                name: scoped_model_type(param.annotation)
                for name, param in signature.parameters.items()
                if scoped_model_type(param.annotation) is not None
            }
            scoped = bool(models) or "user_id" in signature.parameters or "owner" in signature.parameters
            if not scoped:
                return getattr(self._app, method)(path, **kwargs)(fn)
            parameters = list(signature.parameters.values())
            added_caller = "caller_user_id" not in signature.parameters and "caller" not in signature.parameters
            if added_caller:
                parameters.append(inspect.Parameter("caller_user_id", inspect.Parameter.KEYWORD_ONLY, default="", annotation=str))
            wire_signature = signature.replace(parameters=parameters)

            def prepare(args, values):
                bound = wire_signature.bind(*args, **values)
                bound.apply_defaults()
                call = dict(bound.arguments)
                from ducky.security.auth import _caller_bindings, current_request_token_fingerprint, enforce_caller_binding
                table = _caller_bindings()
                fp = current_request_token_fingerprint()
                if table is not None and fp:
                    model_key = next(iter(models), None)
                    model = call.get(model_key) if model_key else None
                    if model_key and model is None:
                        model = models[model_key]()
                        call[model_key] = model
                    target = getattr(model, "user_id", None) if model is not None else call.get("user_id", call.get("owner", ""))
                    bank = getattr(model, "bank_id", "default") if model is not None else call.get("bank_id", "default")
                    caller = (getattr(model, "caller_user_id", "") if model is not None else "") or call.get("caller_user_id", call.get("caller", ""))
                    allowed = table.get(fp, [])
                    if not caller and isinstance(allowed, list) and len(allowed) == 1:
                        caller = str(allowed[0])
                    enforce_caller_binding(caller, f"route:{method}:{path}")
                    if target:
                        require_scope_access(target, caller, bank_id=bank or "default", action="read" if method == "get" else "write")
                    else:
                        authorize_governance_view("", "", "", caller)
                    if model is not None and "caller_user_id" in type(model).model_fields:
                        call[model_key] = model.model_copy(update={"caller_user_id": caller})
                    if "caller_user_id" in signature.parameters:
                        call["caller_user_id"] = caller
                    elif "caller" in signature.parameters:
                        call["caller"] = caller
                if added_caller:
                    call.pop("caller_user_id", None)
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


def authorize_governance_view(
    user_id: str,
    scope_user_id: str,
    bank_id: str,
    caller_user_id: str,
) -> None:
    """Authorize a scoped or full governance-candidate view.

    A full-instance view remains an administrative operation.  Direct route
    unit tests and loopback no-auth development retain the historical owner
    semantics; authenticated bearer callers must name an admin explicitly.
    """
    from ducky.security.auth import current_request_auth_kind

    target = (scope_user_id or user_id or "").strip()
    if target:
        require_scope_access(
            target,
            caller_user_id,
            bank_id=(bank_id or "default"),
            action="read",
        )
        return
    if current_request_auth_kind() in ("", "session"):
        return
    import os
    admins = {
        item.strip()
        for item in os.environ.get("AIDUMEI_FEDERATION_ADMINS", "").split(",")
        if item.strip()
    }
    from ducky.security.auth import enforce_caller_binding
    enforce_caller_binding(caller_user_id, "governance:all")
    if caller_user_id not in admins:
        raise HTTPException(status_code=403, detail="governance 全量候选视图仅 admin 可访问")


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
