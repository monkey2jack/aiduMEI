"""Clotho must apply Pantheon policy to every scope-bearing endpoint.

f0.6+++ WP2: only GET /api/core-memory survives the route slimming (CC hook
inject); checkpoint / inject / block write endpoints were removed.
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_clotho_cross_hall_read_grants(monkeypatch):
    import ducky.routes_clotho as clotho
    import ducky.pantheon as pantheon
    import ducky.security.auth as auth

    monkeypatch.setattr(auth, "current_request_auth_kind", lambda: "bearer")
    grants = set()
    monkeypatch.setattr(pantheon, "check_hall_access",
                        lambda target, caller, action="read", bank_id=None:
                        (target, caller, action, bank_id) in grants)
    monkeypatch.setattr(clotho, "get_all_blocks", lambda user_id, bank_id: {"owner": f"{user_id}/{bank_id}"})

    app = FastAPI()
    clotho.register_clotho_routes(app)
    client = TestClient(app)
    scope = {"user_id": "victim", "bank_id": "private", "caller_user_id": "attacker"}
    assert client.get("/api/core-memory", params=scope).status_code == 403
    grants.add(("victim", "attacker", "read", "private"))
    r = client.get("/api/core-memory", params=scope)
    assert r.status_code == 200, r.text
    assert "victim/private" in r.text
    # a grant on another bank does not leak this one
    other = client.get("/api/core-memory", params={**scope, "bank_id": "work"})
    assert other.status_code == 403
    own = client.get("/api/core-memory", params={"user_id": "victim", "bank_id": "private",
                                                 "caller_user_id": "victim"})
    assert own.status_code == 200 and "victim/private" in own.text
    no_caller = client.get("/api/core-memory", params={"user_id": "victim", "bank_id": "private"})
    assert no_caller.status_code == 403
