"""f0.8+ ④ cleanup scripts: wash, reasoning rows, backup repack, CC transcript sweep, config beside bundle."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import tarfile
import time
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "scripts" / "f08p_data_cleanup.py"
_spec = importlib.util.spec_from_file_location("f08p_data_cleanup", _SRC)
D = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(D)

SECRET = "api_key: sk-proj-AbCdEf1234567890XYZabcdef"
RAW = b"sk-proj-AbCdEf1234567890XYZabcdef"


def _facts(path: Path) -> None:
    c = sqlite3.connect(str(path))
    c.execute("CREATE TABLE reflections (id INTEGER PRIMARY KEY, user_id TEXT, content TEXT, evidence TEXT)")
    c.execute("CREATE TABLE memory_edits (edit_id INTEGER PRIMARY KEY, old_content TEXT, new_content TEXT, reason TEXT)")
    c.execute("INSERT INTO reflections (user_id, content, evidence) VALUES ('u', ?, '[]')", (f"洞察里有 {SECRET}",))
    c.execute("INSERT INTO reflections (user_id, content, evidence) VALUES ('u', '干净的洞察', '[]')")
    c.execute("INSERT INTO memory_edits (old_content, new_content, reason) VALUES (?, 'x', '')", (SECRET,))
    c.commit()
    c.close()


def test_wash_dry_run_counts_and_apply_backs_up_then_redacts(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    _facts(data / "facts.db")
    dry = D.wash(data, apply=False, backup_dir=None)
    assert dry["tables"]["reflections"] == {"rows": 2, "would_change": 1}
    assert dry["tables"]["memory_edits"]["would_change"] == 1
    assert RAW in (data / "facts.db").read_bytes()          # dry run changed nothing
    rep = D.wash(data, apply=True, backup_dir=tmp_path / "bk")
    assert Path(rep["backup"]).exists() and oct(Path(rep["backup"]).stat().st_mode & 0o777) == "0o600"
    c = sqlite3.connect(str(data / "facts.db"))
    assert "[redacted:api_key]" in c.execute("SELECT content FROM reflections WHERE id=1").fetchone()[0]
    assert c.execute("SELECT content FROM reflections WHERE id=2").fetchone()[0] == "干净的洞察"


def test_reasoning_rows_only_from_derived_summaries(tmp_path):
    from ducky.memory_kernel.impl_native import connect
    data = tmp_path / "data"
    data.mkdir()
    m = connect(str(data / "memory.db"))
    text = "We need to output one or two sentences summarizing the conversation. Must be specific."
    for mid, md in ((1, {"kind": "session_distill"}), (2, {})):
        m.execute("INSERT INTO memories (mid,id,content,user_id,hash,metadata) VALUES (?,?,?,?,?,?)",
                  (mid, f"m{mid}", text, "u", f"h{mid}", json.dumps(md)))
    m.commit()
    m.close()
    rows = D.reasoning_rows(data)
    assert [r["id"] for r in rows] == ["m1"]


def _backup_tree(root: Path) -> Path:
    b = root / "pre-old-20261020_000000"
    b.mkdir(parents=True)
    c = sqlite3.connect(str(b / "memory.db"))
    c.executescript("""
      CREATE TABLE memories (mid INTEGER PRIMARY KEY, content TEXT, metadata TEXT);
      CREATE VIRTUAL TABLE memories_fts USING fts5(content, content=memories, content_rowid=mid);
    """)
    c.execute("INSERT INTO memories VALUES (1, ?, ?)", (f"记住 {SECRET}", json.dumps({"note": SECRET})))
    c.execute("INSERT INTO memories_fts(rowid, content) SELECT mid, content FROM memories")
    c.commit()
    c.close()
    (b / "state.json").write_text(json.dumps({"k": SECRET}), encoding="utf-8")
    (b / "mem0_config_local.json").write_text(json.dumps({"api_key": "keep-me-sk-123456789012345678"}))
    inner = root.parent / "stage"
    inner.mkdir()
    (inner / "notes.md").write_text(f"笔记 {SECRET}\n", encoding="utf-8")
    with tarfile.open(b / "conf.tgz", "w:gz") as tf:
        tf.add(inner / "notes.md", arcname="notes.md")
    (b / "SHA256SUMS").write_text("stale  x\n")
    old = time.time() - 30 * 86400
    os.utime(b, (old, old))
    return b


def test_repack_redacts_in_place_keeps_secret_files_and_reseals(tmp_path):
    root = tmp_path / "backups"
    b = _backup_tree(root)
    (root / "recent-keep").mkdir()
    dry = D.repack(root, apply=False, keep_days=7, keep=[])
    assert dry["skipped_recent"] == ["recent-keep"]
    rep = dry["backups"][0]
    assert rep["sqlite_cells"] == 2 and rep["text_files_hit"] == 1 and rep["archives_hit"] == 1
    assert RAW in (b / "memory.db").read_bytes()
    D.repack(root, apply=True, keep_days=7, keep=[])
    os.utime(b, (time.time() - 30 * 86400,) * 2)
    for name in ("memory.db", "state.json"):
        assert RAW not in (b / name).read_bytes(), name
    with tarfile.open(b / "conf.tgz") as tf:
        assert RAW not in tf.extractfile("notes.md").read()
    assert b"keep-me" in (b / "mem0_config_local.json").read_bytes()   # restore needs it
    c = sqlite3.connect(str(b / "memory.db"))
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert c.execute("SELECT count(*) FROM memories_fts WHERE memories_fts MATCH 'redacted'").fetchone()[0] == 1
    sums = dict(line.split("  ")[::-1] for line in (b / "SHA256SUMS").read_text().splitlines())
    assert sums["memory.db"] == hashlib.sha256((b / "memory.db").read_bytes()).hexdigest()
    again = D.repack(root, apply=False, keep_days=7, keep=[])
    assert again["backups"][0]["sqlite_cells"] == 0 and again["backups"][0]["archives_hit"] == 0


def test_repack_keep_pattern(tmp_path):
    root = tmp_path / "backups"
    _backup_tree(root)
    rep = D.repack(root, apply=False, keep_days=7, keep=[r"^pre-old"])
    assert rep["skipped_keep"] == ["pre-old-20261020_000000"] and rep["backups"] == []


def test_transcripts_retention_redaction_and_api_request_stub(tmp_path):
    root = tmp_path / "projects" / "p"
    root.mkdir(parents=True)
    now = time.time()
    old = root / "old.jsonl"
    old.write_text('{"type":"user"}\n')
    os.utime(old, (now - 40 * 86400,) * 2)
    cur = root / "cur.jsonl"
    old_ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 10 * 86400))
    cur.write_text("\n".join([
        json.dumps({"type": "user", "message": {"content": f"我的 {SECRET}"}}),
        json.dumps({"type": "api-request", "timestamp": old_ts, "body": {"messages": ["x" * 500]}}),
        json.dumps({"type": "assistant", "message": {"content": "好的"}}),
    ]) + "\n")
    mt = now - 2 * 86400
    os.utime(cur, (mt, mt))
    live = root / "live.jsonl"
    live.write_text(json.dumps({"m": SECRET}) + "\n")
    dry = D.transcripts(tmp_path / "projects", apply=False, retain_days=30, api_request_days=7)
    assert (dry["files"], dry["expired_files"], dry["redacted_lines"], dry["api_request_stubbed"],
            dry["skipped_live"]) == (3, 1, 1, 1, 1)
    assert old.exists() and RAW in cur.read_bytes()
    D.transcripts(tmp_path / "projects", apply=True, retain_days=30, api_request_days=7)
    assert not old.exists() and RAW not in cur.read_bytes() and RAW in live.read_bytes()
    lines = [json.loads(x) for x in cur.read_text().splitlines()]
    assert lines[1] == {"type": "api-request", "timestamp": old_ts, "_pruned": "aidumei-f08p"}
    assert lines[2]["message"]["content"] == "好的"
    assert abs(cur.stat().st_mtime - mt) < 1        # age kept for the retention clock


def test_backup_gate_packs_config_beside_bundle(tmp_path):
    import subprocess
    gate = Path(__file__).resolve().parents[1] / "scripts" / "backup_gate.sh"
    data = tmp_path / "data"
    data.mkdir()
    sqlite3.connect(str(data / "facts.db")).execute("CREATE TABLE t(x)").connection.commit()
    cfg = tmp_path / "mem0_config_local.json"
    cfg.write_text('{"llm": {}}')
    base = os.environ.get("AIDUMEI_TEST_BACKUP_HOME") or str(Path.home())
    root = Path(base) / ".aidumem_test_backups" / ("cfg-" + tmp_path.name)
    try:
        env = dict(os.environ, AIDUMEM_DATA_DIR=str(data), AIDUMEM_BACKUP_ROOT=str(root),
                   AIDUMEM_CONFIG_FILE=str(cfg))
        p = subprocess.run(["bash", str(gate), "create", "cfgtest"], capture_output=True, text=True, env=env)
        assert p.returncode == 0, p.stderr
        tgz = next(root.glob("pre-cfgtest-*.config.tgz"))
        assert oct(tgz.stat().st_mode & 0o777) == "0o600"
        with tarfile.open(tgz) as tf:
            assert tf.getnames() == ["mem0_config_local.json"]
        v = subprocess.run(["bash", str(gate), "verify", "latest"], capture_output=True, text=True, env=env)
        assert v.returncode == 0, v.stderr          # bundle membership unchanged
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)
