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
    assert mt.classify_text("某服务器端口暴露在公网") == "OBSERVATIONS"
    assert mt.classify_text("没有明确信号的普通内容") == "FACTS"


def test_record_and_get_type():
    mt = _fresh()
    r = mt.classify_and_record("mem1", "用户偏好 Python", use_llm=False)
    assert r["memory_type"] == "PREFERENCES"
    assert mt.get_memory_type("mem1") == "PREFERENCES"
    # 更新同一条
    mt.classify_and_record("mem1", "用户喜欢 Go 了", use_llm=False)
    assert mt.get_memory_type("mem1") == "PREFERENCES"


def test_same_default_bank_ref_can_belong_to_two_users():
    mt = _fresh()
    mt.classify_and_record("same-id", "用户偏好 Python", user_id="alice")
    mt.classify_and_record("same-id", "用户决定迁移", user_id="bob")
    mt.classify_and_record("later-default", "用户偏好 Go", user_id="alice")
    mt.classify_and_record("later-default", "用户决定保留", user_id=utils.DEFAULT_USER_ID)

    assert mt.get_memory_type("same-id", user_id="alice") == "PREFERENCES"
    assert mt.get_memory_type("same-id", user_id="bob") == "DECISIONS"
    assert mt.get_memory_type("later-default", user_id="alice") == "PREFERENCES"
    assert mt.get_memory_type("later-default", user_id=utils.DEFAULT_USER_ID) == "DECISIONS"
    conn = sqlite3.connect(_TEST_DB)
    rows = conn.execute(
        "SELECT user_id, memory_ref_raw, memory_type FROM memory_types ORDER BY user_id"
    ).fetchall()
    conn.close()
    assert len(rows) == 4
    assert {r for r in rows if r[1] == "same-id"} == {
        ("alice", "same-id", "PREFERENCES"), ("bob", "same-id", "DECISIONS")}
    assert {r for r in rows if r[1] == "later-default"} == {
        ("alice", "later-default", "PREFERENCES"),
        (utils.DEFAULT_USER_ID, "later-default", "DECISIONS")}


def test_legacy_fts_migration_preserves_category_and_adds_distinct_type(tmp_path):
    from ducky.text_fts import _ensure_trigram_fts

    conn = sqlite3.connect(tmp_path / "legacy_fts.db")
    conn.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT, "
                 "user_id TEXT, bank_id TEXT DEFAULT 'default', category TEXT, "
                 "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    conn.execute("INSERT INTO memories (id,content,user_id,bank_id,category) "
                 "VALUES ('m1','用户偏好 Python','alice','default','tech')")
    conn.commit()
    _ensure_trigram_fts(conn)
    first = conn.execute("SELECT category, memory_type FROM memories WHERE id='m1'").fetchone()
    assert first == ("tech", "FACTS")
    conn.execute("UPDATE memories SET memory_type='PREFERENCES' WHERE id='m1'")
    conn.commit()
    _ensure_trigram_fts(conn)
    second = conn.execute("SELECT category, memory_type FROM memories WHERE id='m1'").fetchone()
    assert second == ("tech", "PREFERENCES")
    conn.close()


def test_fts_type_updates_keep_categories_in_same_ref_across_scopes(monkeypatch, tmp_path):
    import ducky.text_fts as fts

    db = str(tmp_path / "text_fts.db")
    monkeypatch.setattr(utils, "TEXT_FTS_DB", db)
    fts._init_text_fts()
    fts._index_memory("same-ref", "用户偏好 Python", user_id="alice",
                      bank_id="default", category="alice-default")
    fts._index_memory("same-ref", "用户决定迁移", user_id="bob",
                      bank_id="default", category="bob-default")
    fts._index_memory("same-ref", "观察到配置", user_id="alice",
                      bank_id="work", category="alice-work")

    assert fts._set_memory_type("same-ref", "DECISIONS", user_id="bob",
                                bank_id="default") == 1
    assert fts._set_memory_type("same-ref", "OBSERVATIONS", user_id="alice",
                                bank_id="work") == 1
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT user_id, bank_id, category, memory_type FROM memories "
        "ORDER BY user_id, bank_id"
    ).fetchall()
    conn.close()
    assert rows == [
        ("alice", "default", "alice-default", "FACTS"),
        ("alice", "work", "alice-work", "OBSERVATIONS"),
        ("bob", "default", "bob-default", "DECISIONS"),
    ]


