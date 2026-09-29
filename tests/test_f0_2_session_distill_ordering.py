"""Committed session turns remain complete while semantic extraction lags."""
from __future__ import annotations

import sqlite3


def test_distill_uses_all_committed_raw_turns_when_sidecars_are_partial(
    tmp_path, monkeypatch,
):
    import ducky.llm_client as llm
    import ducky.salience.core as salience
    import ducky.session_distill as distill
    import ducky.utils as utils

    db = tmp_path / "facts.db"
    monkeypatch.setattr(utils, "FACTS_DB", str(db))
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE memory_epistemic (
            memory_ref TEXT, origin_session_id TEXT, origin_turn INTEGER,
            created_at TEXT, user_id TEXT, bank_id TEXT, origin_agent TEXT
        );
        CREATE TABLE verbatim_turns (
            id INTEGER PRIMARY KEY, user_id TEXT, bank_id TEXT, session_id TEXT,
            role TEXT, content TEXT, recorded_at TEXT, created_at TEXT
        );
    """)
    for turn in range(1, 6):
        conn.execute("INSERT INTO verbatim_turns VALUES (?,?,?,?,?,?,?,?)",
                     (turn, "alice", "work", "session-1", "user",
                      f"committed raw turn {turn}", "2026-09-29", "2026-09-29"))
    for turn in range(1, 4):
        conn.execute("INSERT INTO memory_epistemic VALUES (?,?,?,?,?,?,?)",
                     (f"ref-{turn}", "session-1", turn, "2026-09-29",
                      "alice", "work", "hermes"))
    conn.execute("INSERT INTO verbatim_turns VALUES (?,?,?,?,?,?,?,?)",
                 (20, "bob", "work", "session-1", "user",
                  "BOB PRIVATE", "2026-09-29", "2026-09-29"))
    conn.execute("INSERT INTO verbatim_turns VALUES (?,?,?,?,?,?,?,?)",
                 (21, "alice", "home", "session-1", "user",
                  "HOME PRIVATE", "2026-09-29", "2026-09-29"))
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        salience, "get_batch_salience_records",
        lambda refs: {f"ref-{turn}": {"content_preview": f"semantic turn {turn}"}
                      for turn in range(1, 4)},
    )
    seen = []
    monkeypatch.setattr(llm, "call_llm", lambda prompt, **kwargs: seen.append(prompt) or "summary")

    out = distill.distill_session("session-1", user_id="alice", bank_id="work")

    assert out["status"] == "ok"
    assert out["source_count"] == 5
    assert "committed raw turn 5" in seen[0]
    assert "BOB PRIVATE" not in seen[0]
    assert "HOME PRIVATE" not in seen[0]


def test_distill_uses_semantic_sources_when_no_raw_turns_exist(tmp_path, monkeypatch):
    import ducky.salience.core as salience
    import ducky.session_distill as distill
    import ducky.utils as utils

    db = tmp_path / "facts.db"
    monkeypatch.setattr(utils, "FACTS_DB", str(db))
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE memory_epistemic (
            memory_ref TEXT, origin_session_id TEXT, origin_turn INTEGER,
            created_at TEXT, user_id TEXT, bank_id TEXT, origin_agent TEXT
        );
        CREATE TABLE verbatim_turns (
            id INTEGER PRIMARY KEY, user_id TEXT, bank_id TEXT, session_id TEXT,
            role TEXT, content TEXT, recorded_at TEXT, created_at TEXT
        );
    """)
    for turn in range(1, 4):
        conn.execute("INSERT INTO memory_epistemic VALUES (?,?,?,?,?,?,?)",
                     (f"ref-{turn}", "session-1", turn, "2026-09-29",
                      "alice", "work", "hermes"))
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        salience, "get_batch_salience_records",
        lambda refs: {f"ref-{turn}": {"content_preview": f"semantic turn {turn}"}
                      for turn in range(1, 4)},
    )

    rows = distill.collect_session_memories("session-1", user_id="alice", bank_id="work")
    assert [row["text"] for row in rows] == [
        "semantic turn 1", "semantic turn 2", "semantic turn 3",
    ]


def test_bounded_raw_window_keeps_latest_turn_and_changes_generation(tmp_path, monkeypatch):
    import ducky.llm_client as llm
    import ducky.session_distill as distill
    import ducky.utils as utils

    db = tmp_path / "facts.db"
    monkeypatch.setattr(utils, "FACTS_DB", str(db))
    monkeypatch.setattr(distill, "MAX_SOURCE", 3)
    monkeypatch.setattr(llm, "call_llm", lambda *args, **kwargs: "summary")
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE verbatim_turns (
        id INTEGER PRIMARY KEY, user_id TEXT, bank_id TEXT, session_id TEXT,
        role TEXT, content TEXT, recorded_at TEXT, created_at TEXT)""")
    for turn in range(1, 4):
        conn.execute("INSERT INTO verbatim_turns VALUES (?,?,?,?,?,?,?,?)",
                     (turn, "alice", "work", "session-1", "user",
                      f"raw turn {turn}", "2026-09-29", "2026-09-29"))
    conn.commit()
    first = distill.distill_session("session-1", user_id="alice", bank_id="work")
    conn.execute("INSERT INTO verbatim_turns VALUES (?,?,?,?,?,?,?,?)",
                 (4, "alice", "work", "session-1", "user",
                  "raw turn 4", "2026-09-29", "2026-09-29"))
    conn.commit()
    conn.close()
    rows = distill.collect_session_memories("session-1", user_id="alice", bank_id="work")
    second = distill.distill_session("session-1", user_id="alice", bank_id="work")

    assert [row["text"] for row in rows] == ["raw turn 2", "raw turn 3", "raw turn 4"]
    assert first["source_count"] == second["source_count"] == 3
    assert first["source_fingerprint"] != second["source_fingerprint"]
