"""f0.3 C8: scripts/restore_tombstones.py -- selection, dry run, apply, receipt.

The tool must never open the vector store itself (the service holds the
embedded Qdrant lock), so it restores over HTTP.  Default is a dry run.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pathlib
import sqlite3
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "restore_tombstones_script", _ROOT / "scripts" / "restore_tombstones.py")
tool = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tool)

TOKEN = "tok-for-tests-only"
ROWS = [
    # tombstone_id, target_id, user, bank, reason, actor, tombstoned_at, restored_at
    (1, "11111111-1111-4111-8111-111111111111", "alice", "default", "cascade_delete",
     "wal_engine", "2026-09-20T18:30:05+00:00", None),
    (2, "22222222-2222-4222-8222-222222222222", "alice", "default", "cascade_delete",
     "wal_engine", "2026-09-20T20:02:40+00:00", None),
    (3, "33333333-3333-4333-8333-333333333333", "alice", "default", "cascade_delete",
     "wal_engine", "2026-09-20T12:00:00+00:00", None),            # outside the windows
    (4, "44444444-4444-4444-8444-444444444444", "alice", "default", "user_request",
     "alice", "2026-09-20T18:31:00+00:00", None),                 # other reason
    (5, "55555555-5555-4555-8555-555555555555", "alice", "default", "cascade_delete",
     "wal_engine", "2026-09-21T18:26:00+00:00", "2026-09-22T00:00:00+00:00"),  # restored
]
TEXT = "a deleted memory whose preview is longer than forty characters in total"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path_factory):
    """The shared credential chain reads os.environ and load_env_file() injects
    .env keys into it: snapshot and restore, and never consult the repo .env."""
    saved = dict(os.environ)
    empty = tmp_path_factory.mktemp("envchain") / "empty.env"
    empty.write_text("", encoding="utf-8")
    for key in ("AIDUMEM_API_TOKEN", "AIDUMEM_API_BASE"):
        os.environ.pop(key, None)
    os.environ["AIDUMEM_ENV_FILE"] = str(empty)
    yield
    for key in set(os.environ) - set(saved):
        del os.environ[key]
    os.environ.update(saved)


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("AIDUMEM_API_TOKEN", TOKEN)
    return TOKEN


def _listing(user, bank):
    return [{"tombstone_id": t, "target_id": target, "target_type": "memory",
             "user_id": u, "bank_id": b, "reason": reason, "actor": actor,
             "tombstoned_at": at, "restored_at": restored, "content_snapshot": TEXT}
            for t, target, u, b, reason, actor, at, restored in ROWS
            if (u, b) == (user, bank)]


@pytest.fixture
def server():
    state = {"requests": [], "restored": set(), "answers": {}}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, payload):
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            state["requests"].append(("GET", url.path, self.headers.get("Authorization"), None))
            qs = {k: v[0] for k, v in parse_qs(url.query).items()}
            rows = _listing(qs.get("user_id"), qs.get("bank_id"))
            for r in rows:
                if r["tombstone_id"] in state["restored"]:
                    r["restored_at"] = "2026-09-29T00:00:00+00:00"
            self._send(200, {"status": "ok", "results": rows})

        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            state["requests"].append(("POST", self.path, self.headers.get("Authorization"), body))
            tid = body["tombstone_id"]
            answer = state["answers"].get(tid) or {
                "status": "ok", "details": {"restored": True, "detail": "fts,vector",
                                            "layers": {"fts": "restored", "vector": "restored"},
                                            "verification": {"facts_row": None, "fts_row": True,
                                                             "vector_point": True}}}
            if answer["status"] == "ok":
                state["restored"].add(tid)
            self._send(200, answer)

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    state["base"] = f"http://127.0.0.1:{srv.server_port}"
    yield state
    srv.shutdown()
    thread.join(timeout=5)
    srv.server_close()


FILTERS = ["--user-id", "alice", "--reason", "cascade_delete",
           "--utc-windows", "18:25-18:40,19:55-20:10"]


def test_dry_run_prints_the_selection_and_changes_nothing(server, token, capsys):
    rc = tool.main(["--base", server["base"], *FILTERS])
    out = capsys.readouterr().out
    assert rc == 0
    assert "#1 " in out and "#2 " in out
    for excluded in ("#3 ", "#4 ", "#5 "):
        assert excluded not in out
    assert TEXT[:40] in out and TEXT[:41] not in out, "preview must be 40 characters"
    assert "selected 2 tombstone(s)" in out and "[dry-run]" in out
    assert [r for r in server["requests"] if r[0] == "POST"] == []
    assert all(r[2] == f"Bearer {TOKEN}" for r in server["requests"])
    assert TOKEN not in out


def test_apply_restores_each_in_its_own_scope_and_writes_a_receipt(server, token, tmp_path,
                                                                   capsys):
    receipt = tmp_path / "receipt.json"
    rc = tool.main(["--base", server["base"], *FILTERS, "--apply", "--receipt", str(receipt)])
    assert rc == 0, capsys.readouterr()
    posts = [r[3] for r in server["requests"] if r[0] == "POST"]
    assert posts == [{"tombstone_id": 1, "user_id": "alice", "bank_id": "default"},
                     {"tombstone_id": 2, "user_id": "alice", "bank_id": "default"}]
    data = json.loads(receipt.read_text())
    assert data["selected"] == 2 and data["restored"] == 2 and data["failed"] == 0
    assert all(r["ok"] and r["restored_at_confirmed"] for r in data["results"])
    assert TEXT not in receipt.read_text(), "the receipt must not copy deleted content"


def test_apply_fails_loudly_on_partial_or_unverified_restores(server, token, tmp_path):
    server["answers"][1] = {"status": "partial", "details": {
        "restored": False, "detail": "partial: vector failed",
        "layers": {"vector": "failed:RuntimeError"},
        "verification": {"vector_point": False}}}
    server["answers"][2] = {"status": "ok", "details": {
        "restored": True, "detail": "fts", "layers": {"fts": "restored"},
        "verification": {"fts_row": True, "vector_point": False}}}
    receipt = tmp_path / "receipt.json"
    rc = tool.main(["--base", server["base"], *FILTERS, "--apply", "--receipt", str(receipt)])
    assert rc == 1
    data = json.loads(receipt.read_text())
    assert data["failed"] == 2
    by_id = {r["tombstone_id"]: r for r in data["results"]}
    assert by_id[1]["status"] == "partial"
    assert "vector_point" in by_id[2]["error"]


def test_env_file_is_parsed_never_executed(server, tmp_path, capsys):
    pwned = tmp_path / "pwned"
    env_file = tmp_path / "prod.env"
    env_file.write_text(
        "# production env\n"
        f"export AIDUMEM_API_BASE={server['base']}\n"
        f"AIDUMEM_API_TOKEN=\"{TOKEN}\"\n"
        f"EVIL=$(touch {pwned})\n"
        f"`touch {pwned}`\n", encoding="utf-8")
    rc = tool.main(["--env-file", str(env_file), *FILTERS])
    out = capsys.readouterr().out
    assert rc == 0 and "selected 2" in out
    assert not pwned.exists(), ".env content was executed"
    assert os.environ.get("EVIL") == f"$(touch {pwned})", "value must be kept literally"
    assert all(r[2] == f"Bearer {TOKEN}" for r in server["requests"])
    assert TOKEN not in out


def test_environment_token_wins_over_the_env_file(server, tmp_path, monkeypatch):
    env_file = tmp_path / "prod.env"
    env_file.write_text(f"AIDUMEM_API_BASE={server['base']}\nAIDUMEM_API_TOKEN=from-file\n",
                        encoding="utf-8")
    monkeypatch.setenv("AIDUMEM_API_TOKEN", TOKEN)
    assert tool.main(["--env-file", str(env_file), *FILTERS]) == 0
    assert {r[2] for r in server["requests"]} == {f"Bearer {TOKEN}"}


def test_credentials_come_from_the_shared_source(server, token, monkeypatch):
    """Discriminating control: a private reader would still send TOKEN."""
    monkeypatch.setattr(tool, "api_auth_headers",
                        lambda: {"Authorization": "Bearer via-ducky-utils"})
    assert tool.main(["--base", server["base"], *FILTERS]) == 0
    assert {r[2] for r in server["requests"]} == {"Bearer via-ducky-utils"}


def test_missing_env_file_is_a_usage_error(tmp_path, capsys):
    rc = tool.main(["--env-file", str(tmp_path / "absent.env"), *FILTERS])
    assert rc == 2 and "env file not found" in capsys.readouterr().err


def test_no_token_anywhere_warns_and_sends_no_header(server, capsys):
    assert tool.main(["--base", server["base"], *FILTERS]) == 0
    assert "without credentials" in capsys.readouterr().err
    assert {r[2] for r in server["requests"]} == {None}


def _facts_db(path):
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE tombstones (
        tombstone_id INTEGER PRIMARY KEY, target_id TEXT, target_type TEXT, user_id TEXT,
        bank_id TEXT, content_snapshot TEXT, facts_snapshot TEXT, reason TEXT, actor TEXT,
        tombstoned_at TEXT, restored_at TEXT)""")
    for t, target, u, b, reason, actor, at, restored in ROWS:
        conn.execute("INSERT INTO tombstones VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (t, target, "memory", u, b, TEXT, "", reason, actor, at, restored))
    conn.execute("INSERT INTO tombstones VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (6, "66666666-6666-4666-8666-666666666666", "memory", "bob", "work", TEXT,
                  "", "cascade_delete", "wal_engine", "2026-09-20T18:35:00+00:00", None))
    conn.commit()
    conn.close()


def test_db_source_reads_every_scope_read_only(server, token, tmp_path, capsys):
    db = tmp_path / "facts.db"
    _facts_db(db)
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    rc = tool.main(["--base", server["base"], "--db", str(db), "--reason", "cascade_delete",
                    "--utc-windows", "18:25-18:40,19:55-20:10"])
    out = capsys.readouterr().out
    assert rc == 0 and "bob/work" in out and "alice/default" in out and "selected 3" in out
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["facts.db"], "sidecar files created"


@pytest.mark.parametrize("spec,stamp,inside", [
    ("18:25-18:40", "2026-09-20T18:40:59+00:00", True),
    ("18:25-18:40", "2026-09-20T18:41:00+00:00", False),
    ("23:50-00:10", "2026-09-20T00:05:00+00:00", True),     # wraps midnight
    ("23:50-00:10", "2026-09-20T12:00:00+00:00", False),
    ("18:25-18:40", "2026-09-21T02:30:00+08:00", True),     # converted to UTC first
])
def test_utc_windows(spec, stamp, inside):
    assert tool.in_windows(tool.parse_iso(stamp), tool.parse_windows(spec)) is inside


def test_since_until_and_include_restored(server, token, capsys):
    rc = tool.main(["--base", server["base"], "--user-id", "alice", "--since",
                    "2026-09-20T18:00:00Z", "--until", "2026-09-20T19:00:00", "--include-restored"])
    out = capsys.readouterr().out
    assert rc == 0 and "selected 2" in out and "#1 " in out and "#4 " in out


def test_bad_window_is_a_usage_error(server, token, capsys):
    rc = tool.main(["--base", server["base"], "--utc-windows", "25:00-26:00"])
    assert rc == 2 and "utc-windows" in capsys.readouterr().err


# -- end to end through the real restore route (TestClient transport) -------

class _Point:
    def __init__(self, pid, payload):
        self.id = pid
        self.payload = payload


class _Store:
    def __init__(self):
        self.points: dict = {}

    def insert(self, vectors, payloads=None, ids=None):
        for i, pid in enumerate(ids or []):
            self.points[str(pid)] = dict((payloads or [{}])[i])

    def get(self, vector_id):
        p = self.points.get(str(vector_id))
        return _Point(str(vector_id), dict(p)) if p is not None else None

    def delete(self, vector_id):
        self.points.pop(str(vector_id), None)


class _Embedder:
    def embed(self, text, memory_action=None):
        return [0.1, 0.2, 0.3]


class _Mem0:
    def __init__(self):
        self.vector_store = _Store()
        self.embedding_model = _Embedder()

    def get_all(self, *, filters=None, top_k=20, show_expired=False, **kwargs):
        out = []
        for pid, p in self.vector_store.points.items():
            if any(p.get(k) != v for k, v in (filters or {}).items()):
                continue
            out.append({"id": pid, "memory": p.get("data", ""), "user_id": p.get("user_id"),
                        "metadata": {k: v for k, v in p.items()
                                     if k not in ("data", "user_id", "hash")}})
        return {"results": out[:top_k]}

    def delete(self, memory_id):
        if str(memory_id) not in self.vector_store.points:
            raise ValueError(f"Memory with id {memory_id} not found")
        self.vector_store.delete(memory_id)


def test_end_to_end_restores_consolidator_deletes_through_the_service(tmp_path, monkeypatch):
    import ducky.hot.crud as crud
    import ducky.mem0_runtime as runtime
    import ducky.memory_types as mt
    import ducky.utils as utils
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ducky.schema_bootstrap import ensure_core_schema
    from ducky.text_fts import _index_memory, _init_text_fts
    from ducky.wal_engine import cascade_delete_memory

    monkeypatch.setattr(utils, "FACTS_DB", str(tmp_path / "facts.db"))
    monkeypatch.setattr(utils, "TEXT_FTS_DB", str(tmp_path / "text_fts.db"))
    monkeypatch.setattr(mt, "_checked", False)
    monkeypatch.setenv("AIDUMEI_ENGINE_MODE", "cloud")
    ensure_core_schema(force=True)
    _init_text_fts()
    fake = _Mem0()
    monkeypatch.setattr(runtime, "get_memory", lambda: fake)
    ids = []
    for text in ("first memory the consolidator removed", "second memory it removed"):
        pid = str(uuid.uuid4())
        fake.vector_store.insert([[0.1]], payloads=[{"data": text, "user_id": "alice",
                                                     "bank_id": "default"}], ids=[pid])
        _index_memory(pid, text, user_id="alice", bank_id="default")
        assert cascade_delete_memory(pid, user_id="alice")["status"] == "committed"
        ids.append(pid)
    assert fake.vector_store.points == {}

    app = FastAPI()
    crud.register_crud_routes(app)
    client = TestClient(app)

    def via_testclient(self, method, path, *, query=None, body=None):
        resp = client.request(method, path, params=query, json=body)
        return resp.json()

    monkeypatch.setattr(tool.Api, "_call", via_testclient)
    receipt = tmp_path / "receipt.json"
    rc = tool.main(["--base", "http://127.0.0.1:1", "--user-id", "alice",
                    "--reason", "cascade_delete", "--apply", "--receipt", str(receipt)])
    data = json.loads(receipt.read_text())
    assert rc == 0, data
    assert data["restored"] == 2
    assert set(fake.vector_store.points) == set(ids), "vector points not back"
