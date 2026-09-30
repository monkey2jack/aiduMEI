"""Clotho must apply Pantheon policy to every scope-bearing endpoint."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_clotho_cross_hall_read_grants_and_write_denials(monkeypatch):
    import ducky.routes_clotho as clotho
    import ducky.pantheon as pantheon
    import ducky.security.auth as auth

    monkeypatch.setattr(auth, "current_request_auth_kind", lambda: "bearer")
    grants = set()
    monkeypatch.setattr(pantheon, "check_hall_access",
                        lambda target, caller, action="read", bank_id=None:
                        (target, caller, action, bank_id) in grants)
    monkeypatch.setattr(clotho, "get_all_blocks", lambda user_id, bank_id: {"owner": f"{user_id}/{bank_id}"})
    monkeypatch.setattr(clotho, "get_block", lambda block_key, user_id, bank_id: {"owner": f"{user_id}/{bank_id}"})
    monkeypatch.setattr(clotho, "core_memory_context", lambda user_id, bank_id: f"core:{user_id}/{bank_id}")
    monkeypatch.setattr(clotho, "put_block", lambda block_key, content, user_id, bank_id, **kwargs: {"owner": f"{user_id}/{bank_id}"})
    monkeypatch.setattr(clotho, "get_latest_checkpoint", lambda user_id, bank_id: {"owner": f"{user_id}/{bank_id}"})
    monkeypatch.setattr(clotho, "get_checkpoint", lambda sid, user_id, bank_id:
                        {"owner": f"{user_id}/{bank_id}"})
    monkeypatch.setattr(clotho, "checkpoint_context", lambda user_id, bank_id: f"checkpoint:{user_id}/{bank_id}")
    monkeypatch.setattr(clotho, "write_checkpoint", lambda sid, blocks, user_id, bank_id:
                        {"owner": f"{user_id}/{bank_id}"})
    monkeypatch.setattr(clotho, "cleanup_old_checkpoints", lambda user_id, bank_id: f"cleaned:{user_id}/{bank_id}")

    app = FastAPI()
    clotho.register_clotho_routes(app)
    client = TestClient(app)
    scope = {"user_id": "victim", "bank_id": "private", "caller_user_id": "attacker"}
    reads = (
        lambda: client.get("/api/core-memory", params=scope),
        lambda: client.get("/api/core-memory/core_user_profile", params=scope),
        lambda: client.post("/api/core-memory/inject", params=scope),
        lambda: client.get("/api/checkpoint/latest", params=scope),
        lambda: client.get("/api/checkpoint/session-1", params=scope),
        lambda: client.post("/api/checkpoint/inject", params=scope),
    )
    writes = (
        lambda: client.put("/api/core-memory/core_user_profile", params=scope,
                           json={"content": "intrusion"}),
        lambda: client.post("/api/checkpoint", json={"session_id": "session-1", "blocks": {}, **scope}),
        lambda: client.delete("/api/checkpoint/cleanup", params=scope),
    )
    assert all(call().status_code == 403 for call in (*reads, *writes))
    grants.add(("victim", "attacker", "read", "private"))
    for call in reads:
        r = call()
        assert r.status_code == 200, r.text
        assert "victim/private" in r.text
    assert all(call().status_code == 403 for call in writes)
    own = client.post("/api/core-memory/inject", params={"user_id": "victim",
                                                          "bank_id": "private",
                                                          "caller_user_id": "victim"})
    assert own.status_code == 200 and "core:victim/private" in own.text
    no_caller = client.post("/api/core-memory/inject", params={"user_id": "victim", "bank_id": "private"})
    assert no_caller.status_code == 403
