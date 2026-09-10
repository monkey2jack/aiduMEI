"""
tests/test_v20_5_0_crud_update.py — v20.5.0 正式版 /update 谱系腿端到端守卫
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
用户审计 🟡-1 整改守卫：旧用例只复刻 SQL 形状、从不驱动端点，于是
「memory_id 是 mem0 UUID → 命中 0 行 → 静默空转」活了整整一个版本。
本文件的用例**真正驱动 POST /update**：
  1. facts 引用形态（裸 id / fact:<id>）：端点必须推进 facts 行与谱系
  2. 纯向量 UUID 形态：响应如实报告 not_a_fact，facts 不被误碰
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp_dir = tempfile.mkdtemp(prefix="aidumem_v20_5_0_crud_")
_TEST_DB = os.path.join(_tmp_dir, "facts.db")

import ducky.utils as utils  # noqa: E402

utils.FACTS_DB = _TEST_DB


class _StubMem:
    """mem0 运行时的最小替身：只实现 /update 用到的 get/update。"""

    def get(self, mid):
        return {"id": mid, "memory": "旧内容",
                "metadata": {"user_id": "default", "bank_id": "default"}}

    def update(self, mid, data=None, metadata=None):
        return {"id": mid, "memory": data}


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch):
    utils.FACTS_DB = _TEST_DB
    from ducky.schema_bootstrap import ensure_core_schema
    from ducky.federation.schema import ensure_federation_schema
    ensure_core_schema(force=True)
    ensure_federation_schema(force=True)
    conn = utils.get_facts_conn()
    try:
        conn.execute("DELETE FROM memory_lineage")
        conn.execute("DELETE FROM facts")
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()
    monkeypatch.setattr("ducky.hot.crud.get_memory", lambda: _StubMem())
    yield


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ducky.hot.crud import register_crud_routes
    app = FastAPI()
    register_crud_routes(app)
    return TestClient(app)


def test_update_endpoint_drives_facts_leg_by_id():
    """端到端：POST /update?memory_id=<facts 行 id> → 行推进 + 谱系 UPDATE。"""
    from ducky.federation.writer import write_fact
    from ducky.memory_lineage import compute_content_hash, verify_lineage_integrity

    r = write_fact("profile", "favorite", "旧爱好", agent_id="dudu", dedup=False)
    fid = r["fact_id"]

    resp = _client().post("/update", json={
        "memory_id": str(fid), "content": "新爱好",
        "user_id": "default", "bank_id": "default",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["facts_sync"] == "advanced:1", resp.json()

    conn = utils.get_facts_conn()
    try:
        row = conn.execute(
            "SELECT fact_value, content_hash, version FROM facts WHERE id=?", (fid,)).fetchone()
        assert row[0] == "新爱好"
        assert row[1] == compute_content_hash("新爱好")
        assert row[2] == 2
    finally:
        conn.close()

    from ducky.memory_lineage import get_memory_lineage
    chain = get_memory_lineage(f"fact:{fid}")
    assert [c["version"] for c in chain] == [1, 2]
    assert chain[1]["action"] == "UPDATE"
    assert chain[1]["content_hash"] == compute_content_hash("新爱好")

    assert verify_lineage_integrity()["status"] == "ok"


def test_update_endpoint_drives_facts_leg_by_fact_key():
    """端到端：memory_id=fact_key 形态同样命中。"""
    from ducky.federation.writer import write_fact

    r = write_fact("profile", "city", "甲城", agent_id="dudu", dedup=False)
    resp = _client().post("/update", json={
        "memory_id": "city", "content": "乙城",
        "user_id": "default", "bank_id": "default",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["facts_sync"] == "advanced:1", resp.json()

    conn = utils.get_facts_conn()
    try:
        assert conn.execute("SELECT fact_value FROM facts WHERE id=?",
                            (r["fact_id"],)).fetchone()[0] == "乙城"
    finally:
        conn.close()


def test_update_endpoint_uuid_form_reports_not_a_fact():
    """mem0 UUID 形态：不静默——响应如实带 facts_sync=not_a_fact，facts 不被误碰。"""
    from ducky.federation.writer import write_fact

    r = write_fact("profile", "untouched", "别动我", agent_id="dudu", dedup=False)
    resp = _client().post("/update", json={
        "memory_id": "3495b0e0-dae9-4cc8-9abc-def012345678",
        "content": "向量记忆新内容", "user_id": "default", "bank_id": "default",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["facts_sync"] == "not_a_fact", resp.json()

    conn = utils.get_facts_conn()
    try:
        row = conn.execute("SELECT fact_value, version FROM facts WHERE id=?",
                           (r["fact_id"],)).fetchone()
        assert row[0] == "别动我" and row[1] == 1, "UUID 形态不得误碰 facts 行"
    finally:
        conn.close()


def test_update_endpoint_fact_ref_zero_hit_warns(caplog):
    """形似 facts 引用却命中 0 行 → WARNING 出声（不再静默空转）。"""
    import logging
    with caplog.at_level(logging.WARNING):
        resp = _client().post("/update", json={
            "memory_id": "987654", "content": "x",
            "user_id": "default", "bank_id": "default",
        })
    assert resp.status_code == 200, resp.text
    assert resp.json()["facts_sync"] == "no_match"
    assert any("谱系腿未推进" in rec.message for rec in caplog.records)
