"""
tests/test_v20_5_0_backfill.py — v20.5.0 正式版 存量行谱系基线守卫
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
用户审计 🟡-2 整改守卫：v4 迁移只补列不回填 → 存量行 content_hash 全空，
首次修改产生「假创世块」。v5 迁移必须：
  1. 回填存量行 content_hash（= 当前内容哈希，如实声明历史不可追）
  2. 为无链存量行补 BACKFILL 基线 v1
  3. 已有链的行不重复补基线
  4. 迁移后 verify 全绿；基线行再修改时链从 v1 基线正常续长
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp_dir = tempfile.mkdtemp(prefix="aidumem_v20_5_0_backfill_")
_TEST_DB = os.path.join(_tmp_dir, "facts.db")

import ducky.utils as utils  # noqa: E402

utils.FACTS_DB = _TEST_DB


@pytest.fixture(autouse=True)
def setup_test_db():
    # 每条用例一个**全新**库文件：线程本地连接池按路径缓存，复用同一路径
    # 会让打开中的连接撞上「文件被删后重建」的 inode 错位（disk I/O error），
    # 全新路径则根本不会命中旧缓存项
    fd, db_path = tempfile.mkstemp(prefix="facts_", suffix=".db", dir=_tmp_dir)
    os.close(fd)
    utils.FACTS_DB = db_path
    from ducky.schema_bootstrap import ensure_core_schema
    from ducky.federation.schema import ensure_federation_schema
    ensure_core_schema(force=True)
    ensure_federation_schema(force=True)
    # 模拟存量 v4 老库：有行、content_hash 为空、无谱系链、user_version=4
    conn = utils.get_facts_conn()
    try:
        conn.execute(
            "INSERT INTO facts (category, fact_key, fact_value, agent_id, content_hash, version) "
            "VALUES ('old', 'legacy_key', '原始老内容', 'dudu', '', 1)")
        conn.execute("PRAGMA user_version = 4")
        conn.commit()
    finally:
        conn.close()
    yield


def test_v5_migration_backfills_hash_and_baseline_chain():
    from ducky.schema_bootstrap import apply_migrations, CURRENT_SCHEMA_VERSION
    from ducky.memory_lineage import compute_content_hash, verify_lineage_integrity

    conn = utils.get_facts_conn()
    apply_migrations(conn)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION == 5
        row = conn.execute(
            "SELECT id, content_hash, version FROM facts WHERE fact_key='legacy_key'").fetchone()
        assert row[1] == compute_content_hash("原始老内容"), "存量行必须回填当前内容哈希"
        assert row[2] == 1

        chain = conn.execute(
            "SELECT version, content_hash, action, diff_summary FROM memory_lineage WHERE memory_id=?",
            (f"fact:{row[0]}",)).fetchall()
        assert len(chain) == 1
        assert chain[0][0] == 1 and chain[0][2] == "BACKFILL"
        assert chain[0][1] == row[1], "基线哈希必须等于迁移时点内容"
        assert "不可追溯" in chain[0][3], "基线必须如实声明历史不可追"
    finally:
        conn.close()

    res = verify_lineage_integrity()
    assert res["status"] == "ok", res["broken_details"]


def test_v5_migration_skips_rows_with_existing_chain():
    """已有链的行不补基线（链就是它的历史）。"""
    from ducky.memory_lineage import compute_content_hash
    conn = utils.get_facts_conn()
    try:
        rid = conn.execute("SELECT id FROM facts WHERE fact_key='legacy_key'").fetchone()[0]
        conn.execute(
            "INSERT INTO memory_lineage (memory_id, version, content_hash, action, actor) "
            "VALUES (?, 1, ?, 'CREATE', 'dudu')",
            (f"fact:{rid}", compute_content_hash("原始老内容")))
        conn.commit()
    finally:
        conn.close()

    from ducky.schema_bootstrap import apply_migrations
    conn = utils.get_facts_conn()
    apply_migrations(conn)
    try:
        rows = conn.execute(
            "SELECT action FROM memory_lineage WHERE memory_id=? ORDER BY version",
            (f"fact:{rid}",)).fetchall()
        assert [r[0] for r in rows] == ["CREATE"], "已有链不得再补 BACKFILL"
        assert conn.execute("SELECT content_hash FROM facts WHERE id=?",
                            (rid,)).fetchone()[0] != ""
    finally:
        conn.close()


def test_backfilled_row_evolves_normally_after_migration():
    """基线行再修改：链从 v1 基线续 v2，verify 全绿——不再是假创世块。"""
    from ducky.schema_bootstrap import apply_migrations

    conn = utils.get_facts_conn()
    apply_migrations(conn)
    conn.close()

    from ducky.federation.writer import write_fact
    r = write_fact("old", "legacy_key", "修订后的新内容", agent_id="dudu",
                   user_id="default", bank_id="default", dedup=False)
    assert r["status"] == "ok"
    assert r["action"] == "update"

    from ducky.memory_lineage import get_memory_lineage, verify_lineage_integrity
    chain = get_memory_lineage(f"fact:{r['fact_id']}")
    assert [c["version"] for c in chain] == [1, 2]
    assert chain[0]["action"] == "BACKFILL"
    assert chain[1]["action"] == "UPDATE"
    assert chain[1]["previous_version_hash"] == chain[0]["content_hash"]

    res = verify_lineage_integrity()
    assert res["status"] == "ok", res["broken_details"]
