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

    result = mt.backfill_from_facts(limit=100, apply=True)
    assert result["scanned"] == 2
    assert result["classified"] == 2
    assert mt.get_memory_type("fact:1") == "PREFERENCES"
    assert mt.get_memory_type("fact:2") == "DECISIONS"


def test_facts_backfill_dry_run_cursor_and_preserve_existing():
    mt = _fresh()
    conn = sqlite3.connect(_TEST_DB)
    conn.executemany(
        "INSERT INTO facts (category, fact_key, fact_value, archived) VALUES (?,?,?,0)",
        [("偏好", "语言", f"用户偏好 Python {i}") for i in range(5)],
    )
    conn.commit()
    conn.close()
    mt.classify_and_record("fact:1", "用户决定保留此标签")

    preview = mt.backfill_from_facts(limit=2, after_id=0, apply=False)
    assert preview["scanned"] == 2
    assert preview["classified"] == 0
    assert preview["would_classify"] == 1
    assert preview["next_cursor"] == 2 and preview["has_more"] is True
    assert mt.get_memory_type("fact:2") == "FACTS", "dry-run 写进了账本"

    cursor = 0
    written = 0
    while True:
        page = mt.backfill_from_facts(limit=2, after_id=cursor, apply=True)
        written += page["classified"]
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert written == 4
    assert mt.get_memory_type("fact:1") == "DECISIONS", "回填覆盖了人工/写时标签"
    assert mt.get_memory_type("fact:5") == "PREFERENCES"
    again = mt.backfill_from_facts(limit=2, after_id=0, apply=True)
    assert again["classified"] == 0, "重跑必须幂等"


def test_backfill_route_is_dry_run_by_default_and_exposes_cursor():
    mt = _fresh()
    conn = sqlite3.connect(_TEST_DB)
    conn.execute("INSERT INTO facts (category, fact_key, fact_value, archived) "
                 "VALUES ('偏好','语言','用户偏好 Python',0)")
    conn.commit()
    conn.close()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ducky.routes_p1 import register_p1_routes

    app = FastAPI()
    register_p1_routes(app)
    body = TestClient(app).post("/memory/types/backfill", json={"limit": 1}).json()
    assert body["status"] == "ok" and body["apply"] is False
    assert body["would_classify"] == 1 and body["classified"] == 0
    assert "next_cursor" in body and "has_more" in body
    conn = sqlite3.connect(_TEST_DB)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='memory_types'").fetchone() is None
    conn.close()
    assert mt.get_memory_type("fact:1") == "FACTS"


@pytest.mark.parametrize("bad_cursor", ["x" * 129, -1, 18446744073709551616, True])
def test_backfill_route_rejects_unbounded_cursor(bad_cursor):
    from pydantic import ValidationError
    from ducky.routes_p1 import BackfillRequest

    with pytest.raises(ValidationError):
        BackfillRequest.model_validate({"source": "mem0", "cursor": bad_cursor})


def test_facts_preview_reads_legacy_type_table_without_migrating():
    mt = _fresh()
    conn = sqlite3.connect(_TEST_DB)
    conn.execute("INSERT INTO facts (category, fact_key, fact_value, archived) "
                 "VALUES ('偏好','语言','用户偏好 Python',0)")
    conn.execute("CREATE TABLE memory_types (memory_ref TEXT PRIMARY KEY, "
                 "memory_type TEXT NOT NULL, source TEXT)")
    conn.execute("INSERT INTO memory_types VALUES ('fact:1','DECISIONS','manual')")
    conn.commit()
    conn.close()

    preview = mt.backfill_from_facts(limit=1)
    assert preview["apply"] is False and preview["would_classify"] == 0
    conn = sqlite3.connect(_TEST_DB)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(memory_types)")}
    assert columns == {"memory_ref", "memory_type", "source"}, "preview 迁移了旧表"
    conn.close()


def test_facts_backfill_cursor_reaches_beyond_first_5000():
    mt = _fresh()
    conn = sqlite3.connect(_TEST_DB)
    conn.executemany(
        "INSERT INTO facts (category, fact_key, fact_value, archived) "
        "VALUES ('general',?, '普通事实',0)",
        [(f"k{i}",) for i in range(5002)],
    )
    conn.commit()
    conn.close()
    first = mt.backfill_from_facts(limit=5000)
    second = mt.backfill_from_facts(limit=5000, after_id=first["next_cursor"])
    assert first["scanned"] == 5000 and first["has_more"] is True
    assert second["scanned"] == 2 and second["has_more"] is False
    assert second["next_cursor"] == 5002


def test_mem0_backfill_pages_and_syncs_distinct_type(monkeypatch, tmp_path):
    mt = _fresh()
    import ducky.mem0_runtime as runtime
    import ducky.text_fts as fts
    from types import SimpleNamespace

    monkeypatch.setattr(utils, "TEXT_FTS_DB", str(tmp_path / "text_fts.db"))
    fts._init_text_fts()

    points = [
        SimpleNamespace(id="m1", payload={"user_id": "alice", "bank_id": "bank-a",
                                          "data": "用户偏好 Python", "category": "tech"}),
        SimpleNamespace(id="m2", payload={"user_id": "alice", "bank_id": "bank-b",
                                          "data": "用户决定迁移", "category": "plan"}),
        SimpleNamespace(id="m3", payload={"user_id": "alice", "bank_id": "bank-a",
                                          "data": "观察到端口暴露", "category": "ops"}),
    ]

    class FakeClient:
        def scroll(self, collection_name, limit, offset=None, **kw):
            assert collection_name == "mem0" and kw["with_payload"] is True
            start = int(offset or 0)
            end = min(start + limit, len(points))
            return points[start:end], (end if end < len(points) else None)

        def set_payload(self, collection_name, payload, points: list):
            assert collection_name == "mem0"
            for p in globals_points:
                if p.id in points:
                    p.payload.update(payload)

    globals_points = points
    fake = SimpleNamespace(vector_store=SimpleNamespace(client=FakeClient(), collection_name="mem0"))
    monkeypatch.setattr(runtime, "get_memory", lambda: fake)

    preview = mt.backfill_from_mem0(user_id="alice", bank_id="bank-a", limit=2,
                                    apply=False)
    assert preview["scanned"] == 2 and preview["would_classify"] == 1
    assert preview["classified"] == 0 and preview["has_more"] is True
    assert "memory_type" not in points[0].payload
    conn = sqlite3.connect(_TEST_DB)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='memory_types'").fetchone() is None
    conn.close()

    page1 = mt.backfill_from_mem0(user_id="alice", bank_id="bank-a", limit=2,
                                  apply=True)
    page2 = mt.backfill_from_mem0(user_id="alice", bank_id="bank-a", limit=2,
                                  cursor=page1["next_cursor"], apply=True)
    assert page1["classified"] == page2["classified"] == 1
    assert mt.get_memory_type("m1", user_id="alice", bank_id="bank-a") == "PREFERENCES"
    assert mt.get_memory_type("m3", user_id="alice", bank_id="bank-a") == "OBSERVATIONS"
    assert mt.get_memory_type("m2", user_id="alice", bank_id="bank-b") == "FACTS"
    assert points[0].payload["memory_type"] == "PREFERENCES"
    assert "memory_type" not in points[1].payload
    conn = sqlite3.connect(str(tmp_path / "text_fts.db"))
    rows = conn.execute("SELECT user_id, bank_id, category, memory_type FROM memories "
                        "ORDER BY id").fetchall()
    conn.close()
    assert rows == [("alice", "bank-a", "tech", "PREFERENCES"),
                    ("alice", "bank-a", "ops", "OBSERVATIONS")]
    again = mt.backfill_from_mem0(user_id="alice", bank_id="bank-a", limit=2,
                                  apply=True)
    assert again["classified"] == 0


def test_mem0_backfill_resumes_after_vector_or_fts_failure(monkeypatch, tmp_path):
    mt = _fresh()
    import ducky.mem0_runtime as runtime
    import ducky.text_fts as fts
    from types import SimpleNamespace

    monkeypatch.setattr(utils, "TEXT_FTS_DB", str(tmp_path / "text_fts.db"))
    fts._init_text_fts()
    point = SimpleNamespace(id="m1", payload={"user_id": "alice", "bank_id": "default",
                                              "data": "用户偏好 Python", "category": "tech"})

    class FakeClient:
        fail_vector = True

        def scroll(self, **kw):
            return [point], None

        def set_payload(self, *, payload, points, **kw):
            if self.fail_vector:
                self.fail_vector = False
                raise OSError("transient vector failure")
            point.payload.update(payload)

    client = FakeClient()
    fake = SimpleNamespace(vector_store=SimpleNamespace(client=client, collection_name="mem0"))
    monkeypatch.setattr(runtime, "get_memory", lambda: fake)

    page_cursor = "prior-page-offset"
    first = mt.backfill_from_mem0(user_id="alice", limit=1,
                                  cursor=page_cursor, apply=True)
    assert first["classified"] == 1 and first["failed"] == 1
    assert first["page_cursor"] == page_cursor
    assert "memory_type" not in point.payload

    original_upsert = fts._upsert_typed_memory
    fail_fts = {"once": True}

    def flaky_fts(*args, **kwargs):
        if fail_fts["once"]:
            fail_fts["once"] = False
            raise OSError("transient FTS failure")
        return original_upsert(*args, **kwargs)

    monkeypatch.setattr(fts, "_upsert_typed_memory", flaky_fts)
    second = mt.backfill_from_mem0(user_id="alice", limit=1,
                                   cursor=first["page_cursor"], apply=True)
    assert second["classified"] == 0 and second["vector_synced"] == 1
    assert second["failed"] == 1 and second["fts_synced"] == 0

    third = mt.backfill_from_mem0(user_id="alice", limit=1,
                                  cursor=second["page_cursor"], apply=True)
    assert third["classified"] == 0 and third["vector_synced"] == 0
    assert third["fts_synced"] == 1 and third["failed"] == 0
    fourth = mt.backfill_from_mem0(user_id="alice", limit=1,
                                   cursor=third["page_cursor"], apply=True)
    assert fourth["classified"] == fourth["vector_synced"] == fourth["fts_synced"] == 0
    conn = sqlite3.connect(str(tmp_path / "text_fts.db"))
    assert conn.execute("SELECT category, memory_type FROM memories").fetchone() == (
        "tech", "PREFERENCES")
    conn.close()


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
    assert fts._upsert_typed_memory("same-ref", "观察到配置", "OBSERVATIONS",
                                    user_id="alice", bank_id="work") is True
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


def test_memory_types_routes():
    mt = _fresh()
    # 造一条 fact 并回填
    conn = sqlite3.connect(_TEST_DB)
    conn.execute(
        "INSERT INTO facts (category, fact_key, fact_value, archived) VALUES ('偏好','语言','用户偏好 Python',0)"
    )
    conn.commit()
    conn.close()
    mt.backfill_from_facts(limit=10, apply=True)

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
