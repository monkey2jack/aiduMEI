"""
tests/test_v19_4_tombstone.py — v19.4.0 Mímir 借鉴 B3 tombstone 遗忘层回归测试

覆盖内容（对照实施计划书验收标准）：
1. tombstones 建表幂等
2. 删除前快照：全文 + 结构化行 + 理由留痕
3. 遗忘后检索不返回（物理删除照常）+ 数据库内可查全文与理由
4. 一键恢复：facts 回插 + FTS 重建，恢复后可再搜到
5. 租户硬隔离：A 的 tombstone B 查不到、恢复不了
6. 重复恢复拒绝（restored_at 已落则不再恢复）
7. cascade_delete_memory 挂钩存在性守卫（快照钩子不得被误删）
"""

import os
import sys
import tempfile

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_tmp_dir = tempfile.mkdtemp(prefix="aidumem_b3_test_")
_TEST_FACTS_DB = os.path.join(_tmp_dir, "facts.db")
_TEST_TEXT_DB = os.path.join(_tmp_dir, "text_fts.db")

import ducky.utils as utils
utils.FACTS_DB = _TEST_FACTS_DB
utils.TEXT_FTS_DB = _TEST_TEXT_DB

from ducky.tombstone import (
    ensure_tombstone_schema,
    list_tombstones,
    restore_tombstone,
    snapshot_before_delete,
)
from ducky.text_fts import _ensure_trigram_fts, _index_memory, get_text_conn


@pytest.fixture(autouse=True)
def _setup_test_env():
    utils.FACTS_DB = _TEST_FACTS_DB
    utils.TEXT_FTS_DB = _TEST_TEXT_DB
    ensure_tombstone_schema()
    # text_fts.db 的 memories 表 + trigram FTS（测试环境需显式初始化）
    tconn = get_text_conn()
    _ensure_trigram_fts(tconn)
    tconn.commit()
    # 确保 facts 表存在（schema_bootstrap 的 DDL 在测试环境未必跑过）
    conn = utils.get_facts_conn()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL DEFAULT 'general',
            fact_key TEXT NOT NULL,
            fact_value TEXT NOT NULL,
            source TEXT DEFAULT 'default',
            agent_id TEXT DEFAULT 'default',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    conn.commit()
    yield


def _seed_memory(memory_id: str, content: str, user_id: str = "tenant_a"):
    """造一条同时存在于 facts.db 与 FTS 的记忆"""
    conn = utils.get_facts_conn()
    conn.execute(
        "INSERT INTO facts (category, fact_key, fact_value, source, agent_id) VALUES (?,?,?,?,?)",
        ("偏好", f"fact:{memory_id}", content, user_id, user_id),
    )
    conn.commit()
    _index_memory(memory_id, content, user_id=user_id)


# ─────────────────────────────────────────────────────────────
# 1. 建表幂等
# ─────────────────────────────────────────────────────────────

def test_schema_idempotent():
    ensure_tombstone_schema()
    ensure_tombstone_schema()
    conn = utils.get_facts_conn()
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='tombstones'"
    ).fetchone()
    assert row is not None


# ─────────────────────────────────────────────────────────────
# 2. 删除前快照留痕
# ─────────────────────────────────────────────────────────────

def test_snapshot_captures_content_and_reason():
    _seed_memory("mem-001", "他喜欢喝热拿铁不加糖")
    tid = snapshot_before_delete("mem-001", user_id="tenant_a", reason="用户要求遗忘", actor="agent_a")
    assert tid is not None
    rows = list_tombstones("tenant_a")
    assert len(rows) == 1
    assert rows[0]["target_id"] == "mem-001"
    assert "热拿铁" in rows[0]["content_snapshot"]
    assert rows[0]["reason"] == "用户要求遗忘"
    assert rows[0]["actor"] == "agent_a"
    assert rows[0]["restored_at"] is None


def test_snapshot_empty_memory_returns_none():
    """无结构化内容的记忆不留快照（返回 None，不阻断删除）"""
    tid = snapshot_before_delete("mem-ghost", user_id="tenant_a")
    assert tid is None


def test_snapshot_blank_id_returns_none():
    assert snapshot_before_delete("", user_id="tenant_a") is None
    assert snapshot_before_delete("   ", user_id="tenant_a") is None


# ─────────────────────────────────────────────────────────────
# 3. 遗忘后检索不返回 + 全文理由可查
# ─────────────────────────────────────────────────────────────

def test_forget_then_search_returns_nothing_but_tombstone_holds_full_text():
    _seed_memory("mem-002", "团队定过的铁律：备份只进持久目录")
    tid = snapshot_before_delete("mem-002", user_id="tenant_a", reason="铁律已废止")
    assert tid is not None

    # 模拟物理删除（cascade_delete_memory 的 FTS/facts 清理动作）
    tconn = get_text_conn()
    tconn.execute("DELETE FROM memories WHERE id=?", ("mem-002",))
    tconn.commit()
    conn = utils.get_facts_conn()
    conn.execute("DELETE FROM facts WHERE fact_key=?", ("fact:mem-002",))
    conn.commit()

    # 活动检索不再返回
    row = tconn.execute("SELECT content FROM memories WHERE id=?", ("mem-002",)).fetchone()
    assert row is None
    # 但 tombstone 里全文与理由可查
    rows = list_tombstones("tenant_a")
    hit = [r for r in rows if r["target_id"] == "mem-002"]
    assert len(hit) == 1
    assert "备份只进持久目录" in hit[0]["content_snapshot"]
    assert hit[0]["reason"] == "铁律已废止"


# ─────────────────────────────────────────────────────────────
# 4. 一键恢复
# ─────────────────────────────────────────────────────────────

def test_restore_brings_memory_back_searchable():
    _seed_memory("mem-003", "他答应过每周复盘一次")
    tid = snapshot_before_delete("mem-003", user_id="tenant_a", reason="误删")
    # 物理删除
    tconn = get_text_conn()
    tconn.execute("DELETE FROM memories WHERE id=?", ("mem-003",))
    tconn.commit()
    conn = utils.get_facts_conn()
    conn.execute("DELETE FROM facts WHERE fact_key=?", ("fact:mem-003",))
    conn.commit()

    res = restore_tombstone(tid, user_id="tenant_a")
    assert res["restored"] is True
    assert res["target_id"] == "mem-003"
    assert "facts" in res["detail"] and "fts" in res["detail"]

    # 恢复后 FTS 能再搜到
    row = tconn.execute("SELECT content FROM memories WHERE id=?", ("mem-003",)).fetchone()
    assert row is not None and "每周复盘" in row["content"]
    # facts 也回插了
    frow = conn.execute("SELECT fact_value FROM facts WHERE fact_key=?", ("fact:mem-003",)).fetchone()
    assert frow is not None and "每周复盘" in frow["fact_value"]


def test_restore_twice_refused():
    _seed_memory("mem-004", "只能恢复一次")
    tid = snapshot_before_delete("mem-004", user_id="tenant_a")
    assert restore_tombstone(tid, user_id="tenant_a")["restored"] is True
    second = restore_tombstone(tid, user_id="tenant_a")
    assert second["restored"] is False


# ─────────────────────────────────────────────────────────────
# 5. 租户硬隔离
# ─────────────────────────────────────────────────────────────

def test_tenant_isolation():
    _seed_memory("mem-005", "租户 A 的秘密", user_id="tenant_a")
    tid = snapshot_before_delete("mem-005", user_id="tenant_a", reason="隔离测试")

    # B 看不到 A 的 tombstone
    assert list_tombstones("tenant_b") == []
    # B 恢复不了 A 的 tombstone
    res = restore_tombstone(tid, user_id="tenant_b")
    assert res["restored"] is False


# ─────────────────────────────────────────────────────────────
# 6. cascade_delete_memory 挂钩存在性守卫
# ─────────────────────────────────────────────────────────────

def test_snapshot_hook_present_in_wal_engine():
    """防误删钩子：cascade_delete_memory 必须调用 snapshot_before_delete"""
    wal_path = os.path.join(_REPO_ROOT, "ducky", "wal_engine.py")
    with open(wal_path, encoding="utf-8") as f:
        src = f.read()
    assert "snapshot_before_delete" in src, "wal_engine 的 tombstone 快照钩子不见了"
