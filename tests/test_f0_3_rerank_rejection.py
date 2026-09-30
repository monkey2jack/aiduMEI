"""Unknown-answer and supplemental-original regressions with fresh evidence."""
import pytest

from ducky import scoring
from ducky.recall_evidence import filter_rerank_relevance, valid_rerank_scores
from ducky.search_candidates import search_candidates
from ducky.verbatim_relevance import rank_originals


@pytest.fixture(autouse=True)
def relevance_defaults(monkeypatch):
    monkeypatch.delenv("AIDUMEI_RERANK_MIN_RELEVANCE", raising=False)


def test_low_relevance_is_removed_before_truncation_and_ignition(monkeypatch):
    import ducky.mem0_runtime as runtime
    monkeypatch.setattr(runtime, "rerank", lambda *a, **k: [
        {"index": 0, "relevance_score": 0.0057},
        {"index": 1, "relevance_score": 0.9}])
    rows = [{"id": "noise", "score": 0.8, "_hybrid_score": 0.9, "_ignited": True},
            {"id": "answer", "score": 0.5, "_hybrid_score": 0.5}]
    assert scoring._apply_rerank("known fact", rows, 1)
    assert [row["id"] for row in rows] == ["answer"]
    assert scoring.last_gate_telemetry()["rerank_relevance"]["dropped"] == 1


@pytest.mark.parametrize("value", [None, True, "0.0", float("nan"), float("inf"), -0.1, 1.1])
def test_missing_invalid_scores_remain_unknown(value):
    scores = valid_rerank_scores([{"index": 0, "relevance_score": value}], 1)
    assert scores == {}
    kept, info = filter_rerank_relevance([{"id": "unjudged"}], scores)
    assert len(kept) == 1 and info["unscored"] == 1 and info["dropped"] == 0


def test_duplicate_indices_cannot_choose_the_last_score():
    assert valid_rerank_scores([
        {"index": 0, "relevance_score": 0.01}, {"index": 0, "relevance_score": 0.99},
        {"index": 0, "relevance_score": 0.7}, {"index": True, "relevance_score": 0.9},
        {"index": 4, "relevance_score": 0.9}, None], 1) == {}


def test_threshold_boundary_and_explicit_disable(monkeypatch):
    rows = [{"id": "below"}, {"id": "boundary"}]
    assert filter_rerank_relevance(rows, {0: 0.0999, 1: 0.1})[0] == rows[1:]
    monkeypatch.setenv("AIDUMEI_RERANK_MIN_RELEVANCE", "0")
    assert filter_rerank_relevance(rows, {0: 0, 1: 0.1})[0] == rows


def test_partial_response_preserves_unjudged_candidates(monkeypatch):
    import ducky.mem0_runtime as runtime
    monkeypatch.setattr(runtime, "rerank", lambda *a, **k: [{"index": 0, "relevance_score": 0.01}])
    rows = [{"id": "reject", "_hybrid_score": 0.8},
            {"id": "unknown", "_hybrid_score": 0.6, "_rerank_score": 0.99,
             "_rerank_original_verified": True}]
    scoring._apply_rerank("query", rows, 1)
    assert rows == [{"id": "unknown", "_hybrid_score": 0.6}]
    assert scoring.last_gate_telemetry()["rerank_relevance"]["unscored"] == 1


def test_originals_are_scored_before_quota_and_primary_telemetry_survives(monkeypatch):
    import ducky.mem0_runtime as runtime
    primary = {"status": "ok", "applied": True, "latency_ms": 100}
    runtime.restore_rerank_telemetry(primary)
    calls = []
    def rerank(query, docs, top_n):
        calls.append((query, docs, top_n))
        runtime.restore_rerank_telemetry({"status": "ok", "latency_ms": 50})
        return [{"index": 0, "relevance_score": 0.0057},
                {"index": 1, "relevance_score": 0.92}]
    monkeypatch.setattr(runtime, "rerank", rerank)
    hits = [{"id": "noise", "memory": "unrelated diary"},
            {"id": "quote", "memory": "the requested original wording"}]
    kept = rank_originals("original wording", hits)
    assert len(kept) == 1 and kept[0]["id"] == "quote"
    assert kept[0]["_rerank_original_verified"] is True
    assert calls[0][2] == 2 and "_rerank_score" not in hits[1]
    assert runtime.last_rerank_telemetry() is primary
    assert primary["latency_ms"] == 100 and primary["verbatim"]["latency_ms"] == 50
    assert primary["verbatim"]["relevance"]["dropped"] == 1


@pytest.mark.parametrize("status", ["error", "disabled", "not_configured", "blocked_by_engine_mode"])
def test_originals_fallback_does_not_reuse_old_evidence(monkeypatch, status):
    import ducky.mem0_runtime as runtime
    def rerank(*args, **kwargs):
        runtime.restore_rerank_telemetry({"status": status})
        return []
    monkeypatch.setattr(runtime, "rerank", rerank)
    rows = rank_originals("different query", [{"memory": "quote", "_rerank_score": 0.99,
                                               "_rerank_original_verified": True}])
    assert rows == [{"memory": "quote"}]


def test_original_only_verdict_requires_fresh_verification():
    from ducky.hot.search import compute_recall_verdict
    assert compute_recall_verdict([{}], None, 0.46, verified_original=True) == ("found", "rerank_verified_original")
    assert compute_recall_verdict([], 0.8, 0.46, verified_original=True)[0] == "not_found"
    assert compute_recall_verdict([], None, 0.46, vector_leg_failed=True, verified_original=True)[0] == "degraded"


def test_current_sdk_does_not_silently_ignore_candidate_budget():
    class CurrentSDK:
        def search(self, query, filters, top_k=20, **kwargs):
            return {"count": top_k, "ignored": kwargs, "scope": filters}
    scope = {"user_id": "alice", "bank_id": "work"}
    assert search_candidates(CurrentSDK(), "query", filters=scope, limit=60) == {
        "count": 60, "ignored": {}, "scope": scope}


def test_legacy_sdk_budget_is_supported_without_retrying_operational_errors():
    class LegacySDK:
        def search(self, query, filters, limit=20):
            return limit
    assert search_candidates(LegacySDK(), "query", filters={}, limit=60) == 60
    class BrokenSDK:
        calls = 0
        def search(self, query, filters, top_k=20, **kwargs):
            self.calls += 1
            raise TypeError("internal service failure")
    memory = BrokenSDK()
    with pytest.raises(TypeError, match="internal service failure"):
        search_candidates(memory, "query", filters={}, limit=30)
    assert memory.calls == 1


@pytest.mark.parametrize("original_score,expected", [(0.0057, "not_found"), (0.95, "found")])
def test_http_response_and_workspace_share_final_relevance_gate(monkeypatch, original_score, expected):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ducky.hot import search as route
    from ducky import mem0_runtime as runtime, memory_workspace as workspace, verbatim_vault as vault
    monkeypatch.setenv("AIDUMEM_RECALL_SCORE_FLOOR", "0.46")
    monkeypatch.setenv("AIDUMEI_RECALL_VERDICT_THRESHOLD", "0.46")
    monkeypatch.setattr(route, "get_memory", lambda: object())
    monkeypatch.setattr(route, "ensure_bank_registered", lambda *a, **k: None)
    monkeypatch.setattr(route, "boost_salience_for_results", lambda *a, **k: None)
    monkeypatch.setattr(route, "_annotate_memory_types", lambda *a, **k: None)
    monkeypatch.setattr(workspace, "ws_lookup", lambda *a, **k: [])
    fed = []
    monkeypatch.setattr(workspace, "ws_feed_from_results", lambda uid, rows, **k: fed.extend(rows))
    def rerank(query, docs, top_n):
        runtime.restore_rerank_telemetry({"status": "ok", "latency_ms": 1})
        return [{"index": i, "relevance_score": original_score if doc == "quote" else 0.0057}
                for i, doc in enumerate(docs)]
    monkeypatch.setattr(runtime, "rerank", rerank)
    def hybrid(*args, **kwargs):
        rows = [{"id": "noise", "memory": "unrelated", "score": 0.6, "_hybrid_score": 0.6}]
        scoring._apply_rerank("query", rows, 3)
        return rows
    monkeypatch.setattr(route, "lazy_import_hybrid", lambda: hybrid)
    monkeypatch.setattr(vault, "verbatim_search", lambda *a, **k: [{"id": "verbatim:1", "memory": "quote"}])
    app = FastAPI()
    route.register_search_routes(app)
    body = TestClient(app).post("/search", json={"query": "quote", "user_id": "alice"}).json()
    assert body["status"] == "ok" and body["recall_verdict"] == expected, body
    assert [row["id"] for row in body["results"]] == (["verbatim:1"] if expected == "found" else [])
    assert [row["id"] for row in fed] == [row["id"] for row in body["results"]]
    assert body["_gate"]["rerank_relevance"]["dropped"] == 1
    assert body["_rerank"]["verbatim"]["applied"] is True
