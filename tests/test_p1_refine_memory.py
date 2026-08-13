"""
tests/test_p1_refine_memory.py — P1-3 记忆递归精炼测试
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
覆盖：
  1. 候选不足 → 跳过
  2. 规则降级生成精炼摘要（不启用 LLM）
  3. proposed → applied → rolled_back 生命周期
  4. /memory/refine 路由
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import ducky.utils as utils

_tmp_dir = tempfile.mkdtemp(prefix="aidumem_p1_refine_test_")
_TEST_DB = os.path.join(_tmp_dir, "facts.db")
utils.FACTS_DB = _TEST_DB


@pytest.fixture(autouse=True)
def _bind_test_db():
    utils.FACTS_DB = _TEST_DB
    yield


_FACTS_DDL = """
CREATE TABLE facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL DEFAULT 'general',
    fact_key TEXT NOT NULL,
    fact_value TEXT NOT NULL,
    source TEXT DEFAULT 'local',
    archived INTEGER DEFAULT 0,
    archived_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def _fresh():
    import ducky.refine_memory as rm
    conn = sqlite3.connect(_TEST_DB)
    for table in ("facts", "refined_memories"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute(_FACTS_DDL)
    conn.commit()
    conn.close()
    rm._checked = False
    return rm


def _seed_facts(category: str, n: int = 4):
    conn = sqlite3.connect(_TEST_DB)
    for i in range(n):
        conn.execute(
            "INSERT INTO facts (category, fact_key, fact_value, source, archived) VALUES (?,?,?,?,0)",
            (category, f"k{i}", f"value {i}", "default"),
        )
    conn.commit()
    conn.close()


def test_too_few_candidates_skipped():
    rm = _fresh()
    _seed_facts("test-cat", n=2)
    res = rm.refine_group("default", "test-cat", use_llm=False)
    assert res["status"] == "skipped"


def test_rule_based_refine_proposed():
    rm = _fresh()
    _seed_facts("ops", n=4)
    res = rm.refine_group("default", "ops", use_llm=False)
    assert res["status"] == "ok"
    assert res["state"] == "proposed"
    assert len(res["source_ids"]) == 4
    assert res["llm_used"] is False


def test_refine_lifecycle_apply_and_rollback():
    rm = _fresh()
    _seed_facts("ops", n=4)
    res = rm.refine_group("default", "ops", use_llm=False)
    rid = res["refine_id"]

    # 应用后源 facts 归档
    applied = rm.apply_refinement(rid)
    assert applied["status"] == "ok"
    assert applied["archived"] == 4
    conn = sqlite3.connect(_TEST_DB)
    assert conn.execute("SELECT COUNT(*) FROM facts WHERE archived=1").fetchone()[0] == 4
    conn.close()

    # 回滚后恢复
    rb = rm.rollback_refinement(rid)
    assert rb["status"] == "ok"
    assert rb["restored"] == 4
    conn = sqlite3.connect(_TEST_DB)
    assert conn.execute("SELECT COUNT(*) FROM facts WHERE archived=1").fetchone()[0] == 0
    conn.close()


def test_refine_routes():
    rm = _fresh()
    _seed_facts("ops", n=4)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ducky.routes_p1 import register_p1_routes

    app = FastAPI()
    register_p1_routes(app)
    client = TestClient(app)

    r = client.post("/memory/refine", json={"category": "ops", "use_llm": False})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    rid = r.json()["refine_id"]

    r = client.get("/memory/refinements", params={"state": "proposed"})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    r = client.post("/memory/refine/apply", json={"refine_id": rid})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    r = client.post("/memory/refine/rollback", json={"refine_id": rid})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
