"""Durable debt must survive deployment and be visible before writers start."""
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("pending,failed,integrity", [(0, 0, "ok"), (1, 0, "ok"), (0, 1, "ok"), (None, None, "unknown")])
def test_wal_health_requires_repair_even_with_valid_integrity(monkeypatch, pending, failed, integrity):
    from ducky.wal_engine import WALEngine
    from ducky.hot.health import _wal_recovery_probe
    needs_attention = integrity != "ok" or bool(pending or failed)
    monkeypatch.setattr(WALEngine, "recovery_status", lambda _: {
        "integrity": integrity, "pending": pending, "failed": failed,
        "needs_attention": needs_attention, "operations": [],
    })
    probe = _wal_recovery_probe()
    assert probe["wal_engine_ok"] is (not needs_attention)
    assert probe["wal_pending_entries"] == pending
    assert probe["wal_failed_entries"] == failed


@pytest.mark.parametrize("state,healthy", [
    ({"status": "ok", "integrity": "not_checked", "repair_required": 0}, True),
    ({"status": "degraded", "repair_required": 2}, False),
    ({"status": "degraded", "error": "DatabaseError"}, False),
])
def test_health_reports_debt_and_unknown_without_false_zero(monkeypatch, state, healthy):
    from ducky import mutation_journal
    from ducky.hot.health import _mutation_journal_probe
    monkeypatch.setattr(mutation_journal, "journal_health", lambda: state)
    probe = _mutation_journal_probe()
    assert probe["mutation_journal_ok"] is healthy
    if "repair_required" not in state:
        assert "mutation_journal_repair_required" not in probe
        assert probe["mutation_journal_integrity"] == "unknown"
    assert probe["mutation_journal_automatic_replay"] is False


@pytest.mark.parametrize("corrupt", [False, True])
def test_recovery_runs_after_ownership_before_writers(monkeypatch, corrupt):
    import api_server
    from ducky import mutation_journal, process_lock
    from fastapi.testclient import TestClient
    events = []
    monkeypatch.setattr(process_lock, "acquire_api_process_lock", lambda _: events.append("lock"))
    def recover():
        events.append("recover")
        return {"status": "degraded", "integrity": "unknown" if corrupt else "ok",
                "repair_required": 1}
    monkeypatch.setattr(mutation_journal, "startup_recover", recover)
    monkeypatch.setattr(api_server, "_start_background", lambda: events.append("writers"))
    if corrupt:
        with pytest.raises(RuntimeError, match="journal startup integrity"):
            with TestClient(api_server.app):
                pytest.fail("corrupt journal admitted traffic")
        assert events == ["lock", "recover"]
    else:
        with TestClient(api_server.app):
            assert events == ["lock", "recover", "writers"]


def test_online_backup_includes_all_sqlite_suffixes_and_live_wal(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    connections = []
    for name in ("facts.db", "mutation_journal.sqlite3", "aux.sqlite"):
        conn = sqlite3.connect(data / name)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE evidence(body TEXT)")
        conn.execute("INSERT INTO evidence VALUES (?)", (name,))
        conn.commit()
        connections.append(conn)
        assert (data / (name + "-wal")).exists()
    home = Path(os.environ.get("AIDUMEI_TEST_BACKUP_HOME", Path.home()))
    home.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix=".aidumei-backup-", dir=home) as directory:
            env = dict(os.environ, AIDUMEM_DATA_DIR=str(data), AIDUMEM_BACKUP_ROOT=directory)
            def run(script, *args):
                return subprocess.run(["bash", str(ROOT / "scripts" / script), *args],
                                      env=env, capture_output=True, text=True, timeout=30)
            created = run("backup_gate.sh", "create", "journal")
            assert created.returncode == 0, created.stdout + created.stderr
            backup = next(Path(directory).glob("pre-journal-*"))
            for name in ("facts.db", "mutation_journal.sqlite3", "aux.sqlite"):
                with sqlite3.connect(backup / name) as conn:
                    assert conn.execute("SELECT body FROM evidence").fetchall() == [(name,)]
            assert not any(p.name.endswith(("-wal", "-shm", "-journal")) for p in backup.iterdir())
            verified = run("backup_gate.sh", "verify", str(backup))
            assert verified.returncode == 0, verified.stdout + verified.stderr
            restored = run("restore_gate.sh", "--dry-run", str(backup))
            assert restored.returncode == 0, restored.stdout + restored.stderr
            assert "db_count=3" in restored.stdout
            (backup / "mutation_journal.sqlite3").write_bytes(b"damaged")
            assert run("restore_gate.sh", "--dry-run", str(backup)).returncode != 0
    finally:
        for conn in connections:
            conn.close()
