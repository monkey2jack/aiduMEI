"""
tests/test_v19_4_0_ledger_target.py — v19.4.0 审计修复 🟡-C 回归测试
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

生产审计 🟡-C：事件账本 target_id 三种形态并存
（fact:{key} / fact:{id} / 裸 memory_id），get_history 精确匹配，
查一条记忆的完整变更史得猜当初记的是哪种形态。
修复：get_history 别名展开——`fact:X` 与裸 `X` 互为别名，
数字 X 额外展开 `fact:{X}`，一个参数查全链。
"""

import os
import sys
import tempfile

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_tmp_dir = tempfile.mkdtemp(prefix="aidumem_v1941_ledger_")

import ducky.utils as utils
utils.FACTS_DB = os.path.join(_tmp_dir, "facts.db")
utils.TEXT_FTS_DB = os.path.join(_tmp_dir, "text_fts.db")

from ducky.event_ledger import (
    _target_aliases,
    ensure_ledger_schema,
    get_history,
    record_event,
)


@pytest.fixture(autouse=True)
def _setup_test_env():
    utils.FACTS_DB = os.path.join(_tmp_dir, "facts.db")
    utils.TEXT_FTS_DB = os.path.join(_tmp_dir, "text_fts.db")
    ensure_ledger_schema()
    yield


# ─────────────────────────────────────────────────────────────
# 1. 别名展开单元
# ─────────────────────────────────────────────────────────────

def test_aliases_fact_prefix_and_bare():
    assert set(_target_aliases("fact:coffee")) == {"fact:coffee", "coffee"}
    assert set(_target_aliases("coffee")) == {"fact:coffee", "coffee"}


def test_aliases_numeric_expands_fact_id():
    assert "fact:42" in _target_aliases("42")
    assert "42" in _target_aliases("42")
    assert set(_target_aliases("fact:42")) == {"fact:42", "42"}


def test_aliases_empty():
    assert _target_aliases("") == []
    assert _target_aliases("   ") == []


# ─────────────────────────────────────────────────────────────
# 2. 一个参数查全链（核心回归点）
# ─────────────────────────────────────────────────────────────

def test_history_cross_form_query():
    """同一事实：add 记 fact:key、opinion 记 fact:id、delete 记裸 id——
    任一形态都能查到全链"""
    conn = utils.get_facts_conn()
    record_event(conn, actor="agent_a", action="add", target_id="fact:tea")
    record_event(conn, actor="system", action="opinion_set", target_id="fact:tea")
    record_event(conn, actor="agent_a", action="delete", target_id="tea")
    conn.commit()
    conn.close()

    for query in ("fact:tea", "tea"):
        hist = get_history(query)
        assert len(hist) == 3, f"用 {query!r} 查不到全链"
        assert [e["action"] for e in hist] == ["add", "opinion_set", "delete"]


def test_history_numeric_id_forms():
    """数字 id：fact:42 与 42 互通"""
    conn = utils.get_facts_conn()
    record_event(conn, actor="system", action="opinion_set", target_id="fact:42")
    record_event(conn, actor="agent_a", action="tombstone", target_id="42")
    conn.commit()
    conn.close()

    assert len(get_history("42")) == 2
    assert len(get_history("fact:42")) == 2


def test_history_no_cross_contamination():
    """别名展开不误伤：coffee 的历史不混入 coffeex"""
    conn = utils.get_facts_conn()
    record_event(conn, actor="a", action="add", target_id="fact:coffee")
    record_event(conn, actor="a", action="add", target_id="fact:coffeex")
    conn.commit()
    conn.close()
    assert len(get_history("coffee")) == 1
    assert len(get_history("coffeex")) == 1


def test_history_limit_still_applies():
    conn = utils.get_facts_conn()
    for i in range(5):
        record_event(conn, actor="a", action="update", target_id="fact:busy")
    conn.commit()
    conn.close()
    assert len(get_history("busy", limit=3)) == 3
