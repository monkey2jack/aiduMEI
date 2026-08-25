"""tests/test_v20_core_memory_staleness.py — v20 P0-4：核心记忆陈旧度要上运维面

用户视角审计 🔴-2：三块核心记忆的时间戳全部停在同一天，约 28 天没动过，而
`core_current_project` 里写的还是三个版本以前的项目状态。用户问「我们现在在做什么」，
拿回来的是一个月前的答案。

**这比「核心记忆失明」更隐蔽：东西在，只是旧的。** 失明会零召回，很快被发现；陈旧会
给出一个语气自信、内容过期的答案 —— 没有任何信号，没有任何日志，没有任何探针。

要紧的是：判据其实早就有了。`_is_stale()` 和 `inject_context()` 里的 ⚠️ 标注在 v18
就写好了 —— 但那条信息**只出现在注入给模型的上下文里**。运维面（`/health`）完全看不见。
也就是说，「核心记忆一个月没更新」此前只有人正好去读一次注入内容才会发现。
**判据存在，射程不到**（铁律 12 的又一例，这次不是判据写窄了，是判据没接到该看的人那里。）

本文件只守机制，不碰内容 —— 三块 block 的实际文字是部署方的私人资料。

跑法：cd <仓库根> && .venv/bin/pytest tests/test_v20_core_memory_staleness.py -v
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import ducky.utils as utils  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="aidumem_v20_cm_stale_")
_DB = os.path.join(_TMP, "facts.db")
utils.FACTS_DB = _DB


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(utils, "FACTS_DB", _DB)
    import ducky.core_memory as cm
    import ducky.memory_types as mt
    monkeypatch.setattr(mt, "_checked", False, raising=False)
    monkeypatch.setattr(cm, "_initialized", False)
    cm._initialized_scopes.clear()
    conn = sqlite3.connect(_DB)
    for t in ("core_memory", "memory_banks"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")
    conn.commit()
    conn.close()
    yield


def _age(days: float) -> str:
    return (datetime.now() - timedelta(days=days)).isoformat()


def _set_verified(block_key: str, ts: str):
    """直接改库里的 last_verified_at —— 探针读的就是这一列。"""
    conn = sqlite3.connect(_DB)
    conn.execute("UPDATE core_memory SET last_verified_at=?, updated_at=? WHERE block_key=?",
                 (ts, ts, block_key))
    n = conn.total_changes
    conn.commit()
    conn.close()
    assert n > 0, f"没有一行被改到（block_key={block_key}）—— 用例改的不是探针读的那一列"


# ═══════════════ ① 探针本体：新鲜 → false，陈旧 → true ═══════════════

def test_fresh_blocks_report_not_stale():
    """负向对照方向：刚写过的 block 不许被判超期。"""
    from ducky.core_memory import init_core_memory, put_block, staleness_status
    init_core_memory()
    put_block("core_user_profile", "今天刚写下的稳定身份信息占位内容")
    st = staleness_status()
    assert st["blocks"] >= 1
    assert st["stale"] is False, f"刚写完就被判超期：{st}"
    assert st["stale_blocks"] == 0
    assert st["oldest_age_days"] is not None and st["oldest_age_days"] < 1.0


def test_block_older_than_threshold_is_reported_stale():
    """★ 验收基准正向：注入一个超阈值的时间戳，探针必须报 stale。"""
    from ducky.core_memory import STALENESS_DAYS, init_core_memory, put_block, staleness_status
    init_core_memory()
    put_block("core_current_project", "三个版本以前的项目状态")
    _set_verified("core_current_project", _age(STALENESS_DAYS + 5))

    st = staleness_status()
    assert st["stale"] is True, f"超期 {STALENESS_DAYS + 5} 天却报不 stale：{st}"
    assert st["stale_blocks"] >= 1
    assert st["oldest_age_days"] >= STALENESS_DAYS, (
        f"最旧年龄 {st['oldest_age_days']} 天 < 阈值 {STALENESS_DAYS} —— 数字自相矛盾"
    )
    assert st["threshold_days"] == STALENESS_DAYS, "阈值必须与注入面用同一个常量"


def test_threshold_boundary_is_measured_not_assumed():
    """阈值两侧各测一次：刚过阈值 → stale，刚不到 → 不 stale。

    只测「远超阈值」的话，把判据从 `>` 写成 `>= 0` 也照样绿。

    v20.1 WP-D2 起阈值按块分级 —— 边界值从生效函数取，不再假设 30：
    core_key_decisions 是 semantic 档（默认 180 天），拿 30±1 测它等于
    测一个不存在的契约。
    """
    from ducky.core_memory import (
        init_core_memory, put_block, staleness_status, staleness_threshold_days,
    )
    init_core_memory()
    put_block("core_key_decisions", "关键决策与约定的占位内容一二三")
    threshold = staleness_threshold_days("core_key_decisions")

    _set_verified("core_key_decisions", _age(threshold - 1))
    assert staleness_status()["stale"] is False, "阈值内侧被判超期"

    _set_verified("core_key_decisions", _age(threshold + 1))
    assert staleness_status()["stale"] is True, "阈值外侧没被判超期"


def test_unparsable_timestamp_counts_as_stale_not_as_fresh():
    """★ 时间戳坏掉不许算「新鲜」。

    `_age_days()` 解析失败返回 None 而不是 0 —— 返回 0 会被读成「刚刚更新过」，
    一个坏时间戳就能把一块永久过期的记忆伪装成最新的。
    """
    from ducky.core_memory import init_core_memory, put_block, staleness_status
    init_core_memory()
    put_block("core_user_profile", "稳定身份信息的占位内容一二三四")
    _set_verified("core_user_profile", "这不是一个时间戳")

    st = staleness_status()
    assert st["stale"] is True, f"坏时间戳被当成新鲜：{st}"
    assert st["unparsable_blocks"] >= 1, (
        f"坏时间戳没被单独计数 —— 「时间戳坏了」和「真的很旧」是两件不同的事，"
        f"运维要能分开看：{st}"
    )


def test_placeholder_blocks_are_reported_unfilled_not_stale():
    """★「从来没填过」不许被报成「很旧」—— 变异轮抓出来的一条。

    我第一版写的是「空 block 不算超期」，然后把出厂状态一放就断言 `stale_blocks == 0`。
    它是绿的，但**绿得没有信息量**：`DEFAULT_BLOCKS` 播的是「（尚未填写）…」这样的
    占位文本，不是空串；而刚播种的东西时间戳是「现在」，所以无论判据怎么写都不 stale。
    把判据改成「所有块一律计入」的变异，那条用例照样绿 —— 它测的不是它名字说的事。

    真正要守的是：占位文本要被识别成 `unfilled`，而且**即使它很旧也不许变成 stale**。
    「去填」和「去核对」是两个不同的动作，合成一条告警等于两条都失去可行动性。
    """
    from ducky.core_memory import (DEFAULT_BLOCKS, STALENESS_DAYS,
                                   init_core_memory, staleness_status)
    init_core_memory()
    st = staleness_status()
    assert st["unfilled_blocks"] == len(DEFAULT_BLOCKS), (
        f"出厂占位块没被识别成 unfilled：{st}"
    )
    assert st["blocks"] == 0, f"占位块被当成已填写内容计入 blocks：{st}"
    assert st["stale_blocks"] == 0 and st["stale"] is False

    # 关键一步：把占位块的时间戳推到很旧 —— 它仍然只能是 unfilled，不能是 stale
    for key in DEFAULT_BLOCKS:
        _set_verified(key, _age(STALENESS_DAYS + 60))
    st2 = staleness_status()
    assert st2["unfilled_blocks"] == len(DEFAULT_BLOCKS), st2
    assert st2["stale"] is False, (
        f"一个从未填写的部署放了 90 天，被报成「核心记忆超期」：{st2} —— "
        "运维照这条告警去「核对内容」，而实际要做的是「先填上」"
    )
    assert st2["blocks"] == 0


# ═══════════════ ② /health 必须端出来并告警 ═══════════════

def test_health_exposes_the_staleness_probe_and_warns():
    """★ 判据落在 `/health` 的结构上：字段在、且三类异常各有一条**真的会执行**的告警。

    ⚠️ 第一版用的是字符串/正则判据，被变异轮打穿了：把分支改成
    `if False and cm["unfilled_blocks"]:`，`warnings.append(...)` 那段文本还在原地，
    正则照样命中，用例照样绿。**字符串判据看得见文本，看不见控制流。**

    改成 AST 级：要求存在一个 `if`，它的条件里引用了该字段，**并且**它的分支体里
    真的调用了 `warnings.append`。同时排除条件被常量假值短路的写法。
    """
    import ast
    src = open(os.path.join(_REPO_ROOT, "ducky/hot/health.py"), encoding="utf-8").read()
    for field in ("core_memory_stale", "core_memory_stale_blocks",
                  "core_memory_oldest_age_days", "core_memory_unfilled_blocks"):
        assert f'probes["{field}"]' in src, f"/health 没有暴露 {field}"

    tree = ast.parse(src)

    def _mentions(node, key: str) -> bool:
        for n in ast.walk(node):
            if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant) \
                    and n.slice.value == key:
                return True
        return False

    def _appends_warning(node) -> bool:
        for n in ast.walk(node):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr == "append" \
                    and isinstance(n.func.value, ast.Name) and n.func.value.id == "warnings":
                return True
        return False

    def _short_circuited(test) -> bool:
        """条件里挂着一个常量假值（`if False and X:` / `if X and False:`）。"""
        for n in ast.walk(test):
            if isinstance(n, ast.Constant) and n.value is False:
                return True
        return False

    for key in ("stale", "unfilled_blocks", "unparsable_blocks"):
        ok = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.If):
                continue
            if not _mentions(node.test, key):
                continue
            if _short_circuited(node.test):
                continue
            if _appends_warning(ast.Module(body=node.body, type_ignores=[])):
                ok = True
                break
        assert ok, (
            f"/health 没有一条「条件引用 cm[{key!r}]、分支体里 warnings.append」的活分支 —— "
            "字段要人主动去看才有用，warning 才会被推到眼前；"
            "而被常量假值短路掉的分支等于没有"
        )


def test_probe_failure_reports_none_not_false():
    """探针自己挂掉时 `/health` 必须报 None，不许报 False。

    False 的意思是「查过了，不陈旧」；None 的意思是「没查出来」。把后者渲染成前者，
    就是用一个绿灯掩盖一次探测失败 —— 静默失败铁律。
    """
    src = open(os.path.join(_REPO_ROOT, "ducky/hot/health.py"), encoding="utf-8").read()
    assert 'probes["core_memory_stale"] = None' in src, (
        "异常分支把 core_memory_stale 设成了 False 或干脆不设 —— "
        "「没查出来」和「查过了没问题」必须分得开"
    )
