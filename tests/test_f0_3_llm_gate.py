"""f0.3 A4 -- one process-level LLM concurrency gate for call_llm AND mem0.

Production fact: the upstream LLM provider answers "concurrency reached, current: 6,
limit: 5" ~134 times/day; the service had no process-level limit -- call_llm and
the mem0 extraction client each fired whenever they liked.

Concurrency is measured, not asserted from source: fake upstreams hold each call
for a moment and record the peak number of simultaneous calls. Every bound is
paired with a negative control showing the same hammer really overlaps when the
gate is wide, so "peak == limit" cannot pass vacuously.
"""
from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

import ducky.llm_client as llm_client
from ducky.speed import config as speed_config
from ducky.speed import patch as speed_patch


class _Tracker:
    def __init__(self, hold: float = 0.15):
        self.lock = threading.Lock()
        self.active = self.peak = self.calls = 0
        self.hold = hold

    def run(self):
        with self.lock:
            self.active += 1
            self.calls += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(self.hold)
        finally:
            with self.lock:
                self.active -= 1


class _Completions:
    def __init__(self, tracker):
        self.tracker = tracker

    def create(self, *args, **kwargs):
        self.tracker.run()
        return {"choices": [{"message": {"content": "ok"}}]}


def _fake_mem(tracker):
    client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions(tracker)))
    return SimpleNamespace(llm=SimpleNamespace(client=client))


def _hammer(fn, n: int) -> list:
    barrier = threading.Barrier(n)
    results: list = [None] * n

    def worker(i):
        barrier.wait()
        try:
            results[i] = fn()
        except Exception as exc:  # surfaced to the assertion, not swallowed
            results[i] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return results


@pytest.fixture
def gate(monkeypatch):
    def _set(limit: int, timeout: float = 10.0):
        monkeypatch.setattr(llm_client, "_LLM_SLOTS", threading.BoundedSemaphore(limit))
        monkeypatch.setattr(llm_client, "LLM_MAX_CONCURRENCY", limit)
        monkeypatch.setattr(llm_client, "LLM_SLOT_TIMEOUT_SEC", timeout)
    return _set


@pytest.fixture(autouse=True)
def _isolated_ledger(monkeypatch, tmp_path):
    from ducky import utils
    monkeypatch.setattr(utils, "DATA_DIR", str(tmp_path))


@pytest.fixture
def upstream(monkeypatch):
    """call_llm's upstream: requests.post held for a moment, peak concurrency recorded."""
    tracker = _Tracker()
    monkeypatch.setattr(llm_client, "_config_cache",
                        {"model": "m", "base_url": "http://llm.invalid/v1", "api_key": "k"})

    class _Resp:
        status_code = 200
        text = json.dumps({"choices": [{"message": {"content": "ok"}}]})

    def fake_post(*args, **kwargs):
        tracker.run()
        return _Resp()

    monkeypatch.setattr(llm_client.requests, "post", fake_post)
    return tracker


@pytest.fixture
def speed_cfg(monkeypatch, tmp_path):
    cfg = tmp_path / "mem0_config_local.json"
    cfg.write_text(json.dumps({"_speed": {}, "llm": {"config": {"max_tokens": 64}}}), encoding="utf-8")
    monkeypatch.setattr(speed_config, "_CFG_PATH", str(cfg))
    monkeypatch.setattr(speed_patch, "_CFG_PATH", str(cfg))
    monkeypatch.setattr(speed_config, "_speed_cfg_cache", None)
    monkeypatch.setattr(speed_config, "_speed_cfg_mtime", 0.0)


# ---------------------------------------------------------------- call_llm
def test_call_llm_concurrency_is_bounded(gate, upstream):
    gate(2)
    assert _hammer(lambda: llm_client.call_llm("hi"), 8) == ["ok"] * 8
    assert upstream.calls == 8
    assert upstream.peak == 2, "bounded by the gate AND genuinely concurrent up to it"


def test_negative_control_wide_gate_lets_call_llm_overlap(gate, upstream):
    gate(8)
    _hammer(lambda: llm_client.call_llm("hi"), 8)
    assert upstream.peak > 2


# ---------------------------------------------------------------- mem0 extraction client
def test_mem0_wrapper_concurrency_is_bounded(gate, speed_cfg):
    gate(2)
    tracker = _Tracker()
    mem = _fake_mem(tracker)
    speed_patch.patch_llm_for_speed(mem)
    results = _hammer(lambda: mem.llm.client.chat.completions.create(model="m", messages=[]), 8)
    assert all(isinstance(r, dict) for r in results), results
    assert tracker.calls == 8 and tracker.peak == 2


def test_negative_control_unpatched_mem0_client_overlaps(gate):
    gate(2)
    tracker = _Tracker()
    mem = _fake_mem(tracker)   # NOT patched: bypasses the gate
    _hammer(lambda: mem.llm.client.chat.completions.create(model="m", messages=[]), 8)
    assert tracker.peak > 2


def test_both_channels_share_one_gate(gate, upstream, speed_cfg):
    gate(3)
    mem = _fake_mem(upstream)          # same tracker for both channels
    speed_patch.patch_llm_for_speed(mem)
    fns = [lambda: llm_client.call_llm("hi"),
           lambda: mem.llm.client.chat.completions.create(model="m", messages=[])]
    counter = iter(range(10))
    results = _hammer(lambda: fns[next(counter) % 2](), 10)
    assert not any(isinstance(r, Exception) for r in results), results
    assert upstream.calls == 10
    assert upstream.peak == 3, "one process-wide limit, not one per channel"


# ---------------------------------------------------------------- timeout = LLM failure, no deadlock
def test_gate_timeout_is_an_llm_failure_not_a_deadlock(gate, upstream, speed_cfg):
    gate(1, timeout=0.05)
    assert llm_client._LLM_SLOTS.acquire(timeout=1)       # a stuck caller holds the only slot
    try:
        t0 = time.monotonic()
        assert llm_client.call_llm("hi") is None           # call_llm contract: failure -> None
        assert time.monotonic() - t0 < 2
        assert upstream.calls == 0, "must not reach upstream without a slot"

        mem = _fake_mem(_Tracker())
        speed_patch.patch_llm_for_speed(mem)
        with pytest.raises(llm_client.LLMConcurrencyTimeout) as exc:
            mem.llm.client.chat.completions.create(model="m", messages=[])
        assert isinstance(exc.value, TimeoutError)
    finally:
        llm_client._LLM_SLOTS.release()
    assert llm_client.call_llm("hi") == "ok"               # slot freed -> traffic flows again
    assert llm_client._LLM_SLOTS.acquire(blocking=False)   # nothing leaked
    llm_client._LLM_SLOTS.release()


def test_timeout_propagates_out_of_the_real_mem0_openai_llm(gate, speed_cfg, monkeypatch):
    """Signature-aligned double: mem0's real OpenAILLM + real openai client.

    mem0 2.2.1 wraps any exception raised by `self.llm.generate_response` into
    LLMError (memory/main.py), which the write path already treats as an LLM
    failure (record_llm_failure + deterministic direct write).
    """
    from mem0.llms.openai import OpenAILLM

    # The client is never used for I/O here, but httpx inspects proxy env at
    # construction time (a SOCKS proxy without socksio raises ImportError).
    for var in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(var, raising=False)

    real_llm = OpenAILLM({"model": "m", "api_key": "k", "openai_base_url": "http://127.0.0.1:9/v1"})
    mem = SimpleNamespace(llm=real_llm)
    speed_patch.patch_llm_for_speed(mem)
    gate(1, timeout=0.05)
    assert llm_client._LLM_SLOTS.acquire(timeout=1)
    try:
        with pytest.raises(llm_client.LLMConcurrencyTimeout):
            real_llm.generate_response(messages=[{"role": "user", "content": "hi"}])
    finally:
        llm_client._LLM_SLOTS.release()


# ---------------------------------------------------------------- env parsing
@pytest.mark.parametrize("raw, expected", [
    (None, 4), ("", 4), ("3", 3), ("1", 1),
    ("0", 4), ("-2", 4), ("nan", 4), ("inf", 4), ("2.5", 4), ("four", 4),
])
def test_max_concurrency_env_is_parsed_strictly(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("AIDUMEI_LLM_MAX_CONCURRENCY", raising=False)
    else:
        monkeypatch.setenv("AIDUMEI_LLM_MAX_CONCURRENCY", raw)
    assert llm_client.llm_gate_limit_from_env() == expected


def test_invalid_concurrency_env_is_reported(monkeypatch, caplog):
    from ducky.env_config import config_errors
    monkeypatch.setenv("AIDUMEI_LLM_MAX_CONCURRENCY", "nan")
    assert llm_client.llm_gate_limit_from_env() == llm_client.DEFAULT_LLM_MAX_CONCURRENCY
    assert "AIDUMEI_LLM_MAX_CONCURRENCY" in config_errors("AIDUMEI_LLM_MAX_CONCURRENCY")
    monkeypatch.setenv("AIDUMEI_LLM_MAX_CONCURRENCY", "3")
    assert llm_client.llm_gate_limit_from_env() == 3
    assert not config_errors("AIDUMEI_LLM_MAX_CONCURRENCY"), "a fixed value clears the error"


@pytest.mark.parametrize("raw, expected", [("30", 30.0), ("0", 120.0), ("nan", 120.0), ("x", 120.0)])
def test_slot_timeout_env_is_parsed_strictly(monkeypatch, raw, expected):
    monkeypatch.setenv("AIDUMEI_LLM_SLOT_TIMEOUT_SEC", raw)
    assert llm_client.llm_slot_timeout_from_env() == expected
