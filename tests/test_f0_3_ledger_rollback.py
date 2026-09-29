"""f0.3: a failed distill_sources ledger write never rides along with the next commit.

The ledger helpers share the request thread's facts connection.  A statement
that fails half-way leaves that connection inside a transaction: the rows it
already wrote stay pending (holding the write lock) and the next unrelated
commit on the same thread would persist the half-done write.  The helpers now
roll back -- but only a transaction they opened themselves; pending writes of
the caller on the same connection are not theirs to discard.
"""
from __future__ import annotations

import sqlite3

import pytest

POISON = "poison-ref"
USER, BANK = "alice", "work"


def _summary_md(refs):
    return {"kind": "session_distill", "lane": "distill", "_origin_agent": "session-distill",
            "_origin_session_id": "s-1", "distill_source_refs": list(refs)}


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    import ducky.utils as utils
    from ducky import session_distill as sd
    db = str(tmp_path / "facts.db")
    monkeypatch.setattr(utils, "FACTS_DB", db)
    conn = utils.get_facts_conn()
    sd.ensure_distill_sources_schema(conn)
    conn.execute("CREATE TABLE caller_work (v TEXT)")
    conn.commit()

    def committed(sql):
        """What another connection sees: committed state only."""
        other = sqlite3.connect(db)
        try:
            return other.execute(sql).fetchone()[0]
        finally:
            other.close()

    yield sd, conn, committed
    if conn.in_transaction:
        conn.rollback()


def _poison(conn, event):
    row = "NEW" if event == "INSERT" else "OLD"
    conn.execute(f"CREATE TRIGGER boom BEFORE {event} ON distill_sources "
                 f"WHEN {row}.source_ref = '{POISON}' "
                 "BEGIN SELECT RAISE(ABORT, 'simulated ledger failure'); END")
    conn.commit()


def _unrelated_commit(conn):
    conn.execute("INSERT INTO caller_work VALUES ('later request')")
    conn.commit()


def test_negative_control_the_ledger_write_is_visible(ledger):
    sd, conn, committed = ledger
    assert sd.record_summary_sources(USER, BANK, _summary_md(["a", "b", "c"]), "summary") == 3
    assert committed("SELECT COUNT(*) FROM distill_sources") == 3


def test_failed_record_leaves_no_half_written_rows(ledger):
    sd, conn, committed = ledger
    _poison(conn, "INSERT")
    md = _summary_md(["a", "b", POISON, "c"])
    assert sd.record_summary_sources(USER, BANK, md, "summary") == 0
    assert conn.in_transaction is False, "failed ledger write left the transaction open"
    _unrelated_commit(conn)
    assert committed("SELECT COUNT(*) FROM distill_sources") == 0, (
        "the next unrelated commit persisted the half-done ledger write")


def test_failed_forget_is_all_or_nothing(ledger):
    sd, conn, committed = ledger
    assert sd.record_summary_sources(USER, BANK, _summary_md(["a", "b"]), "first") == 2
    assert sd.record_summary_sources(USER, BANK, _summary_md([POISON]), "second") == 1
    _poison(conn, "DELETE")
    hashes = [sd.summary_text_hash("first"), sd.summary_text_hash("second")]
    with pytest.raises(sqlite3.Error):
        sd.forget_summary_sources(USER, BANK, hashes)
    assert conn.in_transaction is False
    _unrelated_commit(conn)
    assert committed("SELECT COUNT(*) FROM distill_sources") == 3, (
        "rows of the first summary were deleted by a failed forget")


def test_failed_scope_delete_does_not_leave_a_transaction_open(ledger):
    sd, conn, committed = ledger
    assert sd.record_summary_sources(USER, BANK, _summary_md(["a", POISON]), "summary") == 2
    _poison(conn, "DELETE")
    with pytest.raises(sqlite3.Error):
        sd.delete_scope_sources(USER, BANK)
    assert conn.in_transaction is False
    _unrelated_commit(conn)
    assert committed("SELECT COUNT(*) FROM distill_sources") == 2


def test_the_callers_pending_write_is_never_discarded(ledger):
    sd, conn, committed = ledger
    _poison(conn, "INSERT")
    conn.execute("INSERT INTO caller_work VALUES ('pending caller write')")
    assert sd.record_summary_sources(USER, BANK, _summary_md([POISON]), "summary") == 0
    assert conn.in_transaction is True, "the caller's open transaction was rolled back"
    conn.commit()
    assert committed("SELECT COUNT(*) FROM caller_work "
                     "WHERE v = 'pending caller write'") == 1
