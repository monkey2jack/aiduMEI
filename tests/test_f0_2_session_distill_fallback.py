"""Session distillation can use scoped, committed raw turns while async facts lag."""
from __future__ import annotations

import sqlite3


def test_three_committed_raw_turns_distill_even_before_sidecars(tmp_path, monkeypatch):
    import ducky.utils as utils
    import ducky.session_distill as distill
    import ducky.llm_client as llm

    db = tmp_path / "facts.db"
    monkeypatch.setattr(utils, "FACTS_DB", str(db))
    monkeypatch.setattr(llm, "call_llm", lambda *a, **kw: None)
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE memory_epistemic (
            memory_ref TEXT, origin_session_id TEXT, origin_turn INTEGER,
            created_at TEXT, user_id TEXT, bank_id TEXT
        );
        CREATE TABLE verbatim_turns (
            id INTEGER PRIMARY KEY, user_id TEXT, bank_id TEXT, session_id TEXT,
            role TEXT, content TEXT, recorded_at TEXT, created_at TEXT
        );
    """)
    for i in range(3):
        con.execute("INSERT INTO verbatim_turns VALUES (?,?,?,?,?,?,?,?)",
                    (i + 1, "alice", "work", "session-1", "user",
                     f"work fact {i}", f"2026-09-29T00:0{i}:00Z", "2026-09-29"))
    for i in range(4):
        con.execute("INSERT INTO verbatim_turns VALUES (?,?,?,?,?,?,?,?)",
                    (i + 11, "bob", "work", "session-1", "user",
                     f"BOB PRIVATE {i}", "2026-09-29", "2026-09-29"))
    for i in range(2):
        con.execute("INSERT INTO verbatim_turns VALUES (?,?,?,?,?,?,?,?)",
                    (i + 21, "alice", "home", "session-1", "user",
                     f"HOME PRIVATE {i}", "2026-09-29", "2026-09-29"))
    con.commit()
    con.close()

    out = distill.distill_session("session-1", user_id="alice", bank_id="work")
    assert out["status"] == "ok" and out["source_count"] == 3
    assert "BOB PRIVATE" not in out["summary"] and "HOME PRIVATE" not in out["summary"]
    short = distill.distill_session("session-1", user_id="alice", bank_id="home")
    assert short["status"] == "skipped" and short["reason"] == "too_short"
