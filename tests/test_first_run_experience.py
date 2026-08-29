"""零配置首跑体验 —— **评委第一印象就在这几个响应里**（参赛前自查 WP-C）。

这组用例的由来：拿干净克隆按 README 走一遍，不填任何 key，第一个动作
（`POST /add` 写一条记忆）换来的是 HTTP 500 加一句 httpx 内部错误。
那句话是真的，但对拿到项目的人**没有用**。

判据一律落在**响应形状与可操作性**上，不落在措辞上 —— 措辞会改，
「不许把内部异常原文当成唯一内容」这条契约不该跟着改。
"""

import ast
import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent


# ── N-1：依赖未就绪时说人话 ───────────────────────────────────────────

@pytest.mark.parametrize("raw,expect_cause", [
    ("Using SOCKS proxy, but the 'socksio' package is not installed", "代理"),
    ("Incorrect API key provided: sk-***", "凭据"),
    ("Connection refused", "不可达"),
    ("some totally unknown failure", "初始化"),
])
def test_dependency_not_ready_message_is_actionable(raw, expect_cause):
    """四类失败都要给出**缺什么**，而不是只回抛内部异常。"""
    from ducky.mem0_runtime import _mem0_unavailable_detail

    msg = _mem0_unavailable_detail(Exception(raw))
    assert expect_cause in msg, f"没说清病因（期望提到「{expect_cause}」）：{msg[:120]}"
    # 三件事缺一不可：去哪配 / 怎么查 / 不配怎么办
    assert "mem0_config_local.json" in msg, "没说去哪配"
    assert "/health" in msg, "没说怎么查还缺什么"
    assert "/add/raw" in msg, (
        "没告诉他「不配 key 也有一条路能走」—— 那是这个项目零凭据下"
        "唯一还能用的入口，第一印象全靠它"
    )
    assert raw[:20] in msg, "原始错误必须保留（运维要靠它定位），只是不该是唯一内容"


def test_dependency_not_ready_is_503_not_500():
    """依赖未就绪是 **503**，不是 500。

    500 会让调用方以为撞上了 bug 去提 issue；503 才是「先去把依赖配好」。
    源码级判据：那一处 raise 必须是 503，且不能再把裸异常拼进 detail。
    """
    src = (_ROOT / "ducky" / "mem0_runtime.py").read_text(encoding="utf-8")
    assert 'raise HTTPException(503, _mem0_unavailable_detail(e))' in src, (
        "mem0 初始化失败的出口变了 —— 要么状态码退回 500，要么绕开了翻译函数"
    )
    assert 'HTTPException(500, f"mem0 不可用: {e}")' not in src, (
        "裸异常回抛的老写法又回来了"
    )


# ── N-2：degraded 与 degraded_details 必须同源 ─────────────────────────

def test_degraded_details_explains_every_degraded_entry():
    """`degraded` 里的每一项，`degraded_details` 都必须有一条对应。

    零配置首跑实测到的形态是：`degraded=['vector_backend','entity_keywords']`
    而 `degraded_details=None` —— **明细通道恰好在最需要它的时候是空的**。
    """
    from ducky.hot.health import _reconcile_degraded_details

    degraded = ["vector_backend", "entity_keywords", "mystery"]
    probes = {"vector_backend_error": "no credentials configured"}
    out = _reconcile_degraded_details(degraded, probes)

    assert {d["component"] for d in out} == set(degraded), (
        f"details 覆盖的组件与 degraded 对不上：{[d['component'] for d in out]}"
    )
    by = {d["component"]: d for d in out}
    assert by["vector_backend"]["reason"] == "no credentials configured"
    assert by["vector_backend"]["source"] == "probe"
    # 没有理由的那一条也必须说出「没有理由」，而不是留空让人猜
    assert by["mystery"]["reason"], "没有理由的降级也要有一句话，不能是空的"
    assert by["mystery"]["source"] == "probe_no_reason"


def test_degraded_details_survives_a_broken_tracker(monkeypatch):
    """明细来源坏掉时，/health 不许被带崩 —— 降级信息本身不该是新的故障源。"""
    from ducky.hot import health as H

    class _Boom:
        @staticmethod
        def get_degraded_details():
            raise RuntimeError("tracker 挂了（模拟）")

    monkeypatch.setattr(H, "DegradationTracker", _Boom)
    out = H._reconcile_degraded_details(["vector_backend"], {})
    assert [d["component"] for d in out] == ["vector_backend"], (
        "追踪器抛异常时应当回落到探针理由，而不是整个 /health 崩掉"
    )


# ── 首跑契约的元守卫 ─────────────────────────────────────────────────

def test_no_route_hands_a_bare_exception_to_the_caller():
    """**元守卫**：不许再有 `HTTPException(5xx, f"...{e}")` 这种把裸异常当正文的写法。

    这条抓的是「下一个同类」：N-1 不是孤例，它是一类写法。
    判据用 AST，不用 grep —— 注释里写着这种字样的地方不该被算进来
    （本仓在 v20.2.5 已经为「grep 分不清代码和注释」付过一次学费）。

    豁免：`detail` 里除了异常还给了可操作信息的（长度超过阈值、或包含路径
    /端点提示）不算 —— 判据要认的是「**只有**裸异常」。
    """
    offenders = []
    for path in sorted((_ROOT / "ducky").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name != "HTTPException" or len(node.args) < 2:
                continue
            code = node.args[0]
            if not (isinstance(code, ast.Constant) and isinstance(code.value, int)
                    and 500 <= code.value < 600):
                continue
            detail = node.args[1]
            text = ast.unparse(detail)
            # 只有裸异常（f"{e}" / str(e) / e 本身），没有任何指引
            bare = re.fullmatch(r"""f?['"]?\{?(str\()?e(xc)?\)?\}?['"]?""", text.strip()) \
                or re.fullmatch(r"""f['"][^'"]{0,24}\{(str\()?e(xc)?\)?\}['"]""", text.strip())
            if bare:
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        f"这些 5xx 出口把裸异常当成了全部正文：{offenders} —— "
        "调用方拿到的是内部实现细节，不是「他该做什么」。"
        "至少要说清缺什么、去哪配、有没有替代路径（见 N-1）。"
    )
