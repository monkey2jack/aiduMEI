"""Rejected facts keep their tenant scope through snapshot and erasure."""
import json

import pytest


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    import ducky.utils as utils
    from ducky.governance import ensure_governance_schema
    from ducky.tombstone import ensure_tombstone_schema

    monkeypatch.setattr(utils, "FACTS_DB", str(tmp_path / "facts.db"))
    monkeypatch.setattr(utils, "TEXT_FTS_DB", str(tmp_path / "text_fts.db"))
    conn = utils.get_facts_conn()
    conn.execute("""CREATE TABLE facts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT, fact_key TEXT, fact_value TEXT, source TEXT,
        user_id TEXT, bank_id TEXT, trust_score REAL DEFAULT 0.5,
        archived INTEGER DEFAULT 0, archived_at TEXT
    )""")
    conn.commit()
    ensure_governance_schema()
    ensure_tombstone_schema()
    yield conn
    conn.close()


def reject_fact(conn, route, user, bank, key="schedule"):
    from ducky.governance import (
        evaluate_candidate, govern_fact_write, review_candidate,
    )

    value = "。。。。。。" if route == "rule" else "周三下午做演示，先备三张说明卡"
    cur = conn.execute("""INSERT INTO facts
        (category, fact_key, fact_value, source, user_id, bank_id)
        VALUES ('pattern_kv', ?, ?, 'pattern_extract', ?, ?)""",
        (key, value, user, bank))
    conn.commit()
    result = govern_fact_write(conn, cur.lastrowid, "pattern_kv", key, value,
                               user_id="pattern_extract")
    conn.commit()
    candidate_id = result["candidate_id"]
    assert candidate_id is not None
    if route == "rule":
        assert result["route"] == "rule_rejected"
    elif route == "evaluator":
        result = evaluate_candidate(candidate_id, evaluator=lambda c, k, v: {
            "verdict": "reject", "confidence": 0.95, "reason": "temporary schedule",
        })
        assert result["status"] == "rejected"
    else:
        result = review_candidate(candidate_id, "reject", "temporary schedule",
                                  user_id=user, bank_id=bank)
        assert result["status"] == "rejected"
    return candidate_id, value


@pytest.mark.parametrize("route", ["rule", "evaluator", "human"])
@pytest.mark.parametrize("user,bank", [
    ("scope-alpha", "work"), ("scope-beta", "work"),
    ("scope-alpha", "home"), ("default", "default"),
])
def test_rejection_snapshot_uses_fact_scope(isolated_store, route, user, bank):
    from ducky.tombstone import list_tombstones

    conn = isolated_store
    candidate_id, value = reject_fact(conn, route, user, bank)
    candidate = conn.execute("SELECT * FROM candidate_facts WHERE candidate_id=?",
                             (candidate_id,)).fetchone()
    assert candidate["user_id"] == "pattern_extract"
    assert (candidate["scope_user_id"], candidate["bank_id"]) == (user, bank)
    tombstone = conn.execute("SELECT * FROM tombstones").fetchone()
    assert (tombstone["user_id"], tombstone["bank_id"]) == (user, bank)
    assert tombstone["content_snapshot"] == value
    assert json.loads(tombstone["facts_snapshot"])["fact_value"] == value
    assert len(list_tombstones(user_id=user, bank_id=bank)) == 1
    assert list_tombstones(user_id="pattern_extract", bank_id=bank) == []
    event = conn.execute("SELECT user_id,bank_id FROM memory_events WHERE action='reject'").fetchone()
    assert tuple(event) == (user, bank)


@pytest.mark.parametrize("route", ["rule", "evaluator", "human"])
def test_erasure_removes_only_the_rejected_owner_snapshot(isolated_store, route):
    from ducky.bank_contract import make_scope
    from ducky.wal_engine import _cascade_all_tombstones

    conn = isolated_store
    scopes = [("scope-alpha", "work"), ("scope-beta", "work"), ("scope-alpha", "home")]
    for index, (user, bank) in enumerate(scopes):
        reject_fact(conn, route, user, bank, key=f"schedule-{index}")
    expected = [dict(row) for row in conn.execute("SELECT * FROM tombstones")
                if (row["user_id"], row["bank_id"]) != scopes[0]]
    result = {}
    failures = []
    _cascade_all_tombstones(make_scope(*scopes[0]), result,
                            lambda layer, error: failures.append((layer, error)))
    assert failures == []
    assert result["tombstones_deleted"] == 1
    assert [dict(row) for row in conn.execute("SELECT * FROM tombstones")] == expected
    assert len(expected) == 2
    assert conn.execute("SELECT COUNT(*) FROM tombstones WHERE user_id='pattern_extract'").fetchone()[0] == 0

