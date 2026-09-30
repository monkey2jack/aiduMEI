"""Behavior regressions for relevance evidence and post-rank text fusion."""
import json
import sqlite3

import pytest

from ducky.hot.search import annotate_recall_strength, compute_recall_verdict
from ducky import query_aliases, scoring
from ducky.verbatim_vault import fuse_verbatim


def candidate(raw=0.38, rerank=0.99, fused=0.7):
    return {"id": "synthetic", "memory": "The confirmed decision is November.",
            "score": raw, "_rerank_score": rerank, "_hybrid_score": fused}


def test_strong_current_rerank_survives_without_lowering_vector_floor(monkeypatch):
    monkeypatch.delenv("AIDUMEI_RERANK_RESCUE_THRESHOLD", raising=False)
    rows = [candidate()]
    info = annotate_recall_strength(rows, floor=0.46, allow_rerank=True)
    assert len(rows) == 1 and info["rerank_rescued"] == 1
    assert info["top_score"] == 0.38 and info["decision_score"] == 0.7
    assert compute_recall_verdict(rows, info["decision_score"], 0.46)[0] == "found"


@pytest.mark.parametrize("updates", [
    {"_rerank_score": 0.2}, {"_hybrid_score": 0.3}, {"score": -0.1},
    {"_rerank_score": float("nan")}, {"_rerank_score": float("inf")},
    {"_rerank_score": 1.1}, {"_rerank_score": True}, {"_rerank_score": "0.99"},
])
def test_untrusted_or_insufficient_rerank_cannot_rescue(updates):
    rows = [dict(candidate(), **updates)]
    annotate_recall_strength(rows, floor=0.46, allow_rerank=True)
    assert rows == []


def test_cache_or_failed_call_cannot_reuse_old_rerank():
    rows = [candidate()]
    annotate_recall_strength(rows, floor=0.46)
    assert rows == []


def test_raw_score_passes_when_rerank_unavailable():
    rows = [candidate(raw=0.6, rerank=0.1)]
    annotate_recall_strength(rows, floor=0.46, allow_rerank=True)
    assert len(rows) == 1


def test_channel_failure_clears_stale_scores(monkeypatch):
    import ducky.mem0_runtime as runtime
    monkeypatch.setattr(runtime, "rerank", lambda *a, **k: None)
    rows = [candidate()]
    assert scoring._apply_rerank("new query", rows, 1) is False
    assert "_rerank_score" not in rows[0]


def test_all_candidates_reranked_and_only_valid_indices_apply(monkeypatch):
    import ducky.mem0_runtime as runtime
    calls = []
    def rank(q, docs, top_n):
        calls.append(top_n)
        return [{"index": True, "relevance_score": 0.99},
                {"index": 3, "relevance_score": 0.95}]
    monkeypatch.setattr(runtime, "rerank", rank)
    rows = [candidate() for _ in range(4)]
    assert scoring._apply_rerank("new query", rows, 1)
    assert calls == [4] and "_rerank_score" not in rows[1]
    assert rows[3]["_rerank_score"] == 0.95


def test_fact_query_originals_do_not_displace_ranked_decisions():
    main = [{"id": str(i), "memory": f"项目升级决定{i}", "memory_type": "DECISIONS"} for i in range(3)]
    original = [{"id": "verbatim:1", "memory": "项目升级决定的完整长篇对话"}]
    assert fuse_verbatim(main, original, limit=3, query="项目升级决定是什么") == main


def test_original_wording_query_keeps_original_quota():
    main = [{"id": str(i), "memory": f"项目升级决定{i}"} for i in range(3)]
    original = [{"id": "verbatim:1", "memory": "项目升级决定的完整原话"}]
    merged = fuse_verbatim(main, original, limit=3, query="项目升级决定的原话是什么")
    assert len(merged) == 3 and merged[-1]["id"] == "verbatim:1"


@pytest.fixture
def aliases(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE entities(entity_id INTEGER PRIMARY KEY, name TEXT, aliases TEXT, user_id TEXT, bank_id TEXT)")
    conn.execute("CREATE TABLE facts(id INTEGER PRIMARY KEY, fact_key TEXT, fact_value TEXT, user_id TEXT, bank_id TEXT, category TEXT, epistemic_mode TEXT, confidence INTEGER, archived INTEGER, superseded_by INTEGER, expires_at TEXT, valid_to TEXT, valid_from TEXT)")
    conn.executemany("INSERT INTO entities VALUES (?,?,?,?,?)", [
        (1, "研究员甲", json.dumps(["青松", "alex"]), "alice", "work"),
        (2, "研究员乙", json.dumps(["青松"]), "bob", "work"),
        (3, "研究员丙", json.dumps(["青松"]), "alice", "home"),
    ])
    monkeypatch.setattr(query_aliases, "get_facts_conn", lambda: conn)
    yield conn
    conn.close()


def test_aliases_apply_only_to_exact_user_bank(aliases):
    assert query_aliases.resolve_query_aliases("青松的生日", "alice", "work") == "研究员甲的生日"
    assert query_aliases.resolve_query_aliases("青松的生日", "alice", "home") == "研究员丙的生日"
    assert query_aliases.resolve_query_aliases("青松的生日", "carol", "work") == "青松的生日"


def test_ambiguous_alias_is_not_guessed(aliases):
    aliases.execute("INSERT INTO entities VALUES (4, '研究员丁', '[\"青松\"]', 'alice','work')")
    assert query_aliases.resolve_query_aliases("青松的生日", "alice", "work") == "青松的生日"


def test_alias_boundary_and_non_recursive_replacement(aliases):
    assert query_aliases.resolve_query_aliases("alexander", "alice", "work") == "alexander"
    assert query_aliases.resolve_query_aliases("alex birthday", "alice", "work") == "研究员甲 birthday"


def test_confirmed_alias_fact_and_retraction(aliases):
    aliases.execute("INSERT INTO facts VALUES (1,'研究员戊','[\"白鹭\"]','alice','work','entity_alias','user_provided',100,0,NULL,NULL,NULL,NULL)")
    assert query_aliases.resolve_query_aliases("白鹭的生日", "alice", "work") == "研究员戊的生日"
    aliases.execute("UPDATE facts SET archived=1")
    assert query_aliases.resolve_query_aliases("白鹭的生日", "alice", "work") == "白鹭的生日"


def test_empty_final_results_retain_weak_evidence_telemetry(monkeypatch):
    from ducky.hot.search import _final_recall_strength
    monkeypatch.setenv("AIDUMEM_RECALL_SCORE_FLOOR", "0.46")
    rows = [candidate()]
    main = annotate_recall_strength(rows, floor=0.46)
    final = _final_recall_strength(rows, main, False)
    assert rows == [] and final["weak"] is True
    assert final["dropped"] == 1 and final["top_score"] == 0.38


def test_fresh_rerank_requires_current_hybrid_telemetry(monkeypatch):
    from ducky.hot.search import _fresh_rerank_allowed
    import ducky.mem0_runtime as runtime
    monkeypatch.setattr(runtime, "last_rerank_telemetry", lambda: {"status": "ok", "applied": True})
    assert _fresh_rerank_allowed("hybrid") is True
    assert _fresh_rerank_allowed("cache") is False
    monkeypatch.setattr(runtime, "last_rerank_telemetry", lambda: {"status": "error", "applied": True})
    assert _fresh_rerank_allowed("hybrid") is False
