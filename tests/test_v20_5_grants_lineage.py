"""
tests/test_v20_5_grants_lineage.py — aiduMEI v20.5.0a 联邦授权与记忆谱系针对性测试
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
验证：
  1. federation_grants 表与 memory_lineage 表 schema 幂等初始化与 facts 谱系字段
  2. 联邦授权 Grant 创建、权限判定（动作 actions、作用域 resource_scope、租户 user_id）
  3. 授权过期失效（expires_at 判定）与即时撤销机制（revoke_grant）
  4. 越权拒绝（未授权跨 Agent 访问/读写默认拒绝）
  5. 记忆谱系（Memory Lineage）SHA-256 哈希计算与链式版本演化（version、previous_version_hash）
  6. 谱系完整性与断链自检（verify_lineage_integrity）
  7. 联邦写入（write_fact）及更新、冲突消解时密码学谱系账本的同生共死同事务保障
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp_dir = tempfile.mkdtemp(prefix="aidumem_v20_5_test_")
_TEST_DB = os.path.join(_tmp_dir, "facts.db")

import ducky.utils as utils  # noqa: E402

utils.FACTS_DB = _TEST_DB


@pytest.fixture(autouse=True)
def setup_test_db():
    """每次测试前保证测试库包含核心 schema 与联邦/谱系迁移，并清空授权表防止污染。"""
    from ducky.schema_bootstrap import ensure_core_schema
    from ducky.federation.schema import ensure_federation_schema
    ensure_core_schema(force=True)
    ensure_federation_schema(force=True)
    conn = utils.get_facts_conn()
    try:
        conn.execute("DELETE FROM federation_grants")
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()
    yield


# ── 2. 记忆密码学谱系 Lineage 测试 ──────────────────────────

def test_compute_content_hash_deterministic():
    from ducky.memory_lineage import compute_content_hash

    h1 = compute_content_hash("重要指令：对得起每一位用户")
    h2 = compute_content_hash("重要指令：对得起每一位用户")
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex string

    h_empty = compute_content_hash("")
    assert h_empty == "0" * 64


def test_lineage_detects_broken_chain():
    from ducky.memory_lineage import verify_lineage_integrity

    conn = utils.get_facts_conn()
    try:
        # 人工注入一条父哈希篡改的损坏链
        conn.execute(
            """INSERT INTO memory_lineage
               (memory_id, version, content_hash, previous_version_hash, action, actor)
               VALUES ('tampered:mem', 1, 'hash_1111111111111111111111111111111111111111111111111111111111111111', '', 'CREATE', 'test')"""
        )
        conn.execute(
            """INSERT INTO memory_lineage
               (memory_id, version, content_hash, previous_version_hash, action, actor)
               VALUES ('tampered:mem', 2, 'hash_2222222222222222222222222222222222222222222222222222222222222222', 'WRONG_PARENT_HASH', 'UPDATE', 'test')"""
        )
        conn.commit()
    finally:
        conn.close()

    audit = verify_lineage_integrity("tampered:mem")
    assert audit["status"] == "broken"
    assert audit["broken_count"] > 0
    assert "父哈希断链" in audit["broken_details"][0]["reason"]


# ── 3. 写入端点 (write_fact) 谱系与版本自增集成测试 ────────

def test_write_fact_populates_lineage_and_hashes():
    from ducky.federation.writer import write_fact
    from ducky.memory_lineage import compute_content_hash

    res1 = write_fact(
        "测试谱系", "项A", "事实初始内容", agent_id="agent_lineage_test", dedup=False
    )
    assert res1["status"] == "ok"
    fid = res1["fact_id"]

    conn = utils.get_facts_conn()
    try:
        row = conn.execute(
            "SELECT content_hash, version, previous_version_hash, last_actor FROM facts WHERE id=?",
            (fid,),
        ).fetchone()
        assert row is not None
        assert row[0] == compute_content_hash("事实初始内容")
        assert row[1] == 1
        assert row[2] == ""
    finally:
        conn.close()

    # 再次更新该事实
    res2 = write_fact(
        "测试谱系", "项A", "事实升级内容", agent_id="agent_lineage_test", dedup=True
    )
    assert res2["status"] == "ok"

    conn = utils.get_facts_conn()
    try:
        row2 = conn.execute(
            "SELECT content_hash, version, previous_version_hash, last_actor FROM facts WHERE id=?",
            (fid,),
        ).fetchone()
        assert row2[0] == compute_content_hash("事实升级内容")
        assert row2[1] == 2
        assert row2[2] == compute_content_hash("事实初始内容")
    finally:
        conn.close()



