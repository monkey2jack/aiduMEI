"""Historical type backfill must preserve newer FTS text and category."""
from __future__ import annotations

import sqlite3


def test_typed_backfill_preserves_existing_category_and_is_idempotent(
    monkeypatch, tmp_path,
):
    import ducky.text_fts as fts

    db = tmp_path / "text_fts.db"

    def connect():
        return sqlite3.connect(db)

    monkeypatch.setattr(fts, "get_text_conn", connect)
    conn = connect()
    fts._ensure_trigram_fts(conn)
    conn.close()

    fts._index_memory("m1", "用户决定迁移", user_id="alice", bank_id="work",
                      category="corrected")

    # Qdrant can retain older text and category after an FTS correction.
    # The type backfill owns neither field in an existing row.
    assert fts._upsert_typed_memory(
        "m1", "旧正文，已经撤销", "DECISIONS", user_id="alice", bank_id="work",
        category="old",
    ) is True
    assert fts._upsert_typed_memory(
        "m1", "旧正文，已经撤销", "DECISIONS", user_id="alice", bank_id="work",
        category="old",
    ) is False

    conn = connect()
    row = conn.execute(
        "SELECT content, category, memory_type FROM memories "
        "WHERE id=? AND user_id=? AND bank_id=?",
        ("alice\x1fwork\x1fm1", "alice", "work"),
    ).fetchone()
    conn.close()
    assert row == ("用户决定迁移", "corrected", "DECISIONS")


def test_typed_backfill_uses_payload_category_for_missing_fts_row(
    monkeypatch, tmp_path,
):
    import ducky.text_fts as fts

    db = tmp_path / "text_fts.db"

    def connect():
        return sqlite3.connect(db)

    monkeypatch.setattr(fts, "get_text_conn", connect)
    conn = connect()
    fts._ensure_trigram_fts(conn)
    conn.close()

    assert fts._upsert_typed_memory(
        "m2", "用户偏好 Python", "PREFERENCES", user_id="alice", bank_id="work",
        category="tech",
    ) is True
    conn = connect()
    row = conn.execute(
        "SELECT category, memory_type FROM memories WHERE user_id=? AND bank_id=?",
        ("alice", "work"),
    ).fetchone()
    conn.close()
    assert row == ("tech", "PREFERENCES")
