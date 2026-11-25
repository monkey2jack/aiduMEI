"""pi full adaptation: LLM fallback chain, distill retry hint, honest engine-mode probe."""
from __future__ import annotations

import pytest

from ducky import llm_client


def _cfg(monkeypatch, fallbacks):
    monkeypatch.setattr(llm_client, "get_llm_config", lambda: {
        "api_key": "k", "model": "primary", "base_url": "http://gw.invalid/v1", "fallbacks": fallbacks})
    monkeypatch.setattr("ducky.engine_mode.cloud_egress_allowed", lambda origin="": True)
    monkeypatch.setattr(llm_client, "record_llm_outcome", lambda *a, **k: None)


def test_fallback_model_answers_when_primary_is_empty(monkeypatch):
    _cfg(monkeypatch, ["backup"])
    seen = []

    def fake(endpoint, key, model, messages, budget, temperature, timeout, reasoning_effort=None):
        seen.append(model)
        return ("", False) if model == "primary" else ("ok from backup", False)

    monkeypatch.setattr(llm_client, "_post_completion", fake)
    assert llm_client.call_llm("hi") == "ok from backup"
    assert seen == ["primary", "backup"]


def test_fallback_model_answers_when_primary_raises(monkeypatch):
    _cfg(monkeypatch, ["backup", "backup", "primary", ""])
    seen = []

    def fake(endpoint, key, model, messages, budget, temperature, timeout, reasoning_effort=None):
        seen.append(model)
        if model == "primary":
            raise RuntimeError("upstream 500")
        return "ok", False

    monkeypatch.setattr(llm_client, "_post_completion", fake)
    assert llm_client.call_llm("hi") == "ok"
    assert seen == ["primary", "backup"], "duplicates and the primary itself are not retried as fallbacks"


def test_gate_timeout_never_switches_model(monkeypatch):
    _cfg(monkeypatch, ["backup"])
    seen = []

    def fake(endpoint, key, model, messages, budget, temperature, timeout, reasoning_effort=None):
        seen.append(model)
        raise llm_client.LLMConcurrencyTimeout("slot")

    monkeypatch.setattr(llm_client, "_post_completion", fake)
    assert llm_client.call_llm("hi") is None
    assert seen == ["primary"]


def test_no_fallbacks_keeps_single_model_contract(monkeypatch):
    _cfg(monkeypatch, [])
    monkeypatch.setattr(llm_client, "_post_completion",
                        lambda *a, **k: ("", False))
    assert llm_client.call_llm("hi") is None


def test_throttled_distill_carries_retry_after(monkeypatch):
    from ducky import session_distill as sd
    rows = [{"ref": f"r{i}", "text": f"t{i}", "turn": i, "created_at": ""} for i in range(5)]
    monkeypatch.setattr(sd, "collect_session_memories", lambda *a, **k: rows)
    monkeypatch.setattr(sd, "_latest_generation",
                        lambda *a, **k: {"refs": {"r0", "r1", "r2", "r3"}, "created_at": "x"})
    monkeypatch.setattr(sd, "_generation_age_seconds", lambda _c: 600.0)
    out = sd.distill_session("pi-s", user_id="u", bank_id="b")
    assert out["status"] == "skipped" and out["reason"] == "throttled"
    assert out["retry_after_sec"] == sd.MIN_INTERVAL_SEC - 600


def test_unchanged_distill_has_no_retry_hint(monkeypatch):
    from ducky import session_distill as sd
    rows = [{"ref": f"r{i}", "text": f"t{i}", "turn": i, "created_at": ""} for i in range(4)]
    monkeypatch.setattr(sd, "collect_session_memories", lambda *a, **k: rows)
    monkeypatch.setattr(sd, "_latest_generation",
                        lambda *a, **k: {"refs": {"r0", "r1", "r2", "r3"}, "created_at": "x"})
    monkeypatch.setattr(sd, "_generation_age_seconds", lambda _c: 10.0)
    out = sd.distill_session("pi-s", user_id="u", bank_id="b")
    assert out["reason"] == "unchanged" and out["retry_after_sec"] is None


@pytest.mark.parametrize("native,expected", [(True, False), (False, True)])
def test_engine_mode_reports_real_local_fallback(monkeypatch, native, expected):
    from ducky import engine_mode
    monkeypatch.setenv("AIDUMEI_ENGINE_MODE", "auto")
    monkeypatch.setattr("ducky.mem0_runtime.native_kernel_active", lambda: native)
    st = engine_mode.mode_status()
    assert st["local_leg"] is True
    assert st["local_fallback_index"] is expected
    assert st["kernel"] == ("native" if native else "mem0")
    if native:
        assert "FTS" in st["note"]
