"""
tests/test_v19_4_0_llm_sse.py — v19.4.0 审计修复 🔴-B 回归测试
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

生产审计 🔴-B：上游网关对 chat/completions 返回 Content-Type:
text/event-stream，body 是「完整 JSON + data: [DONE]」拼接体，
r.json() 抛异常 → 独立评估器永远记 evaluator_unavailable，
B1 治理管线的 LLM 评估形同虚设。修复：

1. 请求显式带 stream: False
2. 响应体改走 _parse_completion_body 兜底解析：
   标准 JSON / 拼接体 / 真 SSE delta 流 三种形态都能取出内容

本文件守住这三种形态 + 垃圾体降级 None + stream:False 请求守卫。
"""

import json
import os
import sys

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import ducky.llm_client as llm_client
from ducky.llm_client import _parse_completion_body, call_llm


# ─────────────────────────────────────────────────────────────
# 1. 响应体解析：三种形态
# ─────────────────────────────────────────────────────────────

def test_parse_standard_json():
    body = json.dumps({"choices": [{"message": {"content": "  标准响应  "}}]})
    assert _parse_completion_body(body) == "标准响应"


def test_parse_gateway_concatenated_body():
    """上游网关实测形态：完整 JSON + data: [DONE] 拼接，r.json() 会炸"""
    payload = {"choices": [{"message": {"content": '{"verdict": "approve", "confidence": 0.95, "reason": "ok"}'}}]}
    body = json.dumps(payload, ensure_ascii=False) + "\ndata: [DONE]"
    out = _parse_completion_body(body)
    assert out is not None
    assert '"verdict": "approve"' in out


def test_parse_true_sse_delta_stream():
    """真 SSE 流：多个 delta 块拼接成完整内容"""
    chunks = ["你", "好，", "这是", "流式回复"]
    lines = []
    for c in chunks:
        lines.append("data: " + json.dumps(
            {"choices": [{"delta": {"content": c}}]}, ensure_ascii=False))
    lines.append("data: [DONE]")
    body = "\n".join(lines)
    assert _parse_completion_body(body) == "你好，这是流式回复"


def test_parse_sse_with_full_message_events():
    """部分网关 SSE 里直接给 message 完整块"""
    body = (
        "data: " + json.dumps({"choices": [{"message": {"content": "整块返回"}}]})
        + "\ndata: [DONE]\n"
    )
    assert _parse_completion_body(body) == "整块返回"


def test_parse_garbage_returns_none():
    assert _parse_completion_body("") is None
    assert _parse_completion_body("这不是 JSON") is None
    assert _parse_completion_body("data: 垃圾\ndata: [DONE]") is None
    assert _parse_completion_body(json.dumps({"choices": []})) is None
    assert _parse_completion_body(json.dumps({"no_choices": 1})) is None


# ─────────────────────────────────────────────────────────────
# 2. call_llm 端到端：stream:False 守卫 + 拼接体兜底
# ─────────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text

    def json(self):
        # 模拟网关拼接体：r.json() 必炸，逼调用方走 text 兜底
        return json.loads(self.text)


@pytest.fixture
def _fake_llm_env(monkeypatch):
    monkeypatch.setattr(llm_client, "_config_cache", {
        "model": "test-model",
        "base_url": "http://fake.local/v1",
        "api_key": "fake-key",
    })
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None, **kw):
        captured["url"] = url
        captured["json"] = json
        payload = {"choices": [{"message": {"content": "评估结果"}}]}
        body = __import__("json").dumps(payload, ensure_ascii=False) + "\ndata: [DONE]"
        return _FakeResponse(200, body)

    monkeypatch.setattr(llm_client.requests, "post", fake_post)
    return captured


def test_call_llm_sends_stream_false(_fake_llm_env):
    out = call_llm("测试提示", system="测试系统")
    assert out == "评估结果"
    assert _fake_llm_env["json"]["stream"] is False, "请求必须显式 stream: False"
    assert _fake_llm_env["url"].endswith("/chat/completions")


def test_call_llm_survives_concatenated_body(_fake_llm_env):
    """拼接体（r.json() 会炸）也能取到内容——🔴-B 核心回归点"""
    assert call_llm("测试") == "评估结果"


def test_call_llm_unconfigured_returns_none(monkeypatch):
    monkeypatch.setattr(llm_client, "_config_cache", {
        "model": "", "base_url": "", "api_key": "",
    })
    assert call_llm("测试") is None


def test_call_llm_http_error_returns_none(monkeypatch):
    monkeypatch.setattr(llm_client, "_config_cache", {
        "model": "m", "base_url": "http://fake.local/v1", "api_key": "k",
    })
    monkeypatch.setattr(
        llm_client.requests, "post",
        lambda *a, **kw: _FakeResponse(500, "boom"),
    )
    assert call_llm("测试") is None


# ─────────────────────────────────────────────────────────────
# 3. 推理截断自动放大重试（v19.4.0 生产实测补强）
#    网关侧的推理模型：思考与输出共享 max_tokens，
#    请求级 reasoning_effort/enable_thinking 被网关无视。
#    小预算 → content 空 + finish_reason=length + reasoning_content 非空。
# ─────────────────────────────────────────────────────────────

def _truncated_body():
    return json.dumps({
        "choices": [{
            "finish_reason": "length",
            "message": {"role": "assistant", "content": "",
                        "reasoning_content": "思考中……"},
        }],
    }, ensure_ascii=False)


@pytest.fixture
def _cfg(monkeypatch):
    monkeypatch.setattr(llm_client, "_config_cache", {
        "model": "test-model",
        "base_url": "http://fake.local/v1",
        "api_key": "fake-key",
    })


def test_call_llm_reasoning_truncation_retries_with_bigger_budget(_cfg, monkeypatch):
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None, **kw):
        calls.append(json["max_tokens"])
        if json["max_tokens"] < 400:
            return _FakeResponse(200, _truncated_body())
        payload = {"choices": [{"message": {"content": "重试后的裁决"}}]}
        return _FakeResponse(200, __import__("json").dumps(payload, ensure_ascii=False))

    monkeypatch.setattr(llm_client.requests, "post", fake_post)
    assert call_llm("测试", max_tokens=200) == "重试后的裁决"
    assert calls == [200, 800], "应按 ×4 放大预算重试一次"


def test_call_llm_reasoning_retry_budget_capped_at_4096(_cfg, monkeypatch):
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None, **kw):
        calls.append(json["max_tokens"])
        return _FakeResponse(200, _truncated_body())

    monkeypatch.setattr(llm_client.requests, "post", fake_post)
    assert call_llm("测试", max_tokens=2000) is None
    assert calls == [2000, 4096], "重试预算封顶 4096"


def test_call_llm_garbage_body_no_retry(_cfg, monkeypatch):
    """非推理截断的垃圾体（无 finish_reason=length/reasoning_content）不触发重试"""
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None, **kw):
        calls.append(1)
        return _FakeResponse(200, "这不是 JSON")

    monkeypatch.setattr(llm_client.requests, "post", fake_post)
    assert call_llm("测试") is None
    assert len(calls) == 1, "普通解析失败不应重试"


def test_evaluator_uses_reasoning_safe_budget(monkeypatch):
    """🔴-B 补强：评估器首试预算必须 ≥512（推理模型思考耗预算）"""
    import ducky.governance as gov
    captured = {}

    def fake_call_llm(prompt, *, system="", max_tokens=1024,
                      temperature=0.3, timeout=45):
        captured["max_tokens"] = max_tokens
        captured["timeout"] = timeout
        return '{"verdict": "approve", "confidence": 0.9, "reason": "ok"}'

    monkeypatch.setattr("ducky.llm_client.call_llm", fake_call_llm)
    out = gov._llm_evaluate("偏好", "测试键", "测试值")
    assert out is not None and out["verdict"] == "approve"
    assert captured["max_tokens"] >= 512, "评估器预算过小会被推理思考耗尽"
    assert captured["timeout"] >= 30, "推理模型需要更宽裕的超时"
