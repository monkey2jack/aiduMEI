"""
tests/test_p1_memory_types.py — P1-1 记忆类型分离测试
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
覆盖：
  1. 规则判型确定性行为
  2. 类型账本写入/去重/查询
  3. facts 存量回填
  4. /memory/types 路由查询视图
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp_dir = tempfile.mkdtemp(prefix="aidumem_p1_test_")
_TEST_DB = os.path.join(_tmp_dir, "facts.db")

import pytest  # noqa: E402

import ducky.utils as utils  # noqa: E402

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
    confidence INTEGER DEFAULT 100,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    trust_score REAL DEFAULT 0.5,
    archived INTEGER DEFAULT 0,
    valid_from TEXT,
    valid_to TEXT,
    recorded_at TIMESTAMP,
    level TEXT DEFAULT 'I'
);
"""


def _fresh():
    import ducky.memory_types as mt
    conn = sqlite3.connect(_TEST_DB)
    for table in ("facts", "memory_types"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute(_FACTS_DDL)
    conn.commit()
    conn.close()
    mt._checked = False
    return mt


def test_classify_rules():
    mt = _fresh()
    assert mt.classify_text("用户决定使用 Qdrant 作为向量库") == "DECISIONS"
    assert mt.classify_text("用户偏好 Python，不太喜欢 React") == "PREFERENCES"
    assert mt.classify_text("我帮用户部署了 Dashboard API") == "EXPERIENCES"
    assert mt.classify_text("东京服务器 18888 端口暴露在公网") == "OBSERVATIONS"
    assert mt.classify_text("没有明确信号的普通内容") == "FACTS"


def test_record_and_get_type():
    mt = _fresh()
    r = mt.classify_and_record("mem1", "用户偏好 Python", use_llm=False)
    assert r["memory_type"] == "PREFERENCES"
    assert mt.get_memory_type("mem1") == "PREFERENCES"
    # 更新同一条
    mt.classify_and_record("mem1", "用户喜欢 Go 了", use_llm=False)
    assert mt.get_memory_type("mem1") == "PREFERENCES"


def test_list_types():
    mt = _fresh()
    mt.classify_and_record("mem1", "用户偏好 Python")
    mt.classify_and_record("mem2", "用户偏好 Go")
    mt.classify_and_record("mem3", "用户决定迁移到 Qdrant")
    rows = mt.list_types()
    by_type = {r["memory_type"]: r["count"] for r in rows}
    assert by_type.get("PREFERENCES") == 2
    assert by_type.get("DECISIONS") == 1


def test_backfill_from_facts():
    mt = _fresh()
    conn = sqlite3.connect(_TEST_DB)
    conn.executemany(
        "INSERT INTO facts (category, fact_key, fact_value, archived) VALUES (?,?,?,0)",
        [
            ("偏好", "语言", "用户偏好 Python"),
            ("项目", "决定", "用户决定迁移到 Qdrant"),
        ],
    )
    conn.commit()
    conn.close()

    result = mt.backfill_from_facts(limit=100)
    assert result["scanned"] == 2
    assert result["classified"] == 2
    assert mt.get_memory_type("fact:1") == "PREFERENCES"
    assert mt.get_memory_type("fact:2") == "DECISIONS"


def test_memory_types_routes():
    mt = _fresh()
    # 造一条 fact 并回填
    conn = sqlite3.connect(_TEST_DB)
    conn.execute(
        "INSERT INTO facts (category, fact_key, fact_value, archived) VALUES ('偏好','语言','用户偏好 Python',0)"
    )
    conn.commit()
    conn.close()
    mt.backfill_from_facts(limit=10)

    from fastapi.testclient import TestClient
    from ducky.routes_p1 import register_p1_routes
    from fastapi import FastAPI

    app = FastAPI()
    register_p1_routes(app)
    client = TestClient(app)

    r = client.get("/memory/types")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"

    r = client.get("/memory/types/query", params={"memory_type": "PREFERENCES", "limit": 10})
    assert r.status_code == 200
    facts = r.json()["facts"]
    assert len(facts) == 1
    assert facts[0]["fact_key"] == "语言"

    r = client.post("/memory/types/backfill", json={"limit": 50})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
