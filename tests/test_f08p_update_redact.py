"""f0.8+ plan ⑦: /update redacts P7 spans in place (and says so) instead of only refusing."""
from __future__ import annotations

import uuid


def _client(monkeypatch):
    import api_server
    from fastapi.testclient import TestClient
    monkeypatch.setenv("AIDUMEM_API_TOKEN", "upd-f08p-token")
    monkeypatch.setenv("AIDUMEI_ENGINE_MODE", "local")
    return TestClient(api_server.app, headers={"Authorization": "Bearer upd-f08p-token"})


def _hit(c, u, q):
    r = c.post("/search", json={"query": q, "user_id": u, "caller_user_id": u})
    assert r.status_code == 200, r.text
    return r.json()["results"][0]


def test_update_redacts_in_place_and_reports_it(monkeypatch):
    c = _client(monkeypatch)
    u = f"updr-{uuid.uuid4().hex[:8]}"
    r = c.post("/add", json={"messages": [{"role": "user", "content": "synthetic notes about the garage router"}],
                             "user_id": u, "caller_user_id": u, "infer": False})
    assert r.status_code == 200, r.text
    mid = _hit(c, u, "garage router")["id"]
    r = c.post("/update", json={"memory_id": mid, "user_id": u, "caller_user_id": u,
                                "content": "garage router admin password: Hunter2024!x, reboot weekly"})
    assert r.status_code == 200, r.text
    assert r.json()["redacted"] == ["credential"]
    hit = _hit(c, u, "garage router reboot")
    assert "Hunter2024" not in str(hit)
    assert "[redacted:credential]" in hit["memory"] and "reboot weekly" in hit["memory"]
    assert hit["metadata"].get("redacted") == ["credential"]


def test_update_plain_text_has_no_redaction_note(monkeypatch):
    c = _client(monkeypatch)
    u = f"updr-{uuid.uuid4().hex[:8]}"
    c.post("/add", json={"messages": [{"role": "user", "content": "synthetic plan to paint the fence"}],
                         "user_id": u, "caller_user_id": u, "infer": False})
    mid = _hit(c, u, "paint fence")["id"]
    r = c.post("/update", json={"memory_id": mid, "user_id": u, "caller_user_id": u,
                                "content": "synthetic plan to paint the fence green"})
    assert r.status_code == 200 and "redacted" not in r.json()


def test_update_secret_only_is_still_refused(monkeypatch):
    c = _client(monkeypatch)
    u = f"updr-{uuid.uuid4().hex[:8]}"
    c.post("/add", json={"messages": [{"role": "user", "content": "synthetic note about the shed lock"}],
                         "user_id": u, "caller_user_id": u, "infer": False})
    mid = _hit(c, u, "shed lock")["id"]
    r = c.post("/update", json={"memory_id": mid, "user_id": u, "caller_user_id": u,
                                "content": "sk-ant-api03-Zq8vT3mK9pL2xW7nR4bY6cJ1abcdefgh"})
    assert r.status_code == 422
    assert "shed lock" in _hit(c, u, "shed lock")["memory"]
