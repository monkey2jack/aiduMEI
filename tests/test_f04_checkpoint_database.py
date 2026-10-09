"""Readiness must follow the actual SQLite store across database changes."""
import sqlite3


def test_checkpoint_schema_is_initialized_per_database(monkeypatch, tmp_path):
    from ducky import checkpoint
    current = [tmp_path / "first.db"]

    def connect():
        conn = sqlite3.connect(current[0])
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(checkpoint, "get_facts_conn", connect)
    monkeypatch.setattr(checkpoint, "_table_checked", False)
    checkpoint.write_checkpoint("first-session", {"cp_active_intent": "first content"}, user_id="alice")
    current[0] = tmp_path / "second.db"
    checkpoint.write_checkpoint("second-session", {"cp_active_intent": "second content"}, user_id="alice")
    assert checkpoint.get_latest_checkpoint("alice")["session_id"] == "second-session"
    assert checkpoint.get_latest_checkpoint("bob") is None
    current[0] = tmp_path / "first.db"
    assert checkpoint.get_latest_checkpoint("alice")["session_id"] == "first-session"
