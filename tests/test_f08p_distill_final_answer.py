"""f0.8+ C: session distill keeps a reasoning model's final answer, never its thinking.

Fixtures are the *shapes* recorded from a GPT-OSS-20B OpenAI-compatible gateway
(2026-11-13) with synthetic session text; no real memories.
"""
import json

import pytest

from ducky import llm_client, session_distill
from ducky.write_policy import extract_final_answer

THINK = ("We need to output one or two sentences summarizing the most memorable part of the "
         "conversation. Must be specific, point out people and events, under 80 Chinese characters. "
         "So maybe \"先上影子模式观察一周\". That's under 80. It")
ANSWER = "这一程定下了新版本先上影子模式、观察一周再拦截，上线前先备份。"

# max_tokens=200, default effort: thinking eats the budget; the gateway copies it into content.
SHAPE_TRUNCATED_ECHO = {"choices": [{"finish_reason": "length", "message": {
    "role": "assistant", "content": THINK, "reasoning": THINK[:180]}}]}
# reasoning_effort=low: short thinking in its own field, clean content.
SHAPE_LOW_EFFORT = {"choices": [{"finish_reason": "stop", "message": {
    "role": "assistant", "content": ANSWER, "reasoning": "Need one-two sentences, specific."}}]}
# Older gateways: reasoning_content field.
SHAPE_REASONING_CONTENT = {"choices": [{"finish_reason": "length", "message": {
    "role": "assistant", "content": "", "reasoning_content": THINK}}]}


class _Resp:
    def __init__(self, data, status=200):
        self.status_code = status
        self.text = json.dumps(data) if isinstance(data, dict) else data


@pytest.fixture()
def fake_llm(monkeypatch):
    from ducky.security import egress
    monkeypatch.setattr(egress, "guard_url", lambda url, purpose="egress": url)
    monkeypatch.setattr(llm_client, "get_llm_config",
                        lambda: {"api_key": "k", "model": "m", "base_url": "https://llm.invalid/v1"})
    from ducky import engine_mode
    monkeypatch.setattr(engine_mode, "cloud_egress_allowed", lambda *_a, **_k: True)
    calls = []

    def install(*responses):
        queue = list(responses)

        def post(url, json=None, **kw):
            calls.append(dict(json or {}))
            return queue.pop(0)
        monkeypatch.setattr(llm_client.requests, "post", post)
        return calls
    return install


# ── llm_client ───────────────────────────────────────────────────────────
def test_reasoning_echoed_into_content_is_not_an_answer():
    assert llm_client._parse_completion_body(json.dumps(SHAPE_TRUNCATED_ECHO)) is None
    assert llm_client._parse_completion_body(json.dumps(SHAPE_LOW_EFFORT)) == ANSWER


def test_truncated_reasoning_field_triggers_the_bigger_budget_retry(fake_llm):
    calls = fake_llm(_Resp(SHAPE_TRUNCATED_ECHO), _Resp(SHAPE_LOW_EFFORT))
    out = llm_client.call_llm("p", max_tokens=200, reasoning_effort="low")
    assert out == ANSWER
    assert [c["max_tokens"] for c in calls] == [200, 800]
    assert all(c.get("reasoning_effort") == "low" for c in calls)


def test_reasoning_content_shape_also_retries(fake_llm):
    calls = fake_llm(_Resp(SHAPE_REASONING_CONTENT), _Resp(SHAPE_LOW_EFFORT))
    assert llm_client.call_llm("p", max_tokens=100) == ANSWER
    assert len(calls) == 2 and "reasoning_effort" not in calls[0]


def test_gateway_rejecting_reasoning_effort_is_retried_without_it(fake_llm):
    calls = fake_llm(_Resp('{"error":"unknown parameter"}', status=400), _Resp(SHAPE_LOW_EFFORT))
    assert llm_client.call_llm("p", reasoning_effort="low") == ANSWER
    assert calls[0].get("reasoning_effort") == "low" and "reasoning_effort" not in calls[1]


def test_unparseable_body_is_logged_without_its_text(fake_llm, caplog):
    fake_llm(_Resp({"choices": [{"finish_reason": "stop", "message": {"content": ""}}]}))
    caplog.set_level("WARNING")
    assert llm_client.call_llm("p") is None
    assert "choices" not in caplog.text


# ── extract_final_answer ─────────────────────────────────────────────────
@pytest.mark.parametrize("raw", [
    THINK + "\n\n" + ANSWER,
    THINK + "\nFinal answer: " + ANSWER,
    THINK + "\n最终答案：" + ANSWER,
    THINK + '\n{"summary": "' + ANSWER + '"}',
    "<think>" + THINK + "</think>" + ANSWER,
    "analysis We need to output a sentence. assistantfinal " + ANSWER,
])
def test_final_answer_after_reasoning_is_kept(raw):
    answer, why = extract_final_answer(raw)
    assert why is None and answer == ANSWER


@pytest.mark.parametrize("raw", [
    THINK,                                      # truncated mid-thought
    THINK + "\nThe answer should be short.",    # English tail is still thinking
    "<think>" + THINK + "</think>",
])
def test_pure_reasoning_yields_no_answer(raw):
    answer, why = extract_final_answer(raw)
    assert answer == "" and why


def test_clean_answer_is_untouched():
    assert extract_final_answer(ANSWER) == (ANSWER, None)


# ── distill end to end ───────────────────────────────────────────────────
ROWS = [{"ref": f"r{i}", "text": t, "turn": i} for i, t in enumerate([
    "我们决定新版本先上影子模式，观察一周再拦截",
    "部署前必须先备份数据库并验证可以恢复",
    "说好了周五晚上一起复盘这次上线",
])]


@pytest.fixture()
def distill(monkeypatch):
    monkeypatch.setattr(session_distill, "collect_session_memories", lambda *a, **k: list(ROWS))
    monkeypatch.setattr(session_distill, "_latest_generation", lambda *a, **k: {})
    import ducky.core_memory as cm
    monkeypatch.setattr(cm, "autosink_from_distill", lambda *a, **k: {"status": "skipped"})
    seen = {}

    def run(llm_out):
        def fake(*a, **k):
            seen.update(k)
            return llm_out
        monkeypatch.setattr(llm_client, "call_llm", fake)
        return session_distill.distill_session("s-1", user_id="u", bank_id="default", force=True), seen
    return run


def test_distill_keeps_answer_after_reasoning_and_stores_no_thinking(distill):
    res, seen = distill(THINK + "\n\n" + ANSWER)
    assert res["mode"] == "llm" and res["summary"] == ANSWER
    assert "need to" not in json.dumps(res, ensure_ascii=False).lower()
    assert seen.get("reasoning_effort") == "medium"


def test_distill_falls_back_only_when_no_answer(distill):
    res, _ = distill(THINK)
    assert res["mode"] == "fallback"
    assert res["summary"] == session_distill._fallback_summary(ROWS)
    assert "need to" not in res["summary"].lower()


@pytest.mark.parametrize("env,expected", [("off", None), ("", None), ("medium", "medium"), ("bogus", "medium")])
def test_reasoning_effort_is_configurable(monkeypatch, env, expected):
    monkeypatch.setenv("AIDUMEI_DISTILL_REASONING_EFFORT", env)
    assert session_distill._distill_reasoning_effort() == expected
