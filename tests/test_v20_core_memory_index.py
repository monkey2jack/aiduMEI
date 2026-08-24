"""tests/test_v20_core_memory_index.py — v20 P0-6：核心记忆必须搜得到

用户视角审计 🟡-3 实测：`ducky/core_memory.py` 全文 **0 处**向量／嵌入调用（唯一的
`.add(` 是 Python set，不是向量库）。核心记忆块只经由 `inject_context()` 直接拼进
上下文，**从不进入任何检索索引** —— 于是 `/search` 永远搜不到它们。

后果不是「少了一个功能」，是**两套互不相通的系统**：用户问一句话能不能得到答案，
取决于他那句话恰好走了哪条路（hook 注入 or 显式检索），而这两条路的边界，
用户完全无从得知。审计原话：「用户问什么只能靠运气。」

**本次整改的范围（部署方明确划定）**：机制做全，**存量不回填**。
`backfill_core_index()` 存在但不会被自动调用 —— 回填等于把已有核心记忆内容写进
检索索引，那是一次改变「什么可被搜到」边界的动作，属于数据决策，不是代码决策。

跑法：cd <仓库根> && .venv/bin/pytest tests/test_v20_core_memory_index.py -v
"""
from __future__ import annotations

import ast
import os
import sqlite3
import sys
import tempfile

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import ducky.utils as utils  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="aidumem_v20_cmidx_")
_DB = os.path.join(_TMP, "facts.db")
_FTS = os.path.join(_TMP, "text_fts.db")
utils.FACTS_DB = _DB
utils.TEXT_FTS_DB = _FTS


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(utils, "FACTS_DB", _DB)
    monkeypatch.setattr(utils, "TEXT_FTS_DB", _FTS)
    import ducky.core_memory as cm
    import ducky.memory_types as mt
    import ducky.text_fts as tf
    monkeypatch.setattr(mt, "_checked", False, raising=False)
    monkeypatch.setattr(cm, "_initialized", False)
    cm._initialized_scopes.clear()
    monkeypatch.setattr(tf, "TEXT_FTS_DB", _FTS, raising=False)
    for path, tables in ((_DB, ("core_memory", "memory_banks")), (_FTS, ("memories",))):
        conn = sqlite3.connect(path)
        for t in tables:
            conn.execute(f"DROP TABLE IF EXISTS {t}")
        conn.commit(); conn.close()
    # FTS 建表：drop 之后必须重建，否则 _index_memory 会撞上 no such table，
    # 而那个报错和「核心记忆没进索引」在断言里长得一模一样（假红灯）
    from ducky.text_fts import _init_text_fts
    _init_text_fts()
    yield


def _fts_rows():
    """直接读 FTS 侧的行 —— 判据落在索引里真有没有这条，不落在返回值上。"""
    import ducky.text_fts as tf
    conn = tf.get_text_conn()
    try:
        return conn.execute(
            "SELECT id, content, category FROM memories"
        ).fetchall()
    finally:
        conn.close()


# ═══════════════ ① 写入即入索引 ═══════════════

def test_writing_a_core_block_puts_it_into_the_search_index():
    """★ 核心断言：`put_block` 之后，这块内容必须真的躺在检索索引里。

    整改前这条必然红 —— 核心记忆和索引是两套系统。
    """
    from ducky.core_memory import (CORE_INDEX_CATEGORY, core_index_id,
                                   init_core_memory, put_block)
    init_core_memory()
    put_block("core_current_project", "当前正在做 v20 的可观测性整改与召回质量")

    rows = _fts_rows()
    hit = [r for r in rows if core_index_id("core_current_project") in str(r[0])]
    assert hit, (
        f"核心记忆写完没进索引 —— /search 永远搜不到它。索引里现有：{[r[0] for r in rows]}"
    )
    assert "可观测性整改" in hit[0][1], f"进索引的内容不对：{hit[0][1]!r}"
    assert hit[0][2] == CORE_INDEX_CATEGORY, (
        f"没打上 {CORE_INDEX_CATEGORY} 标记 —— 召回结果无法自证「这条来自核心记忆」，"
        "也就无法单独降权"
    )


def test_rewriting_a_block_overwrites_instead_of_piling_up():
    """★ 同一块改十次，索引里只许有一条。

    核心记忆是**可覆盖**的。若用随机 id，索引里会堆十条内容各异的同名块，
    召回时随机命中其中一条旧的 —— 那比搜不到更糟：它会给出一个曾经正确的答案。
    """
    from ducky.core_memory import core_index_id, init_core_memory, put_block
    init_core_memory()
    for i in range(5):
        put_block("core_user_profile", f"第 {i} 版的稳定身份信息内容占位")

    key = core_index_id("core_user_profile")
    rows = [r for r in _fts_rows() if key in str(r[0])]
    assert len(rows) == 1, f"同一块在索引里堆了 {len(rows)} 条：{[r[1][:20] for r in rows]}"
    assert "第 4 版" in rows[0][1], f"留下的不是最新那版：{rows[0][1]!r}"


def test_index_failure_never_breaks_the_write():
    """★ 索引挂了，写入必须照样成功 —— 正本在 core_memory 表里，索引只是副本。

    但失败要留痕（走 P1-8 的账本），否则又是一次「绿灯亮着、活没干」。
    """
    import ducky.core_memory as cm
    from ducky import failure_ledger as FL
    from ducky.core_memory import init_core_memory, put_block
    init_core_memory()
    FL.reset()

    import ducky.text_fts as tf
    orig = tf._index_memory
    tf._index_memory = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("索引挂了"))
    try:
        out = put_block("core_key_decisions", "关键决策与约定的内容占位一二三")
    finally:
        tf._index_memory = orig

    assert out and out["status"] == "ok", "索引失败把正常写入也弄挂了"
    # 正本必须还在
    from ducky.core_memory import get_block
    assert "关键决策" in (get_block("core_key_decisions") or {}).get("content", "")
    # 失败要留痕
    assert FL.snapshot()["by_feature"].get("core_memory_index", 0) >= 1, (
        f"索引失败没进特性账本 —— 「搜不到」这件事会静默持续："
        f"{FL.snapshot()}"
    )


# ═══════════════ ② 回填是显式动作，不许自动发生 ═══════════════

def test_backfill_exists_but_is_never_called_automatically():
    """★ 部署方划定的范围：机制先上，**存量不回填**。

    回填等于把已有核心记忆内容写进检索索引 —— 那是一次改变「什么可被搜到」边界的
    动作，属于数据决策。判据用 AST：全仓除了它自己的定义和测试，不许有人调用它。
    """
    import pathlib
    root = pathlib.Path(_REPO_ROOT)
    callers = []
    for p in sorted(root.rglob("*.py")):
        if any(x in p.parts for x in (".venv", "__pycache__", "tests")):
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                    and n.func.id == "backfill_core_index":
                callers.append(f"{p.relative_to(root).as_posix()}:{n.lineno}")
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                    and n.func.attr == "backfill_core_index":
                callers.append(f"{p.relative_to(root).as_posix()}:{n.lineno}")
    assert not callers, (
        "backfill_core_index 被自动调用了：\n  " + "\n  ".join(callers)
        + "\n它必须只在被显式调用时才动 —— 回填是数据决策，不是代码决策。"
    )


def test_backfill_when_invoked_skips_placeholders_and_reports_per_block():
    """显式调用时：占位文本不进索引，且逐块报告结果（不许只给一个总数）。

    搜到一条「（尚未填写）用户的稳定身份信息」对用户毫无价值，还会挤掉真结果。
    """
    from ducky.core_memory import (DEFAULT_BLOCKS, backfill_core_index,
                                   init_core_memory, put_block)
    init_core_memory()
    put_block("core_user_profile", "真正填写过的身份信息内容占位")

    rep = backfill_core_index()
    assert set(rep) == {"indexed", "skipped", "failed"}, f"报告字段不对：{rep}"
    assert "core_user_profile" in rep["indexed"], rep
    assert len(rep["skipped"]) == len(DEFAULT_BLOCKS) - 1, (
        f"占位块没被跳过：{rep}"
    )
    assert not rep["failed"], rep


# ═══════════════ ③ 负向对照：不许什么都能召回 ═══════════════

def test_placeholder_blocks_never_enter_the_index():
    """★ 负向对照：全新部署（三块全是占位文本）索引里必须一条都没有。

    没有这条，「写入即入索引」那条断言可以靠「什么都往索引里塞」通过 —— 那样
    `/search` 会开始返回「（尚未填写）…」，比搜不到更糟。
    """
    from ducky.core_memory import backfill_core_index, init_core_memory
    init_core_memory()
    rep = backfill_core_index()
    assert rep["indexed"] == [], f"占位文本被塞进了索引：{rep}"
    core_rows = [r for r in _fts_rows() if str(r[0]).startswith("core::")
                 or "core::" in str(r[0])]
    assert not core_rows, f"索引里有占位核心块：{[r[1][:24] for r in core_rows]}"
