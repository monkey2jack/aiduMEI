"""f0.3++ scope identity regressions.

The target hall is an argument; the caller is deployment identity.  These
tests keep the two from becoming interchangeable again.
"""
from __future__ import annotations

import inspect
import json

import pytest


def test_mcp_transport_uses_configured_principal_for_another_target(monkeypatch):
    import mcp_server as server

    seen = {}

    class Response:
        status_code = 200
        text = "{}"

        def json(self):
            return {"status": "ok"}

    def post(url, *, params=None, json=None, headers=None, timeout=None):
        seen.update(json or {})
        return Response()

    monkeypatch.setenv("AIDUMEM_USER_ID", "configured-agent")
    monkeypatch.setattr(server.httpx, "post", post)
    server._api_post("/search", {"query": "q", "user_id": "target-hall"})

    assert seen["user_id"] == "target-hall"
    assert seen["caller_user_id"] == "configured-agent"


def test_configured_token_binding_rejects_self_reported_target(monkeypatch):
    from fastapi import HTTPException
    from ducky.security.auth import enforce_caller_binding, fingerprint_token, set_request_token_fingerprint

    token = "principal-bound-token"
    monkeypatch.setenv(
        "AIDUMEI_CALLER_BINDINGS",
        json.dumps({fingerprint_token(token): ["configured-agent"]}),
    )
    monkeypatch.setenv("AIDUMEI_CALLER_BINDING_MODE", "strict")
    set_request_token_fingerprint(token)
    try:
        try:
            enforce_caller_binding("target-hall", "scope:read")
        except HTTPException as exc:
            assert exc.status_code == 403
        else:
            raise AssertionError("a bearer caller must not self-authorize as the target hall")
    finally:
        from ducky.security.auth import clear_request_token_fingerprint
        clear_request_token_fingerprint()


@pytest.fixture
def bound_client(monkeypatch):
    import api_server
    from fastapi.testclient import TestClient
    from ducky.security.auth import fingerprint_token
    token = "scope-regression-token"
    monkeypatch.setenv("AIDUMEM_API_TOKEN", token)
    monkeypatch.setenv("AIDUMEI_CALLER_BINDINGS", json.dumps({fingerprint_token(token): ["agent-a"]}))
    monkeypatch.setenv("AIDUMEI_CALLER_BINDING_MODE", "strict")
    monkeypatch.setenv("AIDUMEI_FEDERATION_ADMINS", "admin")
    return TestClient(api_server.app), {"Authorization": "Bearer " + token}


@pytest.mark.parametrize("method,path,placement,payload", [
    ("GET", "/recent", "params", {}),
    ("GET", "/stats", "params", {}),
    ("GET", "/facts", "params", {}),
    ("GET", "/facts/categories", "params", {}),
    ("GET", "/memory/types", "params", {}),
    ("GET", "/reflect/list", "params", {}),
    ("GET", "/session/list", "params", {}),
    ("POST", "/session/start", "params", {}),
    ("GET", "/api/core-memory", "params", {}),
    ("POST", "/add/raw", "json", {"content": "synthetic"}),
    ("POST", "/add", "json", {"messages": [{"role": "user", "content": "synthetic"}], "infer": False}),
    ("POST", "/update", "json", {"memory_id": "nonexistent", "content": "synthetic"}),
    ("POST", "/delete", "json", {"memory_id": "nonexistent"}),
    ("POST", "/delete_all", "json", {"confirm": True}),
    ("POST", "/persona/ai-self/add", "params", {"category": "identity", "key": "x", "value": "synthetic"}),
    ("POST", "/memory/types/reset", "json", {}),
])
def test_token_a_cannot_claim_to_be_target_b(bound_client, method, path, placement, payload):
    client, headers = bound_client
    fields = {**payload, "user_id": "agent-b", "bank_id": "private", "caller_user_id": "agent-b"}
    response = client.request(method, path, headers=headers, **{placement: fields})
    assert response.status_code == 403, (path, response.status_code, response.text[:200])


def test_full_governance_cannot_be_enabled_by_forged_admin(bound_client):
    client, headers = bound_client
    response = client.get("/governance/candidates", params={"caller_user_id": "admin"}, headers=headers)
    assert response.status_code == 403


def test_bound_identity_resolves_an_omitted_caller_and_keeps_own_hall(bound_client):
    client, headers = bound_client
    response = client.get("/api/core-memory", params={"user_id": "agent-a", "bank_id": "synthetic"}, headers=headers)
    assert response.status_code == 200
    assert response.json()["user_id"] == "agent-a"


def test_registered_scope_census_requires_the_shared_wrapper():
    import api_server
    from fastapi.routing import APIRoute
    from ducky.scope_auth import scoped_model_type
    violations = []
    count = 0
    for app in (api_server.app, api_server._api_alias):
        for route in app.routes:
            if not isinstance(route, APIRoute):
                continue
            sig = inspect.signature(route.endpoint, eval_str=True)
            scoped = "user_id" in sig.parameters or "owner" in sig.parameters or any(
                scoped_model_type(p.annotation) is not None for p in sig.parameters.values()
            )
            if scoped:
                count += 1
                if not getattr(route.endpoint, "__aidumei_scoped__", False):
                    violations.append(route.path)
    assert count >= 100
    assert not violations


@pytest.mark.parametrize("method,path,params", [
    ("POST", "/pantheon/grant", {"grantor_user_id": "agent-b", "grantee_user_id": "agent-a", "caller": "agent-b"}),
    ("POST", "/pantheon/grant/nonexistent/revoke", {"caller": "admin"}),
    ("GET", "/pantheon/grants", {"user_id": "agent-b", "caller": "admin"}),
    ("GET", "/pantheon/halls", {"caller": "admin"}),
])
def test_pantheon_management_cannot_self_report_owner_or_admin(bound_client, method, path, params):
    client, headers = bound_client
    response = client.request(method, path, headers=headers, params=params)
    assert response.status_code == 403, response.text
