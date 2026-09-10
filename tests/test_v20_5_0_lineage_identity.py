"""
tests/test_v20_5_0_lineage_identity.py — v20.5.0 正式版 谱系身份与对账守卫
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
用户审计 🔴-1 整改守卫：upsert 冲突命中时 lastrowid 不是被更新行的 id，
曾导致谱系串链/分叉/幽灵链。本文件把缺陷形态钉成回归测试：

  1. 同一 fact_key 反复写 → 只有一条链，版本 1→2→3 连续
  2. 两条 key 交错写 → 各记各链，互不串链
  3. verify_lineage_integrity 必须报出：幽灵链 / 链尾哈希不符 / fact:<key> 非法形态
  4. DELETE/FORGET 终链语义：行在→broken；行删→ok
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp_dir = tempfile.mkdtemp(prefix="aidumem_v20_5_0_lineage_")
_TEST_DB = os.path.join(_tmp_dir, "facts.db")

import ducky.utils as utils  # noqa: E402

utils.FACTS_DB = _TEST_DB


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch):
    # 全量合跑时其他测试模块可能已把 FACTS_DB 改走它们的临时库，
    # 每条用例前显式指回本文件的库（与 test_v20_federation_bank_scope 同款防御）
    utils.FACTS_DB = _TEST_DB
    # legacy_helpers 在 import 期定格了 FACTS_DB 快照，同样指回
    import ducky.hot.legacy_helpers as legacy_helpers
    monkeypatch.setattr(legacy_helpers, "FACTS_DB", _TEST_DB)
    from ducky.schema_bootstrap import ensure_core_schema
    from ducky.federation.schema import ensure_federation_schema
    ensure_core_schema(force=True)
    ensure_federation_schema(force=True)
    conn = utils.get_facts_conn()
    try:
        # 本文件的用例会向 verify_lineage_integrity 投喂全库状态，
        # 用例间必须清 facts/lineage，否则前一条用例的断链污染后一条
        conn.execute("DELETE FROM memory_lineage")
        conn.execute("DELETE FROM facts")
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()
    yield


def _chain(memory_id: str):
    from ducky.memory_lineage import get_memory_lineage
    return get_memory_lineage(memory_id)


# ── 1. 写入路径身份守卫 ─────────────────────────────────────

def test_upsert_conflict_keeps_single_chain():
    """同一 key 连续写三次（dedup 关闭直打 upsert）：单链、版本连号、链尾=行哈希。"""
    from ducky.federation.writer import write_fact
    from ducky.memory_lineage import compute_content_hash

    r1 = write_fact("test", "kA", "v1内容", agent_id="ag_chain", dedup=False)
    assert r1["status"] == "ok"
    fid = r1["fact_id"]
    r2 = write_fact("test", "kA", "v2内容", agent_id="ag_chain", dedup=False)
    r3 = write_fact("test", "kA", "v3内容", agent_id="ag_chain", dedup=False)
    # 冲突命中必须返回**同一行** id（旧缺陷：返回上一条 INSERT 的 id 或 0）
    assert r2["fact_id"] == fid and r3["fact_id"] == fid
    assert r2["action"] == "update" and r3["action"] == "update"

    chain = _chain(f"fact:{fid}")
    assert [c["version"] for c in chain] == [1, 2, 3]
    assert [c["action"] for c in chain] == ["CREATE", "UPDATE", "UPDATE"]
    assert chain[0]["previous_version_hash"] == ""
    assert chain[1]["previous_version_hash"] == chain[0]["content_hash"]
    assert chain[2]["previous_version_hash"] == chain[1]["content_hash"]

    conn = utils.get_facts_conn()
    try:
        row = conn.execute(
            "SELECT content_hash, version FROM facts WHERE id=?", (fid,)).fetchone()
        assert row[0] == compute_content_hash("v3内容")
        assert row[1] == 3
        assert chain[-1]["content_hash"] == row[0]
    finally:
        conn.close()


def test_interleaved_keys_do_not_cross_chains():
    """kA/kB 交错写：kA 的改写绝不可记到 kB 的链上（🔴-1 实测复现形态）。"""
    from ducky.federation.writer import write_fact

    ra1 = write_fact("test", "kA", "A_v1", agent_id="ag_x", dedup=False)
    rb1 = write_fact("test", "kB", "B_v1", agent_id="ag_x", dedup=False)
    ra2 = write_fact("test", "kA", "A_v2", agent_id="ag_x", dedup=False)

    assert ra2["fact_id"] == ra1["fact_id"]
    chain_a = _chain(f"fact:{ra1['fact_id']}")
    chain_b = _chain(f"fact:{rb1['fact_id']}")
    assert [c["version"] for c in chain_a] == [1, 2]
    assert [c["version"] for c in chain_b] == [1]  # kB 不多一条别人的版本

    from ducky.memory_lineage import verify_lineage_integrity
    res = verify_lineage_integrity()
    assert res["status"] == "ok", res["broken_details"]


def test_legacy_facts_add_three_writes_single_chain():
    """legacy /facts/add 真实端点：同一 key 写三次 → 一条链 1→2→3（不分叉出 fact:<key> 假链）。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ducky.hot.legacy_routes import register_legacy_routes

    app = FastAPI()
    register_legacy_routes(app)
    client = TestClient(app)

    for i in range(1, 4):
        resp = client.post("/facts/add", params={
            "category": "test", "fact_key": "k1", "fact_value": f"值{i}",
            "source": "legacy_tester",
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "ok", resp.text

    conn = utils.get_facts_conn()
    try:
        rows = conn.execute(
            "SELECT id, content_hash, version FROM facts WHERE fact_key='k1'").fetchall()
        assert len(rows) == 1
        fid, row_hash, row_ver = rows[0]
        assert row_ver == 3
    finally:
        conn.close()

    chain = _chain(f"fact:{fid}")
    assert [c["version"] for c in chain] == [1, 2, 3]
    assert [c["action"] for c in chain] == ["CREATE", "UPDATE", "UPDATE"]
    assert chain[-1]["content_hash"] == row_hash

    # 不得存在 fact:k1 形态的假链
    assert _chain("fact:k1") == []

    from ducky.memory_lineage import verify_lineage_integrity
    res = verify_lineage_integrity()
    assert res["status"] == "ok", res["broken_details"]


# ── 2. verify 对账守卫（旧实现对这些全报绿）────────────────

def test_verify_detects_ghost_chain():
    """谱系指向不存在的事实行 → broken（生产幽灵链 fact:4351/4352 形态）。"""
    from ducky.memory_lineage import record_lineage, verify_lineage_integrity

    conn = utils.get_facts_conn()
    try:
        record_lineage(conn, memory_id="fact:999999", content="幽灵",
                       action="CREATE", actor="pattern_extract")
        conn.commit()
    finally:
        conn.close()

    res = verify_lineage_integrity()
    assert res["status"] == "broken"
    assert any("不存在" in b["reason"] for b in res["broken_details"])


def test_verify_detects_tail_hash_mismatch():
    """facts 行被绕过写入路径直改（内容哈希不符）→ broken。"""
    from ducky.federation.writer import write_fact
    from ducky.memory_lineage import verify_lineage_integrity

    r = write_fact("test", "kT", "原始内容", agent_id="ag_t", dedup=False)
    fid = r["fact_id"]
    conn = utils.get_facts_conn()
    try:
        # 模拟「绕过写入路径改写」：直接改正文与哈希
        from ducky.memory_lineage import compute_content_hash
        conn.execute("UPDATE facts SET fact_value='篡改内容', content_hash=? WHERE id=?",
                     (compute_content_hash("篡改内容"), fid))
        conn.commit()
    finally:
        conn.close()

    res = verify_lineage_integrity()
    assert res["status"] == "broken"
    assert any("链尾哈希" in b["reason"] for b in res["broken_details"])


def test_verify_rejects_factkey_form_chain():
    """fact:<fact_key> 形态（lastrowid 缺陷路径产物）→ broken。"""
    from ducky.memory_lineage import record_lineage, verify_lineage_integrity

    conn = utils.get_facts_conn()
    try:
        record_lineage(conn, memory_id="fact:some_key", content="x",
                       action="CREATE", actor="legacy")
        conn.commit()
    finally:
        conn.close()

    res = verify_lineage_integrity()
    assert res["status"] == "broken"
    assert any("非法 memory_id 形态" in b["reason"] for b in res["broken_details"])


def test_verify_delete_terminal_chain_semantics():
    """DELETE 终链：行仍存在 → broken（终链撒谎）；行删除后 → ok。"""
    from ducky.federation.writer import write_fact
    from ducky.memory_lineage import record_lineage, verify_lineage_integrity

    r = write_fact("test", "kD", "待删除", agent_id="ag_d", dedup=False)
    fid = r["fact_id"]

    conn = utils.get_facts_conn()
    try:
        record_lineage(conn, memory_id=f"fact:{fid}", content="",
                       action="DELETE", actor="ag_d")
        conn.commit()
    finally:
        conn.close()

    res = verify_lineage_integrity()
    assert res["status"] == "broken"  # 行还在却记了 DELETE
    assert any("仍存在" in b["reason"] for b in res["broken_details"])

    conn = utils.get_facts_conn()
    try:
        conn.execute("DELETE FROM facts WHERE id=?", (fid,))
        conn.commit()
    finally:
        conn.close()

    res2 = verify_lineage_integrity()
    assert res2["status"] == "ok", res2["broken_details"]


# ── 3. 谱系治理守卫（🟡-5）──────────────────────────────────

def test_lineage_unique_constraint_blocks_silent_fork():
    """UNIQUE(memory_id, version) 存在且生效：手工插重复版本 → IntegrityError。"""
    import sqlite3 as _s3
    from ducky.memory_lineage import ensure_lineage_schema, record_lineage

    conn = utils.get_facts_conn()
    try:
        ensure_lineage_schema(conn)
        names = {r[1] for r in conn.execute(
            "PRAGMA index_list(memory_lineage)").fetchall()}
        assert "idx_lineage_mem_version_unique" in names

        record_lineage(conn, memory_id="fact:1", content="v1",
                       action="CREATE", actor="t")
        conn.commit()
        with pytest.raises(_s3.IntegrityError):
            conn.execute(
                "INSERT INTO memory_lineage (memory_id, version, content_hash, action, actor) "
                "VALUES ('fact:1', 1, 'dup', 'UPDATE', 't')")
        conn.rollback()
    finally:
        conn.close()


def test_terminal_lineage_records_delete():
    """删除留痕：有链事实的 DELETE 终链续版本号；无链事实记单节点终链。"""
    from ducky.federation.writer import write_fact
    from ducky.memory_lineage import record_terminal_lineage, verify_lineage_integrity

    r = write_fact("test", "kT8", "将被删除", agent_id="ag8", dedup=False)
    fid = r["fact_id"]
    conn = utils.get_facts_conn()
    try:
        res = record_terminal_lineage(conn, memory_id=f"fact:{fid}",
                                      action="DELETE", actor="ag8", source="test")
        assert res["status"] == "ok" and res["version"] == 2
        # 无链事实的单节点终链
        res2 = record_terminal_lineage(conn, memory_id="fact:424242",
                                       action="DELETE", actor="ag8", source="test")
        assert res2["status"] == "ok" and res2["version"] == 1
        # 非法动作拒绝
        assert record_terminal_lineage(conn, memory_id="fact:1", action="UPDATE"
                                       )["status"] == "error"
        conn.execute("DELETE FROM facts WHERE id=?", (fid,))
        conn.commit()
    finally:
        conn.close()

    chain = _chain(f"fact:{fid}")
    assert [c["action"] for c in chain] == ["CREATE", "DELETE"]
    res = verify_lineage_integrity()
    assert res["status"] == "ok", res["broken_details"]


def test_diff_summary_has_no_fact_key_plaintext():
    """diff_summary 不得含 fact_key 明文（删除权行使后谱系里不留用户原话）。"""
    from ducky.federation.writer import write_fact

    r = write_fact("私密类", "mysecret", "内容", agent_id="agp", dedup=False)
    assert r["status"] == "ok"
    chain = _chain(f"fact:{r['fact_id']}")
    assert chain
    assert all("mysecret" not in (c["diff_summary"] or "") for c in chain)
