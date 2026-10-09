"""Request admission/results have one deadline; transport is not preempted."""
from collections import Counter, OrderedDict
from contextvars import ContextVar, copy_context
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import threading

import pytest

from ducky import decision as d


@pytest.fixture
def channel(monkeypatch):
    now, calls, ledger = [100.0], [], []
    cfg = {"status": "ready", "enabled": True, "provider": "nace", "model": d.MODEL,
           "api_key": "synthetic-key", "timeout_ms": 2000, "threshold": .6, "mode": "always",
           "tasks": {"memory_type": True, "retrieval": True, "evidence_assessment": True}, "users": []}
    monkeypatch.setattr(d, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(d, "_request_budget", ContextVar("isolated_decision_test", default=None))
    monkeypatch.setattr(d, "_cache", OrderedDict())
    monkeypatch.setattr(d, "_circuits", {})
    monkeypatch.setattr(d, "_metrics", Counter())
    monkeypatch.setattr(d, "_slots", threading.BoundedSemaphore(2))
    monkeypatch.setattr(d, "settings", lambda: deepcopy(cfg))
    monkeypatch.setattr(d, "_track_usage", lambda *args: ledger.append(args))

    def provider(config, state, questions):
        calls.append((deepcopy(config), deepcopy(state), deepcopy(questions)))
        return {"model": config["model"], "answers": {
            key: {"type": "noul", "noul": .99 if key == "sufficient" else .01 if key in {"missing", "contradiction", "continue"} else .9}
            for key in questions}, "usage": {"input_tokens": 7}}

    monkeypatch.setitem(d.PROVIDERS, "nace", provider)
    d.reset_telemetry()
    return cfg, now, calls, ledger


def call(cfg, value="one", task="retrieval"):
    return d.decide(task, {"query": value}, {"p0": {"type": "noul"}}, "u", "b", cfg)


def test_shared_call_cap_cache_does_not_spend_calls_or_usage(channel):
    cfg, now, calls, ledger = channel
    for index in range(3):
        assert call(cfg, str(index))[1]["status"] == "ok"
    assert call(cfg, "four")[1]["status"] == "call_budget_fallback"
    assert call(cfg, "2")[1]["status"] == "cached"
    assert len(calls) == len(ledger) == 3
    assert d.retrieval_budget()["calls"] == 3 and d.retrieval_budget()["cache_hits"] == 1
    now[0] = 103.0
    assert call(cfg, "2") == ({}, d.telemetry()["stages"][-1])
    assert d.telemetry()["stages"][-1]["status"] == "deadline_fallback"
    d.reset_telemetry()
    assert d.telemetry() == {"stages": []}
    assert call(cfg, "2")[1]["status"] == "cached"
    assert d.retrieval_budget()["calls"] == 0 and len(calls) == 3


def test_cumulative_deadline_clamps_timeout_and_discards_late_success(channel, monkeypatch):
    cfg, now, calls, ledger = channel
    def slow(config, state, questions):
        calls.append(config["timeout_ms"])
        now[0] += 1.8
        return {"model": cfg["model"], "answers": {"p0": {"type": "noul", "noul": .9}}}
    monkeypatch.setitem(d.PROVIDERS, "nace", slow)
    assert call(cfg)[1]["status"] == "ok"
    answers, info = call(cfg, "two")
    assert answers == {} and info["status"] == "deadline_fallback"
    assert calls == pytest.approx([2000, 1200])
    assert len(d._cache) == 1 and len(ledger) == 2 and ledger[-1][3] is True
    assert d.retrieval_budget()["calls"] == 2
    assert call(cfg, "three")[1]["status"] == "deadline_fallback"
    assert now[0] == pytest.approx(103.6)  # Discarding does not claim to preempt transport.


def test_expiry_during_usage_accounting_cannot_cache_or_apply(channel, monkeypatch):
    cfg, now, _, _ = channel
    monkeypatch.setattr(d, "_track_usage", lambda *args: now.__setitem__(0, 104))
    assert call(cfg)[1]["status"] == "deadline_fallback"
    assert not d._cache


def test_filter_rechecks_deadline_before_applying_support(channel, monkeypatch):
    cfg, now, _, _ = channel
    def result(*args):
        now[0] = 104
        return {"p0": {"type": "noul", "noul": .01}}, {"status": "ok", "applied": True}
    monkeypatch.setattr(d, "decide", result)
    baseline = [{"memory": "Preserve this baseline on expiry.", "_rerank_score": .5}]
    assert d.filter_evidence("What is the fact?", baseline, "u", "b") == baseline
    assert "_decision_support" not in baseline[0]


def test_classification_keeps_separate_timeout_and_call_count(channel, monkeypatch):
    cfg, now, calls, _ = channel
    cfg["timeout_ms"] = 5000
    now[0] += 4
    def choice(config, *args):
        calls.append(config["timeout_ms"])
        return {"model": cfg["model"], "answers": {"type": {
            "type": "choice", "choice": "FACTS", "confidence": .9}}}
    monkeypatch.setitem(d.PROVIDERS, "nace", choice)
    assert d.classify("Synthetic fact", "u", "b") == ("FACTS", .9)
    assert calls == [5000] and d.retrieval_budget()["calls"] == 0
    assert call(cfg)[1]["status"] == "deadline_fallback"


@pytest.mark.parametrize("kwargs", [{"retrieval_max_calls": v} for v in (0, 4, True, 1.5)] +
                         [{"retrieval_timeout_ms": v} for v in (0, 3001, True, float("nan"), float("inf"))])
def test_reset_validates_lower_only_hard_budgets(channel, kwargs):
    with pytest.raises(ValueError):
        d.reset_telemetry(**kwargs)


def test_missing_request_scope_does_not_silently_start_a_fresh_budget(channel):
    cfg, _, calls, _ = channel
    d._request_budget.set(None)
    assert call(cfg)[1]["status"] == "request_budget_missing" and not calls


def test_busy_and_circuit_fallback_do_not_spend_budget(channel, monkeypatch):
    cfg, _, calls, ledger = channel
    d._slots.acquire()
    d._slots.acquire()
    try:
        assert call(cfg)[1]["status"] == "busy_fallback"
        assert not calls and d.retrieval_budget()["calls"] == 0
    finally:
        d._slots.release()
        d._slots.release()
    def fail(*args):
        raise TimeoutError("must-not-echo-sensitive-error")
    monkeypatch.setitem(d.PROVIDERS, "nace", fail)
    for index in range(3):
        assert call(cfg, str(index))[1]["status"] == "error_fallback"
    assert call(cfg, "four")[1]["status"] == "circuit_open"
    assert len(ledger) == d.retrieval_budget()["calls"] == 3
    assert "must-not-echo" not in str(d.telemetry())


@pytest.mark.parametrize("bad", [None, [], {"answers": []}, {"model": "wrong", "answers": {}},
                                 {"answers": {"p0": {"type": "noul", "noul": float("nan")}}}])
def test_malformed_responses_are_counted_but_not_cached(channel, monkeypatch, bad):
    cfg, _, _, ledger = channel
    monkeypatch.setitem(d.PROVIDERS, "nace", lambda *a: bad)
    answers, info = call(cfg)
    assert not answers and info["status"] == "error_fallback"
    assert d.retrieval_budget()["calls"] == 1 and len(ledger) == 1 and not d._cache


def test_cache_validation_and_copy_isolation(channel):
    cfg, _, calls, ledger = channel
    answers, _ = call(cfg)
    answers["p0"]["noul"] = -1
    assert call(cfg)[0]["p0"]["noul"] == .9
    next(iter(d._cache.values()))[1]["answers"]["p0"]["noul"] = float("nan")
    answers, info = call(cfg)
    assert answers == {} and info["status"] == "invalid_cache_fallback"
    assert len(calls) == len(ledger) == 1 and not d._cache


def test_partial_valid_support_is_preserved_but_not_cached(channel, monkeypatch):
    cfg, _, _, ledger = channel
    monkeypatch.setitem(d.PROVIDERS, "nace", lambda *a: {"answers": {"p0": {"type": "noul", "noul": .1}}})
    q = {"p0": {"type": "noul"}, "p1": {"type": "noul"}}
    for _ in range(2):
        answers, info = d.decide("retrieval", {}, q, "u", "b", cfg)
        assert answers == {"p0": {"type": "noul", "noul": .1}} and not info["response_complete"]
    assert not d._cache and len(ledger) == 2


def test_copied_contexts_share_atomic_budget(channel, monkeypatch):
    cfg, _, calls, _ = channel
    d.reset_telemetry(retrieval_max_calls=1)
    entered, release = threading.Event(), threading.Event()
    def provider(*args):
        calls.append(1)
        entered.set()
        assert release.wait(3)
        return {"answers": {"p0": {"type": "noul", "noul": .9}}}
    monkeypatch.setitem(d.PROVIDERS, "nace", provider)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(copy_context().run, call, cfg, "one")
        assert entered.wait(3)
        try:
            second = pool.submit(copy_context().run, call, cfg, "two").result(timeout=2)
            assert second[1]["status"] == "call_budget_fallback"
        finally:
            release.set()
        assert first.result(timeout=2)[1]["status"] == "ok"
    assert calls == [1] and d.retrieval_budget()["calls"] == 1
