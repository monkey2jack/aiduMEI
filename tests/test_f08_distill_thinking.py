"""f0.8 ②: a reasoning model's chain-of-thought never becomes a session summary."""
import json

import pytest

from ducky import llm_client, session_distill

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

    def run(llm_out):
        monkeypatch.setattr(llm_client, "call_llm", lambda *a, **k: llm_out)
        return session_distill.distill_session("s-1", user_id="u", bank_id="default", force=True)
    return run


@pytest.mark.parametrize("out", [
    "We need to output one or two sentences summarizing the session in Chinese, under 80 characters.",
    "Let me think. The user wants the key decision. We should output it in Chinese.",
    "<think>The user wants a summary of the session.</think>",
    "analysis<|message|>We need to summarize.<|end|>",
])
def test_thinking_output_falls_back_to_degraded_summary(distill, out):
    res = distill(out)
    assert res["status"] == "ok" and res["mode"] == "fallback"
    assert res["metadata"]["distill_mode"] == "fallback"
    assert res["metadata"]["distill_fallback_reason"].startswith("llm_reasoning_output:")
    assert res["summary"] == session_distill._fallback_summary(ROWS)
    assert "need to" not in res["summary"].lower()


def test_think_wrapper_is_stripped_and_the_answer_kept(distill):
    res = distill("<think>We need to output one sentence.</think>这一程定下了先上影子模式、观察一周再拦截。")
    assert res["mode"] == "llm" and res["summary"] == "这一程定下了先上影子模式、观察一周再拦截。"
    assert "distill_fallback_reason" not in res["metadata"]


def test_harmony_final_channel_is_kept(distill):
    res = distill("analysis We need to output a sentence. assistantfinal 这一程定下了上线前先备份。")
    assert res["mode"] == "llm" and res["summary"] == "这一程定下了上线前先备份。"


def test_normal_chinese_summary_is_untouched(distill):
    res = distill("这一程最值得记住的是：说好了周五一起复盘上线。")
    assert res["mode"] == "llm" and res["summary"] == "这一程最值得记住的是：说好了周五一起复盘上线。"


def _body(message, finish="stop"):
    return json.dumps({"choices": [{"message": message, "finish_reason": finish}]})


def test_llm_client_never_returns_reasoning_as_content():
    assert llm_client._parse_completion_body(_body({"content": "<think>We need to x</think>答案。"})) == "答案。"
    assert llm_client._parse_completion_body(_body({"content": "<think>only thinking"})) is None
    assert llm_client._parse_completion_body(_body({"content": None, "reasoning_content": "We need to..."})) is None
    assert llm_client._parse_completion_body(_body({"content": "", "reasoning": "We need to..."})) is None
    assert llm_client._parse_completion_body(_body({"content": "普通回答"})) == "普通回答"
