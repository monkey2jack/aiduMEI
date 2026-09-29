"""f0.3 C6 (S-7): verbatim store commits facts first, then FTS, and only
counts what actually committed.

Defect: `with fconn, tconn:` exits in reverse order, so text_fts committed
*before* facts; utils._ConnProxy.__exit__ swallows a failed commit as a
WARNING, and `stored` was counted inside the loop.  A failed facts commit
therefore left FTS map rows pointing at turns that never existed while the
result claimed success.
"""
from __future__ import annotations

import sqlite3

import pytest


class _Conn:
    """Wraps a real connection; can fail commit and records the order."""

    def __init__(self, conn, name, log, fail_commit=False):
        self._conn = conn
        self._name = name
        self._log = log
        self.fail_commit = fail_commit

    def __getattr__(self, attr):
        return getattr(self._conn, attr)

    def execute(self, sql, *args):
        if "verbatim_fts_map" in sql:
            self._log.append(f"{self._name}:fts_write")
        return self._conn.execute(sql, *args)

    def executemany(self, sql, *args):
        if "verbatim_fts_map" in sql:
            self._log.append(f"{self._name}:fts_write")
        return self._conn.executemany(sql, *args)

    def commit(self):
        self._log.append(f"{self._name}:commit")
        if self.fail_commit:
            raise sqlite3.OperationalError("disk I/O error (injected)")
        return self._conn.commit()

    def close(self):
        pass


@pytest.fixture
def vault(tmp_path, monkeypatch):
    import ducky.utils as utils
    import ducky.verbatim_vault as vv

    facts_db = str(tmp_path / "facts.db")
    text_db = str(tmp_path / "text_fts.db")
    monkeypatch.setattr(utils, "FACTS_DB", facts_db)
    monkeypatch.setattr(utils, "TEXT_FTS_DB", text_db)
    vv.ensure_verbatim_schema()
    monkeypatch.setattr(vv, "ensure_verbatim_schema", lambda: None)
    log: list = []
    conns = {
        "facts": _Conn(utils.get_facts_conn(), "facts", log),
        "text": _Conn(utils.get_text_conn(), "text", log),
    }
    monkeypatch.setattr(vv, "get_facts_conn", lambda: conns["facts"])
    monkeypatch.setattr(vv, "get_text_conn", lambda: conns["text"])

    def counts():
        f = sqlite3.connect(facts_db).execute("SELECT COUNT(*) FROM verbatim_turns").fetchone()[0]
        t = sqlite3.connect(text_db).execute("SELECT COUNT(*) FROM verbatim_fts_map").fetchone()[0]
        return f, t

    yield vv, conns, log, counts
    for c in conns.values():
        try:
            c._conn.rollback()
        except sqlite3.Error:
            pass


def _failures(feature="store_verbatim"):
    from ducky import failure_ledger
    return failure_ledger.snapshot()["by_feature"].get(feature, 0)


MSG = [{"role": "user", "content": "the exact words the user said"}]


def test_negative_control_clean_store_counts_and_indexes(vault):
    vv, _, log, counts = vault
    out = vv.store_verbatim("alice", MSG, {"session_id": "s1"}, bank_id="work")
    assert out["stored"] == 1 and "error" not in out
    assert counts() == (1, 1)
    # facts commit happens before any FTS write, and before the FTS commit
    assert log.index("facts:commit") < log.index("text:fts_write") < log.index("text:commit")


def test_failed_facts_commit_rolls_back_both_and_is_reported(vault):
    vv, conns, _, counts = vault
    conns["facts"].fail_commit = True
    before = _failures()
    out = vv.store_verbatim("alice", MSG, {"session_id": "s1"}, bank_id="work")
    assert out["stored"] == 0, "stored counted a row whose commit failed"
    assert out["error"].startswith("facts_commit_failed")
    conns["facts"].fail_commit = False
    assert counts() == (0, 0), "an FTS row survived for a turn that never committed"
    assert _failures() == before + 1, "the failure was swallowed"


def test_failed_fts_commit_keeps_the_committed_turn_and_says_so(vault):
    vv, conns, _, counts = vault
    conns["text"].fail_commit = True
    before = _failures()
    out = vv.store_verbatim("alice", MSG, {"session_id": "s1"}, bank_id="work")
    assert out["stored"] == 1 and out["fts_failed"] == 1
    conns["text"].fail_commit = False
    assert counts() == (1, 0)
    assert _failures() == before + 1
