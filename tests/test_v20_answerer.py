"""答题模型客户端的用例：路由、密钥、官方生成参数、重试语义。

全部离线——``urlopen`` 被替换掉，一个字节都不出网。跑分要花钱，
所以「管线本身对不对」必须在不花钱的前提下先证明。
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from benchmarks import answerer as A


# ---------- 打桩：假的 urlopen ----------

class _FakeResp:
    def __init__(self, payload: dict):
        self._buf = json.dumps(payload).encode()

    def read(self):
        return self._buf

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ok_payload(text="Paris", prompt_tokens=16, completion_tokens=3):
    return {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": prompt_tokens,
                  "completion_tokens": completion_tokens},
    }


def _http_error(code):
    return urllib.error.HTTPError(
        "https://example.invalid", code, "boom", {}, io.BytesIO(b"")
    )


@pytest.fixture
def offline(monkeypatch):
    """截断网络与退避睡眠；返回一个可编排的调用记录器。"""
    calls: list[dict] = []
    script: list = []

    def fake_urlopen(req, timeout=None):
        calls.append({
            "url": req.full_url,
            "headers": dict(req.headers),
            "body": json.loads(req.data.decode()),
            "timeout": timeout,
        })
        item = script.pop(0) if script else _ok_payload()
        if isinstance(item, Exception):
            raise item
        return _FakeResp(item)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(A.time, "sleep", lambda *_: None)
    monkeypatch.setenv("AIDUMEI_VOLINK_API_KEY", "volink-test-key")
    monkeypatch.setenv("AIDUMEI_9R_API_KEY", "niner-test-key")
    # 地址也从环境变量取（源码里不留字面量），离线用假域名。
    monkeypatch.setenv("AIDUMEI_VOLINK_BASE_URL", "https://relay.invalid/v1")
    monkeypatch.setenv("AIDUMEI_9R_BASE_URL", "https://niner.invalid/v1")
    return {"calls": calls, "script": script}


# ---------- 路由 ----------

def test_gpt系模型走中转_其余走9r():
    assert A.resolve("gpt-4o-by-openai").name == "volink"
    assert A.resolve("gpt-4-1-by-openai").name == "volink"
    assert A.resolve("qwen3.8-max").name == "9r"
    assert A.resolve("claude-opus-5").name == "9r"


def test_未登记模型直接报错_不做前缀兜底_负向对照():
    """负向对照：``gpt-`` 开头但未登记的模型**不许**被兜底到中转。

    走错网关＝烧错额度。宁可当场报错，也不要「聪明」地猜。
    """
    with pytest.raises(A.AnswerError) as e:
        A.resolve("gpt-4o-mini")          # 前缀像 volink，但没登记
    assert "没有登记路由" in str(e.value)
    with pytest.raises(A.AnswerError):
        A.resolve("qwen3.8-turbo")        # 前缀像 9r，同样不许兜底


def test_网关地址取自环境变量_并规范化尾斜杠(monkeypatch):
    monkeypatch.setenv("AIDUMEI_VOLINK_BASE_URL", "https://relay.invalid/v1/")
    monkeypatch.setenv("AIDUMEI_9R_BASE_URL", "https://niner.invalid/v1")
    assert A.VOLINK.base_url == "https://relay.invalid/v1"
    assert A.NINER.base_url == "https://niner.invalid/v1"
    for gw in set(A.ROUTES.values()):
        assert gw.base_url.startswith("https://")
        assert gw.base_url.endswith("/v1")


def test_地址两处都取不到就报错_而不是内置兜底(monkeypatch):
    """负向对照：缺配置必须当场报错。代码里若留了兜底字面量，这条会红。"""
    monkeypatch.delenv("AIDUMEI_9R_BASE_URL", raising=False)
    monkeypatch.setattr(A, "_keychain", lambda *a, **k: "")
    with pytest.raises(A.AnswerError) as e:
        _ = A.NINER.base_url
    assert "取不到" in str(e.value)
    assert "网关地址" in str(e.value)


def test_源码里不许出现任何网关地址字面量_负向对照():
    """铁律：私有 endpoint 不进仓。这条守着它别复发。

    仓库是要公开的，网关地址是运营方的私有资产，与密钥同级。
    """
    import pathlib
    import re

    src = pathlib.Path(A.__file__).read_text(encoding="utf-8")
    hits = re.findall(r"https?://[^\s\"')]+", src)
    assert hits == [], f"answerer.py 里出现了 {len(hits)} 处地址字面量"


# ---------- 密钥 ----------

def test_环境变量优先于钥匙串(monkeypatch):
    monkeypatch.setenv("AIDUMEI_9R_API_KEY", "from-env")

    def boom(*a, **k):                     # 钥匙串不该被碰
        raise AssertionError("环境变量已给出时不应再查钥匙串")

    monkeypatch.setattr(A.subprocess, "run", boom)
    m = A.AnswerModel("qwen3.8-max")
    assert m._key == "from-env"


def test_两处都取不到密钥就报错而不是空跑(monkeypatch):
    monkeypatch.delenv("AIDUMEI_9R_API_KEY", raising=False)

    class _R:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(A.subprocess, "run", lambda *a, **k: _R())
    with pytest.raises(A.AnswerError) as e:
        A.AnswerModel("qwen3.8-max")
    assert "取不到" in str(e.value)


def test_自述里没有密钥明文_只有指纹_负向对照(offline):
    m = A.AnswerModel("qwen3.8-max")
    d = m.describe()
    blob = json.dumps(d, ensure_ascii=False)
    assert "niner-test-key" not in blob          # 负向对照：明文必须不在
    assert len(d["key_sha256_16"]) == 16
    assert d["gateway"] == "9r"


# ---------- 官方生成参数 ----------

def test_生成参数锁死官方值(offline):
    m = A.AnswerModel("gpt-4o-by-openai")
    m.complete("hello")
    body = offline["calls"][0]["body"]
    assert body["temperature"] == 0          # 官方 gpt_utils.py L289
    assert body["max_tokens"] == 32          # 官方 num_tokens_request=32
    assert body["model"] == "gpt-4o-by-openai"
    assert body["messages"] == [{"role": "user", "content": "hello"}]


def test_默认值直接引用官方常量_而不是抄一遍数字():
    from benchmarks import locomo_official as L

    assert A.AnswerModel.__dataclass_fields__["temperature"].default is L.OFFICIAL_TEMPERATURE
    assert A.AnswerModel.__dataclass_fields__["max_tokens"].default is L.OFFICIAL_MAX_TOKENS


def test_请求打到正确的网关地址(offline):
    A.AnswerModel("gpt-4o-by-openai").complete("x")
    A.AnswerModel("qwen3.8-max").complete("x")
    assert offline["calls"][0]["url"] == A.VOLINK.base_url + "/chat/completions"
    assert offline["calls"][1]["url"] == A.NINER.base_url + "/chat/completions"


def test_必带浏览器UA否则被Cloudflare挡成1010(offline):
    """实测：缺 UA 一律 403 / ``error code: 1010``。这个头是必需品。"""
    A.AnswerModel("qwen3.8-max").complete("x")
    headers = {k.lower(): v for k, v in offline["calls"][0]["headers"].items()}
    assert "Mozilla/5.0" in headers["user-agent"]
    assert headers["authorization"] == "Bearer niner-test-key"


# ---------- 重试语义 ----------

def test_521先失败后成功_重试计数正确(offline):
    offline["script"].extend([_http_error(521), _http_error(521), _ok_payload("Paris")])
    m = A.AnswerModel("qwen3.8-max")
    assert m.complete("q") == "Paris"
    assert len(offline["calls"]) == 3
    assert m.usage() == {"calls": 1, "retries": 2,
                         "prompt_tokens": 16, "completion_tokens": 3}


def test_401不重试_配置错就当场炸(offline):
    """负向对照：鉴权失败重试多少次都一样，重试反而掩盖问题。"""
    offline["script"].extend([_http_error(401), _ok_payload("绝不该走到这")])
    with pytest.raises(A.AnswerError) as e:
        A.AnswerModel("qwen3.8-max").complete("q")
    assert "不重试" in str(e.value)
    assert len(offline["calls"]) == 1        # 只发了一次


def test_重试到上限仍失败就抛错_不返回空串(offline):
    offline["script"].extend([_http_error(521)] * 4)
    m = A.AnswerModel("qwen3.8-max", max_retries=4)
    with pytest.raises(A.AnswerError) as e:
        m.complete("q")
    assert "HTTP 521" in str(e.value)
    assert len(offline["calls"]) == 4
    assert m.calls == 0                      # 失败不计入成功调用


def test_网络异常也走重试(offline):
    offline["script"].extend([urllib.error.URLError("reset"), _ok_payload("ok")])
    assert A.AnswerModel("qwen3.8-max").complete("q") == "ok"


def test_空choices当错处理而不是当空答案(offline):
    offline["script"].append({"choices": [], "usage": {}})
    with pytest.raises(A.AnswerError) as e:
        A.AnswerModel("qwen3.8-max").complete("q")
    assert "空 choices" in str(e.value)


def test_content为null当错处理而不是当空答案_重试到上限抛错(offline):
    """推理模型的招牌翻车：reasoning_content 吃光预算，content 返回 null。

    这条路最毒——空串会被判「答错」摊进均值，而 answer_failures 仍是 0，
    成绩被静默压低却没有任何报错。必须抛错，不许悄悄返回空串。
    """
    null_content = {"choices": [{"message": {"content": None},
                                 "finish_reason": "length"}], "usage": {}}
    offline["script"].extend([null_content] * 4)
    m = A.AnswerModel("gemini-3.7-flash", max_retries=4)
    with pytest.raises(A.AnswerError) as e:
        m.complete("q")
    assert "content 为空" in str(e.value)
    assert "length" in str(e.value)          # finish_reason 要能诊断
    assert len(offline["calls"]) == 4        # 空是瞬时故障，重试过


def test_content为空白字符也当错处理(offline):
    """负向对照：strip 后为空和 null 一样毒，不许放过。"""
    offline["script"].extend([{"choices": [{"message": {"content": "  \n "}}],
                              "usage": {}}] * 4)
    with pytest.raises(A.AnswerError) as e:
        A.AnswerModel("gemini-3.7-flash", max_retries=4).complete("q")
    assert "content 为空" in str(e.value)


def test_空答案是瞬时的就重试捞回来_不误杀(offline):
    """正向对照：间歇性空答案（同参数时好时坏）重试后应拿到真答案。"""
    offline["script"].extend([
        {"choices": [{"message": {"content": None}}], "usage": {}},
        _ok_payload("Blue"),
    ])
    m = A.AnswerModel("gemini-3.7-flash")
    assert m.complete("q") == "Blue"
    assert m.retries == 1


def test_用量跨多次调用累计(offline):
    offline["script"].extend([
        _ok_payload("a", prompt_tokens=100, completion_tokens=5),
        _ok_payload("b", prompt_tokens=200, completion_tokens=7),
    ])
    m = A.AnswerModel("gpt-4o-by-openai")
    m.complete("1")
    m.complete("2")
    assert m.usage() == {"calls": 2, "retries": 0,
                         "prompt_tokens": 300, "completion_tokens": 12}
