"""Keep actual provider requests in the usage ledger across upgrades."""
import pytest

from ducky import decision, mem0_runtime


@pytest.fixture
def usage_channel(tmp_path, monkeypatch):
    monkeypatch.setattr(mem0_runtime, "USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setattr(mem0_runtime, "_llm_usage", {})
    decision._cache.clear()
    decision._circuits.clear()
    decision.reset_telemetry()
    yield {"enabled": True, "status": "ready", "provider": "nace", "model": "drex-v1.5",
           "api_key": "synthetic-usage-key", "tasks": {"retrieval": True}, "users": []}
    decision._cache.clear()
    decision._circuits.clear()


def totals():
    usage = mem0_runtime.get_llm_usage()
    return next(iter(usage.values()))["decision"]


def test_real_calls_and_failures_count_but_cache_does_not(usage_channel, monkeypatch):
    cfg = usage_channel
    monkeypatch.setitem(decision.PROVIDERS, "nace", lambda *_: {
        "model": cfg["model"], "answers": {"p0": {"type": "noul", "noul": .9}},
        "usage": {"input_tokens": 17}})
    for _ in range(2):
        answers, _ = decision.decide("retrieval", {"query": "first"}, {}, "u", "b", cfg)
        assert answers["p0"]["noul"] == .9
    assert totals()["calls"] == 1
    assert totals()["input_tokens"] == 17
    assert totals()["failures"] == 0

    def fail(*_):
        raise TimeoutError("synthetic timeout")

    monkeypatch.setitem(decision.PROVIDERS, "nace", fail)
    answers, info = decision.decide("retrieval", {"query": "second"}, {}, "u", "b", cfg)
    assert answers == {} and info["status"] == "error_fallback"
    assert totals()["calls"] == 2
    assert totals()["failures"] == 1
    assert totals()["models"][cfg["model"]] == {"calls": 2, "input_tokens": 17, "failures": 1}


@pytest.mark.parametrize("usage", [None, [], {"input_tokens": []}, {"input_tokens": -1},
                                   {"input_tokens": True}, {"input_tokens": "17"}])
def test_invalid_usage_cannot_change_a_success_into_failure(usage_channel, monkeypatch, usage):
    cfg = usage_channel
    monkeypatch.setitem(decision.PROVIDERS, "nace", lambda *_: {
        "model": cfg["model"], "answers": {"p0": {"type": "noul", "noul": .9}}, "usage": usage})
    answers, info = decision.decide("retrieval", {}, {}, "u", "b", cfg)
    assert answers and info["status"] == "ok" and info["input_tokens"] == 0
    assert totals()["calls"] == 1 and totals()["failures"] == 0


def test_usage_io_failure_retains_success_and_cache(usage_channel, monkeypatch):
    cfg = usage_channel
    monkeypatch.setitem(decision.PROVIDERS, "nace", lambda *_: {
        "model": cfg["model"], "answers": {"p0": {"type": "noul", "noul": .9}}})

    def fail(**_):
        raise OSError("synthetic read-only ledger")

    monkeypatch.setattr(mem0_runtime, "_track_decision_usage", fail)
    answers, info = decision.decide("retrieval", {}, {}, "u", "b", cfg)
    assert answers and info["status"] == "ok"
    _, info = decision.decide("retrieval", {}, {}, "u", "b", cfg)
    assert info["status"] == "cached"
