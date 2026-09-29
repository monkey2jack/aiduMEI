"""f0.2 proactive audit: workspace finalization and batch scope."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _search_client(monkeypatch, workspace_hits, hybrid_hits):
    import ducky.hot.search as hot_search
    import ducky.memory_workspace as workspace
    import ducky.evolve_mem as evolve

    logs = []
    monkeypatch.setattr(hot_search, "get_memory", lambda: object())
    monkeypatch.setattr(hot_search, "ensure_bank_registered", lambda *a, **kw: None)
    monkeypatch.setattr(hot_search, "boost_salience_for_results", lambda *a, **kw: None)
    monkeypatch.setattr(hot_search, "_annotate_memory_types", lambda *a, **kw: None)
    monkeypatch.setattr(hot_search, "lazy_import_hybrid", lambda: (
        lambda *a, **kw: list(hybrid_hits)))
    monkeypatch.setattr(workspace, "ws_lookup", lambda *a, **kw: list(workspace_hits))
    monkeypatch.setattr(workspace, "ws_feed_from_results", lambda *a, **kw: None)
    monkeypatch.setattr(evolve, "log_search_quality", lambda *a, **kw: logs.append((a, kw)))
    app = FastAPI()
    hot_search.register_search_routes(app)
    return TestClient(app), logs


def test_workspace_obeys_time_window_and_falls_through(monkeypatch):
    client, logs = _search_client(
        monkeypatch,
        [{"id": "old", "memory": "旧记忆", "score": 0.95,
          "created_at": "2020-01-01T00:00:00Z"}],
        [{"id": "new", "memory": "新记忆", "score": 0.9,
          "created_at": "2026-01-01T00:00:00Z"}],
    )
    body = client.post("/search", json={"query": "记忆", "user_id": "u",
                                        "after": "2025-01-01", "session_id": "s"}).json()
    assert body["status"] == "ok", body
    assert [r["id"] for r in body["results"]] == ["new"]
    assert body["_recall_path"] == "hybrid"
    assert len(logs) == 1 and logs[0][1]["origin_session_id"] == "s"


def test_workspace_obeys_floor_limit_verdict_and_logs(monkeypatch):
    monkeypatch.setenv("AIDUMEM_RECALL_SCORE_FLOOR", "0.8")
    client, logs = _search_client(
        monkeypatch,
        [{"id": "weak", "memory": "弱相关", "score": 0.6}],
        [],
    )
    body = client.post("/search", json={"query": "弱", "user_id": "u",
                                        "session_id": "s"}).json()
    assert body["results"] == []
    assert body["recall_verdict"] == "not_found"
    assert len(logs) == 1 and logs[0][1]["origin_session_id"] == "s"

    client2, logs2 = _search_client(
        monkeypatch,
        [{"id": f"w{i}", "memory": "热记忆", "score": 0.9 - i * 0.001}
         for i in range(8)],
        [],
    )
    body2 = client2.post("/search", json={"query": "热", "user_id": "u",
                                          "top_k": 2, "session_id": "s"}).json()
    assert [r["id"] for r in body2["results"]] == ["w0", "w1"]
    assert body2["recall_verdict"] == "found"
    assert body2["verdict_basis"] == "workspace_hit"
    assert len(logs2) == 1 and len(logs2[0][0][1]) == 2
