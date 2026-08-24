"""tests/test_v20_http_error_rate.py — v20 P1-9 / P0-7：出错要看得见，活着也要看得见

两条同源缺陷，都是「探活是绿的，实际不对」：

**P1-9（外部审计 M12 ／ 用户视角审计四）**：`health.py` 里搜 `5xx|error_rate|alert`
只命中一行注释。`/health` 只探活，不探错误率。于是「195 次 500、持续 13 分钟」那次
事故**没有留下任何可复现的监控路径** —— 服务全程 active、探针全程 ok，而三分之一
的请求在报错。

**P0-7（用户视角审计六）**：`mem0_sync` 服务 active 8 小时，`journalctl` 只有启动
那 2 行，之后**零条目**。于是「在跑」和「启动就卡住」在运维面上完全无法区分 ——
沉默既是正常态也是故障态，同一个信号。

跑法：cd <仓库根> && .venv/bin/pytest tests/test_v20_http_error_rate.py -v
"""
from __future__ import annotations

import ast
import os
import sys

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from ducky import http_metrics as H  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    H.reset()
    yield
    H.reset()


# ═══════════════ ① 计数器语义 ═══════════════

def test_empty_window_reports_none_not_zero():
    """★ 窗口内没有请求时错误率必须是 None，不许是 0.0。

    `0.0` 的意思是「有流量且没出错」；`None` 的意思是「没有流量，无从判断」。
    把后者渲染成前者，就是拿一个绿灯掩盖「服务其实没人用」—— 而那恰恰是事故的
    常见形态之一（上游全挂、本服务闲着、一切"正常"）。
    """
    snap = H.snapshot()
    assert snap["total"] == 0
    assert snap["error_rate_5m"] is None, (
        f"空窗口报了 {snap['error_rate_5m']!r} —— 「没流量」被渲染成「没出错」"
    )


def test_only_5xx_counts_toward_the_error_rate():
    """4xx 不进错误率（会稀释「服务端是否在出错」的含义），但要单独暴露。"""
    for c in (200, 200, 401, 404, 500):
        H.record(c)
    s = H.snapshot()
    assert s["total"] == 5
    assert s["server_errors"] == 1 and s["client_errors"] == 2
    assert s["error_rate_5m"] == 0.2, f"错误率把 4xx 算进去了：{s}"


def test_the_incident_shape_is_visible():
    """★ 复刻事故形态：195 次 500 混在流量里，错误率必须非零且可读。"""
    for _ in range(195):
        H.record(500)
    for _ in range(400):
        H.record(200)
    s = H.snapshot()
    assert s["error_rate_5m"] is not None and s["error_rate_5m"] > 0.3, (
        f"195/595 的 5xx 占比没能被看见：{s}"
    )


def test_samples_outside_the_window_are_dropped():
    """滑动窗口要真的滑动 —— 否则错误率会永远记着一次早已结束的事故。"""
    import time
    now = time.time()
    H.record(500, now=now - H.WINDOW_S - 10)   # 窗口外
    H.record(200, now=now)                     # 窗口内
    s = H.snapshot(now=now)
    assert s["total"] == 1 and s["server_errors"] == 0, (
        f"窗口外的老样本没被剔除：{s} —— 错误率会永远挂着一次早已结束的事故"
    )


def test_recorder_never_raises_on_garbage():
    """计数器挂在每个请求的路径上：它绝不许把主链路带崩。"""
    H.record("not an int")       # type: ignore[arg-type]
    H.record(None)              # type: ignore[arg-type]
    assert H.snapshot()["total"] == 0, "垃圾样本被记进去了"


def test_window_constant_and_field_name_agree():
    """★ 字段名叫 `_5m`，窗口就必须真的是 5 分钟（宣称即承诺）。"""
    assert H.WINDOW_S == 300, (
        f"WINDOW_S={H.WINDOW_S} 而字段名是 http_error_rate_5m —— 字段名开始说谎了"
    )


# ═══════════════ ② 接进 middleware 与 /health ═══════════════

def test_middleware_records_every_outcome_including_401_and_500():
    """★ 判据落在源码结构上：middleware 必须记录**并且**在异常路径上也记。

    未处理异常按 500 记入后再抛 —— 否则「打挂了」这一类会从统计里凭空消失，
    而那是最需要被看见的一类。
    """
    src = open(os.path.join(_REPO_ROOT, "api_server.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.AsyncFunctionDef) and n.name == "_record_http_outcome"), None)
    assert fn is not None, "api_server 里没有 _record_http_outcome middleware"

    handlers = [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)]
    assert handlers, "middleware 没有异常分支 —— 打挂的请求不会被计入"
    records_in_except = any(
        isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) and c.func.attr == "record"
        for h in handlers for c in ast.walk(h)
    )
    assert records_in_except, "异常分支里没有 record() —— 500 会从统计里凭空消失"
    reraises = any(isinstance(n, ast.Raise) for h in handlers for n in ast.walk(h))
    assert reraises, "异常分支吞掉了异常 —— 观测器不许改变主链路行为"


def test_health_exposes_the_error_rate_and_warns_when_nonzero():
    src = open(os.path.join(_REPO_ROOT, "ducky/hot/health.py"), encoding="utf-8").read()
    for f in ("http_error_rate_5m", "http_requests_5m",
              "http_server_errors_5m", "http_client_errors_5m"):
        assert f'probes["{f}"]' in src, f"/health 没有暴露 {f}"

    tree = ast.parse(src)
    ok = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
        if "rate" not in names:
            continue
        if any(isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
               and c.func.attr == "append"
               and isinstance(c.func.value, ast.Name) and c.func.value.id == "warnings"
               for c in ast.walk(node)):
            ok = True
    assert ok, "/health 暴露了错误率但非零时不告警 —— 字段要人主动去看才有用"

    assert 'probes["http_error_rate_5m"] = None' in src, (
        "探针自己挂掉时报了 0.0 或干脆不报 —— 「没测出来」不许伪装成「没出错」"
    )


# ═══════════════ ③ P0-7 · mem0_sync 心跳 ═══════════════

def test_sync_daemon_emits_a_heartbeat_even_with_no_changes():
    """★ 无变更也要打心跳 —— 「在跑」和「启动就卡住」必须分得开。

    判据落在语法树上：`daemon_loop` 里必须有一条**不在 inotify 事件分支内**的
    定时日志，且带一个递增计数（计数停住 = 循环卡住，这是唯一能看出卡死的信号）。
    """
    src = open(os.path.join(_REPO_ROOT, "mem0_sync.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "daemon_loop"), None)
    assert fn is not None, "daemon_loop 不见了"

    # 心跳计数变量必须存在且被自增
    incremented = any(
        isinstance(n, ast.AugAssign) and isinstance(n.target, ast.Name)
        and "heartbeat" in n.target.id.lower()
        for n in ast.walk(fn)
    )
    assert incremented, (
        "心跳没有递增计数 —— 只打一句「我还在」无法区分「循环在转」和"
        "「同一条日志被卡在原地重复」"
    )

    # 心跳日志必须是 info，**而且必须在循环体内**。
    #
    # ⚠️ 变异轮抓到的：第一版只在整个函数里找「带『心跳』二字的 logger.info」，
    # 结果被启动那行 `logger.info("=== mem0_sync daemon 启动 === 心跳间隔 %ds")`
    # 满足了 —— 把循环里那条真心跳降级成 debug，用例照样绿。启动日志只证明它
    # 启动过，恰恰不能证明它还在转，而后者才是这条整改的全部意义。
    whiles = [n for n in ast.walk(fn) if isinstance(n, ast.While)]
    assert whiles, "daemon_loop 里没有 while 循环"
    in_loop_info = [
        n for w in whiles for n in ast.walk(w)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "info"
        and any(isinstance(a, ast.Constant) and isinstance(a.value, str)
                and "心跳" in a.value for a in n.args)
    ]
    assert in_loop_info, (
        "循环体内没有 logger.info 级别的心跳 —— 生产默认日志级别下 debug 等于不存在，"
        "这条整改就退化成了源码里的一句注释。"
        "（注意：启动时那行 info 不算 —— 它只证明启动过，不证明还在转）"
    )
