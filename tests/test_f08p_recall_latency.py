"""f0.8+ C (plan item 9): /search latency — rerank trimming, time budget, breaker,
content-addressed rerank cache, and the short /search response cache."""
import time
from copy import deepcopy

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ducky.mem0_runtime as rt
from ducky import search_cache


@pytest.fixture()
def provider(monkeypatch):
    """A configured fake reranker; returns the list of calls it received."""
    calls = []
    behaviour = {"fn": None}

    def handler(cfg, query, docs, top_n):
        calls.append({"docs": list(docs), "top_n": top_n, "timeout": cfg.get("_timeout")})
        if behaviour["fn"]:
            return behaviour["fn"](docs)
        return [{"index": i, "relevance_score": round(1 - i / 100, 3)} for i in range(len(docs))]

    monkeypatch.setattr(rt, "_load_rerank_config", lambda: {
        "enabled": True, "provider": "fake", "model": "m", "base_url": "https://rr.invalid/v1", "api_key": "k"})
    monkeypatch.setitem(rt.RERANK_PROVIDERS, "fake", handler)
    from ducky import engine_mode
    monkeypatch.setattr(engine_mode, "cloud_egress_allowed", lambda *_a, **_k: True)
    from ducky.security import egress
    monkeypatch.setattr(egress, "guard_url", lambda url, purpose="egress": url)
    monkeypatch.setattr(rt, "_track_rerank_usage", lambda **k: None)
    rt.rerank_cache_clear()
    rt.clear_rerank_budget()
    yield calls, behaviour
    rt.rerank_cache_clear()
    rt.clear_rerank_budget()


# ── trimming ─────────────────────────────────────────────────────────────
def test_docs_are_clipped_and_capped_but_indices_stay_valid(provider, monkeypatch):
    calls, _ = provider
    monkeypatch.setenv("AIDUMEI_RERANK_DOC_CHARS", "10")
    monkeypatch.setenv("AIDUMEI_RERANK_MAX_DOCS", "3")
    out = rt.rerank("q", ["x" * 50, "short", "y" * 11, "never sent"], top_n=4)
    assert calls[0]["docs"] == ["x" * 10, "short", "y" * 10] and calls[0]["top_n"] == 3
    assert {r["index"] for r in out} == {0, 1, 2}
    t = rt.last_rerank_telemetry()
    assert t["status"] == "ok" and t["docs_capped"] == 1 and t["docs_clipped"] == 2 and t["sent"] == 3


# ── time budget / timeout ────────────────────────────────────────────────
def test_timeout_returns_unreranked_and_is_flagged(provider):
    calls, behaviour = provider

    def slow(docs):
        raise requests.exceptions.ReadTimeout("synthetic")
    behaviour["fn"] = slow
    assert rt.rerank("q", ["a", "b"]) == []
    t = rt.last_rerank_telemetry()
    assert t["status"] == "timeout" and t["degraded"] is True


def test_per_request_budget_bounds_the_call_timeout(provider, monkeypatch):
    calls, _ = provider
    monkeypatch.setenv("AIDUMEI_RERANK_TIMEOUT_MS", "2500")
    rt.start_rerank_budget(800)
    rt.rerank("q", ["a"])
    connect, read = calls[0]["timeout"]
    assert read <= 0.8 and connect <= read


def test_spent_budget_skips_the_call(provider):
    calls, _ = provider
    rt.start_rerank_budget(100)          # below the 150 ms floor
    assert rt.rerank("q", ["a"]) == []
    assert calls == [] and rt.last_rerank_telemetry()["status"] == "budget_exhausted"
    rt.clear_rerank_budget()
    rt.rerank("q", ["a"])
    assert len(calls) == 1


# ── breaker ──────────────────────────────────────────────────────────────
def test_breaker_opens_after_consecutive_failures_and_half_opens(provider, monkeypatch):
    calls, behaviour = provider
    monkeypatch.setenv("AIDUMEI_RERANK_BREAKER_FAILS", "2")
    monkeypatch.setenv("AIDUMEI_RERANK_BREAKER_COOLDOWN_S", "0.2")

    def boom(docs):
        raise RuntimeError("rerank HTTP 503")
    behaviour["fn"] = boom
    rt.rerank("q", ["a"])
    rt.rerank("q", ["a"])
    assert len(calls) == 2
    assert rt.rerank("q", ["a"]) == [] and rt.last_rerank_telemetry()["status"] == "circuit_open"
    assert len(calls) == 2
    time.sleep(0.25)
    behaviour["fn"] = None
    assert rt.rerank("q", ["a"]) and len(calls) == 3          # half-open probe succeeds
    assert rt.last_rerank_telemetry()["status"] == "ok"


# ── rerank result cache ──────────────────────────────────────────────────
def test_rerank_cache_is_content_addressed(provider, monkeypatch):
    calls, _ = provider
    monkeypatch.setenv("AIDUMEI_RERANK_CACHE_TTL_S", "60")
    first = rt.rerank("q", ["a", "b"])
    again = rt.rerank("q", ["a", "b"])
    assert first == again and len(calls) == 1 and rt.last_rerank_telemetry()["cached"] is True
    rt.rerank("q", ["a", "b changed"])
    rt.rerank("other q", ["a", "b"])
    assert len(calls) == 3


def test_rerank_cache_off_by_ttl_zero(provider, monkeypatch):
    calls, _ = provider
    monkeypatch.setenv("AIDUMEI_RERANK_CACHE_TTL_S", "0")
    rt.rerank("q", ["a"])
    rt.rerank("q", ["a"])
    assert len(calls) == 2


# ── scoring: only the best candidates are sent, tail is fused on the same scale ──
def test_apply_rerank_sends_top_candidates_and_maps_back(monkeypatch):
    from ducky import scoring
    monkeypatch.setenv("AIDUMEI_RERANK_MAX_DOCS", "2")
    sent = []

    def rank(q, docs, top_n):
        sent.append(list(docs))
        return [{"index": 0, "relevance_score": 0.2}, {"index": 1, "relevance_score": 0.9}]
    monkeypatch.setattr(rt, "rerank", rank)
    monkeypatch.setenv("AIDUMEI_RERANK_MIN_RELEVANCE", "0")
    rows = [{"id": "low", "memory": "low", "_hybrid_score": 0.3},
            {"id": "top", "memory": "top", "_hybrid_score": 0.9},
            {"id": "mid", "memory": "mid", "_hybrid_score": 0.6}]
    assert scoring._apply_rerank("q", rows, 3) is True
    assert sent == [["top", "mid"]]
    by = {r["id"]: r for r in rows}
    assert by["top"]["_rerank_score"] == 0.2 and by["mid"]["_rerank_score"] == 0.9
    assert "_rerank_score" not in by["low"]
    w = scoring.RERANK_WEIGHT
    assert by["low"]["_hybrid_score"] == round(0.3 * (1 - w) + 0.2 * w, 4)


# ── /search response cache (unit) ────────────────────────────────────────
@pytest.fixture()
def cache_on(monkeypatch):
    monkeypatch.setenv("AIDUMEI_SEARCH_CACHE_TTL_S", "30")
    search_cache.clear()
    yield
    search_cache.clear()


def _key(user="u1", query="q", **kw):
    args = dict(user_id=user, bank_id="default", caller="", query=query, limit=5,
                before="", after="", session_id="")
    args.update(kw)
    return search_cache.make_key(**args)


def test_cache_roundtrip_and_user_scoped_invalidation(cache_on):
    ok = {"status": "ok", "results": [{"id": "1"}], "recall_verdict": "found"}
    search_cache.put(_key("u1"), "u1", ok)
    search_cache.put(_key("u2"), "u2", ok)
    hit = search_cache.get(_key("u1"), "u1")
    assert hit["results"] == ok["results"] and hit["_cache"]["hit"] is True
    hit["results"].append("mutated")
    assert search_cache.get(_key("u1"), "u1")["results"] == ok["results"]
    search_cache.invalidate_user("u1")
    assert search_cache.get(_key("u1"), "u1") is None
    assert search_cache.get(_key("u2"), "u2") is not None
    search_cache.invalidate_user(None)          # unknown writer → everything
    assert search_cache.get(_key("u2"), "u2") is None


def test_key_separates_scope_caller_session_and_window(cache_on):
    keys = {_key(), _key(user="u2"), _key(bank_id="b"), _key(caller="c"), _key(session_id="s"),
            _key(before="2026-01-01"), _key(limit=6), _key(query="q2")}
    assert len(keys) == 8
    ok = {"status": "ok", "results": [], "recall_verdict": "not_found"}
    search_cache.put(_key(), "u1", ok)
    assert search_cache.get(_key(), "u2") is None        # owner must match too


@pytest.mark.parametrize("resp", [
    {"status": "error", "results": []},
    {"status": "ok", "results": [], "recall_verdict": "degraded"},
    {"status": "ok", "results": [], "recall_verdict": "found", "_rerank_degraded": "timeout"},
])
def test_degraded_answers_are_never_cached(cache_on, resp):
    search_cache.put(_key(), "u1", resp)
    assert search_cache.get(_key(), "u1") is None


def test_ttl_zero_disables(monkeypatch):
    monkeypatch.setenv("AIDUMEI_SEARCH_CACHE_TTL_S", "0")
    search_cache.put(_key(), "u1", {"status": "ok", "results": [], "recall_verdict": "found"})
    assert search_cache.get(_key(), "u1") is None


def test_http_writes_invalidate(cache_on):
    ok = {"status": "ok", "results": [], "recall_verdict": "found"}
    search_cache.put(_key("u1"), "u1", ok)
    search_cache.put(_key("u2"), "u2", ok)
    search_cache.invalidate_for_http("POST", "/search", None, b'{"user_id":"u1"}')
    search_cache.invalidate_for_http("GET", "/health", None, None)
    assert search_cache.get(_key("u1"), "u1") is not None
    search_cache.invalidate_for_http("POST", "/add", None, b'{"user_id":"u1"}')
    assert search_cache.get(_key("u1"), "u1") is None
    assert search_cache.get(_key("u2"), "u2") is not None
    search_cache.invalidate_for_http("DELETE", "/memories/x", None, None)   # unknown user
    assert search_cache.get(_key("u2"), "u2") is None


# ── /search route wiring ─────────────────────────────────────────────────
@pytest.fixture()
def search_app(monkeypatch):
    from ducky import decision, memory_workspace, verbatim_vault
    from ducky.hot import search
    state = {"rows": [{"id": "a", "memory": "Synthetic fact alpha", "score": .9}], "calls": 0}

    def hybrid(*a, **k):
        state["calls"] += 1
        return deepcopy(state["rows"])
    monkeypatch.setenv("AIDUMEM_RECALL_SCORE_FLOOR", "0.1")
    monkeypatch.setattr(decision, "settings", lambda: {"status": "disabled"})
    monkeypatch.setattr(memory_workspace, "ws_lookup", lambda *a, **k: [])
    monkeypatch.setattr(memory_workspace, "ws_feed_from_results", lambda *a, **k: None)
    monkeypatch.setattr(search, "get_memory", lambda: object())
    monkeypatch.setattr(search, "lazy_import_hybrid", lambda: hybrid)
    monkeypatch.setattr(search, "ensure_bank_registered", lambda *a: None)
    monkeypatch.setattr(search, "boost_salience_for_results", lambda *a, **k: None)
    monkeypatch.setattr(search, "_annotate_memory_types", lambda *a, **k: None)
    monkeypatch.setattr(search, "_log_search_result", lambda *a: None)
    monkeypatch.setattr(search, "_observe_final_evidence", lambda *a, **k: {"status": "skipped"})
    monkeypatch.setattr(verbatim_vault, "verbatim_search", lambda *a, **k: [])
    app = FastAPI()
    search.register_search_routes(app)
    return TestClient(app), state


def test_search_repeats_hit_cache_until_a_write(search_app, cache_on):
    client, state = search_app
    body = {"query": "alpha", "user_id": "cache-owner", "limit": 3}
    first = client.post("/search", json=body).json()
    second = client.post("/search", json=body).json()
    assert state["calls"] == 1 and second["_cache"]["hit"] is True
    assert second["results"] == first["results"] and "_cache" not in first
    search_cache.invalidate_user("cache-owner")        # what a write does
    state["rows"] = [{"id": "b", "memory": "Synthetic fact beta", "score": .9}]
    third = client.post("/search", json=body).json()
    assert state["calls"] == 2 and third["results"][0]["id"] == "b"


def test_search_budget_is_cleared_after_the_request(search_app, cache_on):
    client, _ = search_app
    client.post("/search", json={"query": "alpha", "user_id": "u", "limit": 1})
    assert getattr(rt._rerank_tls, "deadline", None) is None


def test_rerank_timeout_is_flagged_and_not_cached(search_app, cache_on, monkeypatch):
    client, state = search_app
    from ducky.hot import search

    def timed_out(*a, **k):
        rt._rerank_tls.last = {"status": "timeout", "degraded": True}
        return deepcopy(state["rows"])
    monkeypatch.setattr(search, "lazy_import_hybrid", lambda: timed_out)
    body = {"query": "alpha", "user_id": "u-timeout", "limit": 1}
    r1 = client.post("/search", json=body).json()
    assert r1["_rerank_degraded"] == "timeout" and r1["results"]
    r2 = client.post("/search", json=body).json()
    assert "_cache" not in r2


# ── egress re-check: DNS answer reused for a short TTL ──────────────────
def test_egress_default_resolver_caches_successful_lookups(monkeypatch):
    from ducky.security import egress
    egress.set_resolver(None)
    seen = []

    def gai(host, *a, **k):
        seen.append(host)
        return [(2, 1, 6, "", ("93.184.216.34", 0))]  # release-scan:allow synthetic security fixture, no production credential/address
    monkeypatch.setattr(egress.socket, "getaddrinfo", gai)
    monkeypatch.setenv("AIDUMEI_EGRESS_DNS_CACHE_S", "60")
    assert egress.guard_url("https://rr.example.com/v1", "rerank")
    assert egress.guard_url("https://rr.example.com/v1", "rerank")
    assert seen == ["rr.example.com"]
    monkeypatch.setenv("AIDUMEI_EGRESS_DNS_CACHE_S", "0")
    egress.guard_url("https://rr.example.com/v1", "rerank")
    assert len(seen) == 2
    egress.set_resolver(None)


def test_egress_cache_never_hides_a_private_answer(monkeypatch):
    from ducky.security import egress
    egress.set_resolver(None)
    monkeypatch.setenv("AIDUMEI_EGRESS_DNS_CACHE_S", "60")
    monkeypatch.setattr(egress.socket, "getaddrinfo",
                        lambda host, *a, **k: [(2, 1, 6, "", ("10.0.0.5", 0))])
    with pytest.raises(egress.EgressBlocked):
        egress.guard_url("https://inner.example.com/v1", "rerank")
    with pytest.raises(egress.EgressBlocked):     # cached answer is re-judged every time
        egress.guard_url("https://inner.example.com/v1", "rerank")
    egress.set_resolver(None)


def test_wall_clock_bound_covers_more_than_the_http_timeout(provider):
    calls, behaviour = provider

    def hang(docs):
        time.sleep(1.0)          # e.g. DNS + TLS re-handshake, not covered by read timeout
        return [{"index": 0, "relevance_score": 0.5}]
    behaviour["fn"] = hang
    rt.start_rerank_budget(300)
    t = time.perf_counter()
    assert rt.rerank("q", ["a"]) == []
    assert time.perf_counter() - t < 0.6
    assert rt.last_rerank_telemetry()["status"] == "timeout"
