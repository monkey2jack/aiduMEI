"""Historical FTS rows must not mask types recorded in the scoped ledger."""

from __future__ import annotations

import sqlite3
import time

import pytest


@pytest.mark.parametrize("legacy_pk", [False, True])
def test_historical_fts_type_uses_recorded_ledger_label_and_filters_it(
    monkeypatch, tmp_path, legacy_pk,
):
    import ducky.memory_types as types
    import ducky.scoring as scoring
    import ducky.text_fts as fts

    text_db = tmp_path / "text_fts.db"
    facts_db = tmp_path / "facts.db"

    def text_conn():
        conn = sqlite3.connect(text_db)
        conn.row_factory = sqlite3.Row
        return conn

    def facts_conn():
        conn = sqlite3.connect(facts_db)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(fts, "get_text_conn", text_conn)
    monkeypatch.setattr(types, "get_facts_conn", facts_conn)
    monkeypatch.setattr(types, "_checked", False)

    pk = "PRIMARY KEY (id, user_id, bank_id)" if legacy_pk else "PRIMARY KEY (id)"
    conn = text_conn()
    conn.execute(
        "CREATE TABLE memories (id TEXT NOT NULL, content TEXT, user_id TEXT, "
        "bank_id TEXT NOT NULL DEFAULT 'default', category TEXT, "
        f"created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, {pk})"
    )
    conn.execute(
        "INSERT INTO memories (id,content,user_id,bank_id,category) "
        "VALUES (?,?,?,?,?)",
        ("m1", "alpha decision", "alice", "default", "knowledge"),
    )
    conn.commit()
    conn.close()

    types.classify_and_record("m1", "用户决定迁移到新系统", user_id="alice")
    hits = fts._bm25_keyword_search("alpha", user_id="alice")
    assert len(hits) == 1
    assert hits[0]["memory_type"] == "DECISIONS"
    conn = text_conn()
    assert conn.execute("SELECT memory_type FROM memories WHERE id='m1'").fetchone()[0] == "FACTS"
    conn.close()

    labels = scoring._load_type_map(hits, "alice", "default")
    assert labels == {"m1": "DECISIONS"}
    kept, gated = scoring._score_one_candidate(
        "alpha", hits[0], w=scoring.DEFAULT_WEIGHTS, now_ts=time.time(),
        is_fact_query=False, type_decay_on=False, salience_map={},
        type_map=labels, memory_type_filter="DECISIONS", gate_on=False,
    )
    assert kept is hits[0] and not gated
    assert kept["memory_type"] == "DECISIONS"


def test_only_recorded_ledger_type_overrides_candidate(monkeypatch, tmp_path):
    import ducky.memory_types as types
    import ducky.scoring as scoring

    facts_db = tmp_path / "facts.db"

    def facts_conn():
        conn = sqlite3.connect(facts_db)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(types, "get_facts_conn", facts_conn)
    monkeypatch.setattr(types, "_checked", False)

    types.classify_and_record("decision", "用户决定迁移到新系统", user_id="alice")
    types.classify_and_record("fact", "ordinary information", user_id="alice")
    candidates = [
        {"id": "decision", "memory_type": "FACTS"},  # prior f0.2 migration
        {"id": "fact", "memory_type": "DECISIONS"},  # stale replica
        {"id": "unrecorded", "memory_type": "PREFERENCES"},
    ]
    labels = scoring._load_type_map(candidates, "alice", "default")
    assert labels == {"decision": "DECISIONS", "fact": "FACTS"}
    assert [scoring._resolve_memory_type(item, labels) for item in candidates] == [
        "DECISIONS", "FACTS", "PREFERENCES",
    ]
    assert types.get_batch_memory_types(["unrecorded"], user_id="alice") == {
        "unrecorded": "FACTS",
    }


def test_keyword_type_annotation_keeps_user_and_bank_scope(monkeypatch, tmp_path):
    import ducky.memory_types as types
    import ducky.text_fts as fts

    text_db = tmp_path / "text_fts.db"
    facts_db = tmp_path / "facts.db"

    def text_conn():
        conn = sqlite3.connect(text_db)
        conn.row_factory = sqlite3.Row
        return conn

    def facts_conn():
        conn = sqlite3.connect(facts_db)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(fts, "get_text_conn", text_conn)
    monkeypatch.setattr(types, "get_facts_conn", facts_conn)
    monkeypatch.setattr(types, "_checked", False)
    conn = text_conn()
    fts._ensure_trigram_fts(conn)
    conn.close()

    for user, bank, label_text in (
        ("alice", "default", "用户决定迁移"),
        ("bob", "default", "用户偏好 Python"),
        ("alice", "work", "观察到配置"),
    ):
        fts._index_memory("shared", "alpha", user_id=user, bank_id=bank)
        types.classify_and_record("shared", label_text, user_id=user, bank_id=bank)

    assert fts._bm25_keyword_search("alpha", user_id="alice")[0]["memory_type"] == "DECISIONS"
    assert fts._bm25_keyword_search("alpha", user_id="bob")[0]["memory_type"] == "PREFERENCES"
    assert fts._bm25_keyword_search("alpha", user_id="alice", bank_id="work")[0]["memory_type"] == "OBSERVATIONS"
