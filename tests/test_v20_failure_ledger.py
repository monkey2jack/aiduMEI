"""tests/test_v20_failure_ledger.py — v20 P1-8：特性级失败要有人知道 + 资源占用要常驻

**P1-8（外部审计 M7 ／ 第三方审计低-6）**：宽捕获遍地。AST 普查实测（射程 `ducky/`
+ 三个服务入口）：**489 处**宽捕获 —— 重抛 53、纯 pass 20、有动作但零日志 79、
**只有 debug 152**、有 warning/error 184、只有 info 1。也就是 **251 处在生产默认
日志级别下等于无声**。

整改口径不是「全改」。多数无声是正当的：并发建表、向前兼容、时间戳解析兜底、
`health.py` 里把错误写进响应字段的那批 —— 全改会用噪声淹掉真信号。口径是**只改
特性级入口**：挂在写入／读取主链路上、失败后有**持久用户可见后果**的那些。

判据来自审计点名的前科（`ducky/self_edit.py:293` 自己写着）：每次 /add 都稳定抛
`TypeError`，被调用方 `except Exception` 收进一条 `logger.debug`，于是 LLM 语义级
去重**从未执行过一次**，日志上什么都看不出来。

**资源指标**：部署方指定为产品级指标。一个记忆引擎若内存单调上涨、fd 只增不减，
那不是「性能差」，是迟早会把用户的记忆一起带走。

跑法：cd <仓库根> && .venv/bin/pytest tests/test_v20_failure_ledger.py -v
"""
from __future__ import annotations

import ast
import logging
import os
import pathlib
import sys

import pytest

_REPO_ROOT = pathlib.Path(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ducky import failure_ledger as FL  # noqa: E402
from ducky import resource_probe as RP  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    FL.reset()
    yield
    FL.reset()


# ═══════════════ ① 账本语义 ═══════════════

def test_failure_is_counted_and_warned_not_swallowed(caplog):
    """★ 核心：一次特性级失败必须同时产生「一条 warning」和「计数 +1」。

    只计数不告警 → 要人主动去查才看得见；只告警不计数 → 事后查不到发生过几次。
    两者都要。
    """
    with caplog.at_level(logging.WARNING, logger="aiduMEM.failure"):
        FL.feature_failed("index_memory", ValueError("boom"))
    assert FL.snapshot()["by_feature"]["index_memory"] == 1
    msgs = [r.getMessage() for r in caplog.records]
    assert any("index_memory" in m for m in msgs), f"没点名是哪个特性：{msgs}"
    assert any("ValueError" in m for m in msgs), f"没带上异常类型，运维无从下手：{msgs}"


def test_warnings_are_rate_limited_but_never_go_fully_silent(caplog):
    """★ 限流：前 3 次一律 warning，之后每 100 次一条汇总 —— 但不许彻底沉默。

    「每秒失败一次」的特性不该刷满日志；但一个刷了一万次却一条日志都没有的特性，
    等于回到了整改前。两个极端都要避开，所以断言两侧都测。
    """
    with caplog.at_level(logging.WARNING, logger="aiduMEM.failure"):
        for _ in range(250):
            FL.feature_failed("rerank", RuntimeError("x"))
    n = len([r for r in caplog.records if "rerank" in r.getMessage()])
    assert FL.snapshot()["by_feature"]["rerank"] == 250, "计数没记全"
    assert n < 250, f"250 次失败打了 {n} 条 warning —— 日志会被刷满"
    assert n >= 3, f"250 次失败只打了 {n} 条 —— 限流限成了沉默"


def test_ledger_never_raises_even_on_garbage():
    """记账器挂在**降级路径**上：它把降级路径再带崩一层，比没有记账器糟得多。

    ⚠️ 变异轮抓到的：第一版喂的是 `None` 和一个字符串 —— 那两个**根本走不进异常
    分支**（`None` 可哈希、字符串的 `type().__name__` 也正常）。于是把
    `except: pass` 改成 `except: raise`，这条用例照样绿。**「垃圾输入」得真的能
    让它炸，否则测的是「正常路径没炸」，和这条断言想说的完全是两件事。**
    """
    class _Exploding(Exception):
        def __str__(self):
            raise RuntimeError("连字符串化都会炸的异常对象")

    # ① 不可哈希的特性名 → `_counts[feature]` 当场 TypeError
    FL.feature_failed(["unhashable"])            # type: ignore[arg-type]
    # ② 格式化时会炸的异常对象 → 走到 logger.warning 那一行才炸
    FL.feature_failed("boom_feature", _Exploding())

    # 两次都必须被吞掉；进程还活着、账本还能读，就是这条断言的全部要求
    assert isinstance(FL.snapshot()["total"], int)

    # 正向对照：确认这两个输入**真的**会炸 —— 否则上面又是一次空转
    import pytest as _pytest
    with _pytest.raises(TypeError):
        {}[["unhashable"]] = 1                   # 复刻 ① 的炸点
    with _pytest.raises(RuntimeError):
        str(_Exploding())                        # 复刻 ② 的炸点


def test_snapshot_reports_zero_not_none_when_nothing_failed():
    """这里 0 是有意义的（启动以来没失败过），与 http_error_rate 的「无流量」不同。"""
    s = FL.snapshot()
    assert s["total"] == 0 and s["by_feature"] == {}


# ═══════════════ ② 改动点确实落在特性级入口上 ═══════════════

_EXPECTED_FEATURES = {
    "index_memory", "unindex_memory", "store_verbatim", "self_edit",
    "salience_register", "memory_type_classify", "evolve_on_added",
    "rerank", "hybrid_search", "vision_usage_track",
    # v20 P0-6：核心记忆进检索索引。失败意味着那块记忆搜不到 ——
    # 正本还在（core_memory 表），但用户问起来会得到「没有相关记忆」。
    "core_memory_index",
}


def _ledger_calls():
    """全仓所有 `feature_failed(...)` 调用点：(文件, 行, 特性名, 是否在处理器内)。"""
    out = []
    for p in sorted((_REPO_ROOT / "ducky").rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        in_handler = set()
        for h in (n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)):
            for n in ast.walk(h):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                        and n.func.id == "feature_failed":
                    in_handler.add(n.lineno)
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                    and n.func.id == "feature_failed":
                feat = n.args[0].value if (n.args and isinstance(n.args[0], ast.Constant)) else None
                out.append((p.relative_to(_REPO_ROOT).as_posix(), n.lineno, feat,
                            n.lineno in in_handler))
    return out


def test_every_ledger_call_sits_inside_a_wide_except_handler():
    """★ 记账只许出现在宽捕获处理器里。

    出现在正常流程里就说明它被误用成了「普通日志」，而它的语义是
    「一件本该发生的事没有发生」—— 语义一泛化，`/health` 上那个数字就不再可信。
    """
    calls = _ledger_calls()
    assert calls, "全仓一个记账点都没有 —— P1-8 没落地"
    stray = [(f, ln, feat) for f, ln, feat, ok in calls if not ok]
    assert not stray, f"以下记账点不在 except 处理器内：{stray}"


def test_feature_names_are_from_the_agreed_set_and_are_business_names():
    """特性名要能对上业务，不许写成内部私有函数名（它会出现在 /health 里给运维看）。"""
    names = {feat for _, _, feat, _ in _ledger_calls() if feat}
    assert names, "记账点没有一个带字面量特性名"
    unknown = names - _EXPECTED_FEATURES
    assert not unknown, (
        f"出现了未登记的特性名 {sorted(unknown)} —— 新增特性级入口请同时更新本用例的"
        "_EXPECTED_FEATURES，让「有哪些特性会静默失败」这件事有一份可读的清单"
    )
    private = {n for n in names if n.startswith("_")}
    assert not private, f"特性名用了私有函数名 {sorted(private)}，运维读不懂"


def test_the_named_precedent_is_actually_covered():
    """★ 外部审计点名的那个前科必须真的被覆盖到。

    `self_edit_on_add` 的调用点就是「LLM 语义级去重从未执行过一次，而日志上什么都
    看不出来」的现场。整改如果绕过了它，等于整改没打到点上。
    """
    src = (_REPO_ROOT / "ducky/layer1_selfcheck.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    covered = False
    for tryn in (n for n in ast.walk(tree) if isinstance(n, ast.Try)):
        calls = {n.func.id for n in ast.walk(tryn)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        if "self_edit_on_add" not in calls:
            continue
        for h in tryn.handlers:
            if any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == "feature_failed" for n in ast.walk(h)):
                covered = True
    assert covered, (
        "layer1_selfcheck 里调用 self_edit_on_add 的那个 try 块，处理器里没有记账 —— "
        "外部审计点名的前科没被覆盖"
    )


def test_conversion_did_not_change_control_flow():
    """★ 记账只是**插入**，原有的 debug 行必须还在。

    观测器不许改变被观测者：降级路径的行为要逐字节不变，否则这次「加个探针」
    就变成了一次没人要求的行为变更。判据：每个含记账的处理器里，
    仍然有原来那条日志调用。
    """
    naked = []
    for p in sorted((_REPO_ROOT / "ducky").rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        for h in (n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)):
            has_ledger = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                             and n.func.id == "feature_failed" for n in ast.walk(h))
            if not has_ledger:
                continue
            has_log = any(
                isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in {"debug", "info", "warning", "error", "exception"}
                for n in ast.walk(h)
            )
            if not has_log:
                naked.append(f"{p.relative_to(_REPO_ROOT).as_posix()}:{h.lineno}")
    assert not naked, (
        "以下处理器只剩记账、原有日志被替换掉了：\n  " + "\n  ".join(naked)
        + "\n记账是插入，不是替换 —— 细节仍应留在原来那条日志里"
    )


def test_health_exposes_the_failure_ledger():
    src = (_REPO_ROOT / "ducky/hot/health.py").read_text(encoding="utf-8")
    assert 'probes["feature_failures"]' in src
    assert 'probes["feature_failures_by_name"]' in src
    assert 'probes["feature_failures"] = None' in src, (
        "探针自己挂掉时报了 0 —— 「没测出来」不许伪装成「没失败过」"
    )


# ═══════════════ ③ 资源指标 ═══════════════

def test_resource_snapshot_has_the_agreed_fields():
    s = RP.snapshot()
    for k in ("rss_mb", "max_rss_mb", "cpu_seconds", "threads", "open_fds", "pid"):
        assert k in s, f"资源画像缺字段 {k}"
    assert s["pid"] == os.getpid()
    assert isinstance(s["threads"], int) and s["threads"] >= 1


def test_current_rss_and_peak_rss_are_separate_fields():
    """★ 语义分离：当前占用与历史峰值绝不能是同一个字段。

    混成一个的后果有两种，都很坏：把峰值当现值 → 一次早已结束的尖峰永远挂在监控上；
    把现值当峰值 → 真尖峰完全看不见。
    """
    src = (_REPO_ROOT / "ducky/resource_probe.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "snapshot"), None)
    assert fn is not None
    keys = [k.value for n in ast.walk(fn) if isinstance(n, ast.Dict)
            for k in n.keys if isinstance(k, ast.Constant)]
    assert "rss_mb" in keys and "max_rss_mb" in keys, (
        f"当前 rss 与峰值 rss 没有分成两个字段：{keys}"
    )


def test_maxrss_unit_is_converted_per_platform():
    """★ `ru_maxrss` 的单位随平台变（Linux kB / macOS 字节），必须按平台换算。

    不换算的后果不是「数字不好看」，是在 macOS 上把 100MB 报成 100GB —— 那个数字
    看着就像内存泄漏，会让人去查一个不存在的故障（假红灯）。
    """
    src = (_REPO_ROOT / "ducky/resource_probe.py").read_text(encoding="utf-8")
    assert "darwin" in src, "没有按平台区分 ru_maxrss 的单位"
    v = RP.snapshot()["max_rss_mb"]
    assert v is None or 0.5 < v < 100_000, (
        f"峰值内存 {v} MB 明显不合理 —— 八成是单位没换算"
    )


def test_unmeasurable_fields_report_none_not_zero():
    """测不到的一律 None。`0` 的意思是「测了是零」，`None` 是「这个平台测不出来」。

    非 Linux 上 `rss_mb` 必然取不到 —— 那时它必须是 None。填 0 会让监控看到
    「常驻内存 0MB」这种既不可能又完全不像故障的值。
    """
    s = RP.snapshot()
    if not os.path.exists("/proc/self/status"):
        assert s["rss_mb"] is None, (
            f"本平台没有 /proc，rss_mb 却报了 {s['rss_mb']!r} —— 那是猜的"
        )


def test_health_exposes_resource_metrics():
    src = (_REPO_ROOT / "ducky/hot/health.py").read_text(encoding="utf-8")
    for f in ("process_rss_mb", "process_max_rss_mb", "process_cpu_seconds",
              "process_threads", "process_open_fds"):
        assert f'probes["{f}"]' in src, f"/health 没有暴露 {f}"
