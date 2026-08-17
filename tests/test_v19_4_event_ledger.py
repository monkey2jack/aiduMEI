"""
tests/test_v19_4_event_ledger.py — v19.4.0 Mímir 借鉴 B5 事件溯源账本回归测试

覆盖内容（对照实施计划书验收标准）：
1. memory_events 建表幂等
2. record_event 在调用方事务内记录（不自行 commit）
3. 任意记忆变更史可查（get_history 按时间正序）
4. 事务一致性：模拟中途失败回滚，账本与事实同时回滚（同生共死）
5. content_hash 稳定性与空内容处理
6. 写入路径挂钩存在性守卫（/facts/add、cascade_delete、tombstone 三处）
"""

import os
import sys
import tempfile

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_tmp_dir = tempfile.mkdtemp(prefix="aidumem_b5_test_")
_TEST_FACTS_DB = os.path.join(_tmp_dir, "facts.db")
_TEST_TEXT_DB = os.path.join(_tmp_dir, "text_fts.db")

import ducky.utils as utils
utils.FACTS_DB = _TEST_FACTS_DB
utils.TEXT_FTS_DB = _TEST_TEXT_DB

from ducky.event_ledger import (
    content_hash,
    ensure_ledger_schema,
    get_history,
    record_event,
)


@pytest.fixture(autouse=True)
def _setup_test_env():
    utils.FACTS_DB = _TEST_FACTS_DB
    utils.TEXT_FTS_DB = _TEST_TEXT_DB
    ensure_ledger_schema()
    conn = utils.get_facts_conn()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL DEFAULT 'general',
            fact_key TEXT NOT NULL,
            fact_value TEXT NOT NULL,
            source TEXT DEFAULT 'default'
        )"""
    )
    conn.commit()
    yield


# ─────────────────────────────────────────────────────────────
# 1. 建表幂等
# ─────────────────────────────────────────────────────────────

def test_schema_idempotent():
    ensure_ledger_schema()
    ensure_ledger_schema()
    conn = utils.get_facts_conn()
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_events'"
    ).fetchone()
    assert row is not None


# ─────────────────────────────────────────────────────────────
# 2. 记录事件 + 变更史可查
# ─────────────────────────────────────────────────────────────

def test_record_and_history():
    conn = utils.get_facts_conn()
    e1 = record_event(conn, actor="agent_a", action="add", target_id="fact:coffee",
                      reason="category=偏好", after_hash=content_hash("热拿铁"))
    e2 = record_event(conn, actor="tool", action="update", target_id="fact:coffee",
                      reason="self-edit", before_hash=content_hash("热拿铁"),
                      after_hash=content_hash("冰美式"))
    conn.commit()
    assert e1 is not None and e2 is not None

    hist = get_history("fact:coffee")
    assert len(hist) == 2
    # 按时间正序：先 add 后 update
    assert hist[0]["action"] == "add" and hist[0]["actor"] == "agent_a"
    assert hist[1]["action"] == "update"
    assert hist[1]["reason"] == "self-edit"
    assert hist[0]["after_hash"] == hist[1]["before_hash"]  # 变更链可衔接


def test_history_empty_for_unknown():
    assert get_history("fact:nonexistent") == []
    assert get_history("") == []


def test_record_empty_target_returns_none():
    conn = utils.get_facts_conn()
    assert record_event(conn, actor="x", action="add", target_id="") is None


# ─────────────────────────────────────────────────────────────
# 3. 事务一致性（验收核心：账本与事实同生共死）
# ─────────────────────────────────────────────────────────────

def test_transaction_atomicity_rollback():
    """模拟中途失败：事实写入 + 账本记录后回滚，两者应同时消失"""
    conn = utils.get_facts_conn()
    try:
        conn.execute(
            "INSERT INTO facts (category, fact_key, fact_value, source) VALUES (?,?,?,?)",
            ("测试", "fact:atomic", "原子性测试", "tenant_x"),
        )
        record_event(conn, actor="tenant_x", action="add", target_id="fact:atomic",
                     after_hash=content_hash("原子性测试"))
        # 模拟中途失败 → 回滚
        conn.rollback()
    finally:
        pass

    # 事实没了
    row = conn.execute("SELECT COUNT(*) AS n FROM facts WHERE fact_key='fact:atomic'").fetchone()
    assert row["n"] == 0
    # 账本也没了（同生共死，不能先改事实后补账）
    assert get_history("fact:atomic") == []


def test_transaction_atomicity_commit():
    """对照组：正常提交时，事实与账本同时落盘"""
    conn = utils.get_facts_conn()
    conn.execute(
        "INSERT INTO facts (category, fact_key, fact_value, source) VALUES (?,?,?,?)",
        ("测试", "fact:committed", "提交测试", "tenant_x"),
    )
    record_event(conn, actor="tenant_x", action="add", target_id="fact:committed",
                 after_hash=content_hash("提交测试"))
    conn.commit()

    row = conn.execute("SELECT COUNT(*) AS n FROM facts WHERE fact_key='fact:committed'").fetchone()
    assert row["n"] == 1
    assert len(get_history("fact:committed")) == 1


# ─────────────────────────────────────────────────────────────
# 4. content_hash
# ─────────────────────────────────────────────────────────────

def test_content_hash_stable_and_empty():
    assert content_hash("热拿铁") == content_hash("热拿铁")
    assert content_hash("热拿铁") != content_hash("冰美式")
    assert content_hash("") == ""
    assert content_hash(None) == ""
    assert content_hash("   ") == ""


# ─────────────────────────────────────────────────────────────
# 5. 写入路径挂钩存在性守卫
# ─────────────────────────────────────────────────────────────

def test_ledger_hooks_present_in_write_paths():
    """防误删钩子：三处写入路径必须调用 record_event"""
    for rel in (
        os.path.join("ducky", "hot", "legacy_routes.py"),  # /facts/add
        os.path.join("ducky", "wal_engine.py"),            # cascade_delete
        os.path.join("ducky", "tombstone.py"),             # tombstone/restore
    ):
        path = os.path.join(_REPO_ROOT, *rel.split(os.sep))
        with open(path, encoding="utf-8") as f:
            src = f.read()
        assert "record_event" in src, f"{rel} 的事件账本钩子不见了"
