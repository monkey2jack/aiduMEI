"""f0.3 C5 (S-5): /memory/types/query must never CAST a UUID into a facts id.

The ledger holds two disjoint key spaces: `fact:<int>` (facts rowid, written
by the backfill) and mem0 UUIDs (written by the /add path).  The old join
`f.id = CAST(substr(mt.memory_ref_raw, 6) AS INTEGER)` turned the UUID
'550e8400-e29b-…' into 400 and listed unrelated fact #400 as the memory.
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

UUID = "550e8400-e29b-41d4-a716-446655440000"      # substr(…, 6) -> '400-…' -> 400
_OLD_JOIN = ("SELECT f.id FROM memory_types mt "
             "JOIN facts f ON f.id = CAST(substr(mt.memory_ref_raw, 6) AS INTEGER) "
             "WHERE mt.memory_type = 'PREFERENCES'")


@pytest.fixture
def world(tmp_path, monkeypatch):
    import ducky.memory_types as mt
    import ducky.text_fts as fts
    import ducky.utils as utils
    from ducky.schema_bootstrap import ensure_core_schema

    facts_db = str(tmp_path / "facts.db")
    monkeypatch.setattr(utils, "FACTS_DB", facts_db)
    monkeypatch.setattr(utils, "TEXT_FTS_DB", str(tmp_path / "text_fts.db"))
    monkeypatch.setattr(mt, "_checked", False)
    ensure_core_schema(force=True)
    fts._init_text_fts()
    conn = sqlite3.connect(facts_db)
    conn.execute("INSERT INTO facts (id, category, fact_key, fact_value, user_id, bank_id) "
                 "VALUES (1, '偏好', '语言', '用户偏好 Python', 'alice', 'default')")
    conn.execute("INSERT INTO facts (id, category, fact_key, fact_value, user_id, bank_id) "
                 "VALUES (400, '运维', 'unrelated', 'UNRELATED FACT 400', 'alice', 'default')")
    conn.commit()
    conn.close()
    mt.classify_and_record("fact:1", "用户偏好 Python", user_id="alice")
    mt.classify_and_record(UUID, "用户偏好燕麦拿铁", user_id="alice")
    fts._index_memory(UUID, "用户偏好燕麦拿铁", user_id="alice", bank_id="default")

    app = FastAPI()
    from ducky.routes_p1 import register_p1_routes
    register_p1_routes(app)
    return TestClient(app), facts_db, mt, fts


def test_fixture_reproduces_the_uuid_cast_defect(world):
    """Negative control: the old join really returns the unrelated fact."""
    _, facts_db, _, _ = world
    ids = [r[0] for r in sqlite3.connect(facts_db).execute(_OLD_JOIN)]
    assert 400 in ids, "fixture lost its discriminating power"


def test_uuid_ref_never_joins_an_unrelated_fact(world):
    client, _, _, _ = world
    body = client.get("/memory/types/query",
                      params={"memory_type": "PREFERENCES", "user_id": "alice"}).json()
    assert body["status"] == "ok", body
    assert [f["id"] for f in body["facts"]] == [1]
    assert "UNRELATED FACT 400" not in str(body)


def test_uuid_ref_resolves_through_its_own_store(world):
    client, _, _, _ = world
    body = client.get("/memory/types/query",
                      params={"memory_type": "PREFERENCES", "user_id": "alice"}).json()
    assert body["memory_count"] == 1
    mem, = body["memories"]
    assert mem["memory_id"] == UUID and mem["resolved"] is True
    assert mem["content"] == "用户偏好燕麦拿铁"


def test_named_bank_uuid_resolves_through_the_scoped_storage_key(world):
    client, _, mt, fts = world
    other = "0b5c2f9e-1111-4222-8333-944455556666"
    mt.classify_and_record(other, "用户偏好乌龙茶", user_id="alice", bank_id="work")
    fts._index_memory(other, "用户偏好乌龙茶", user_id="alice", bank_id="work")
    work = client.get("/memory/types/query", params={
        "memory_type": "PREFERENCES", "user_id": "alice", "bank_id": "work"}).json()
    assert [(m["memory_id"], m["content"]) for m in work["memories"]] == [(other, "用户偏好乌龙茶")]
    assert work["facts"] == []
    default = client.get("/memory/types/query", params={
        "memory_type": "PREFERENCES", "user_id": "alice"}).json()
    assert other not in str(default), "named-bank memory leaked into the default bank"


def test_malformed_fact_ref_does_not_join(world):
    client, _, mt, _ = world
    mt.classify_and_record("fact:400abc", "用户偏好 malformed ref", user_id="alice")
    body = client.get("/memory/types/query",
                      params={"memory_type": "PREFERENCES", "user_id": "alice"}).json()
    assert [f["id"] for f in body["facts"]] == [1]


def test_unindexed_uuid_is_reported_unresolved_not_guessed(world):
    client, _, mt, _ = world
    ghost = "9f1e2d3c-aaaa-4bbb-8ccc-0123456789ab"
    mt.classify_and_record(ghost, "用户偏好无索引", user_id="alice")
    body = client.get("/memory/types/query",
                      params={"memory_type": "PREFERENCES", "user_id": "alice"}).json()
    row = next(m for m in body["memories"] if m["memory_id"] == ghost)
    assert row["resolved"] is False and row["content"] is None
