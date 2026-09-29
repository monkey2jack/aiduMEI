"""The shipped read hook must inject the selected user's selected bank."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


HOOK = Path(__file__).resolve().parents[1] / "integrations/aidumem-inject.sh"
STATE_NAMES = (".aidumem_circuit_broken", ".aidumem_fuse_count")


def test_shell_core_checkpoint_and_search_share_selected_scope(tmp_path):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.append((self.path, None))
            self._reply({"status": "ok", "health_status": "ok"})

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw) if raw else None
            seen.append((self.path, body))
            endpoint = urlsplit(self.path).path
            if endpoint == "/search":
                self._reply({"status": "ok", "results": []})
                return
            query = parse_qs(urlsplit(self.path).query)
            own = (query.get("user_id") == ["alice"] and query.get("bank_id") == ["work"]
                   and query.get("caller_user_id") == ["alice"])
            name = "core" if endpoint == "/api/core-memory/inject" else "checkpoint"
            self._reply({"status": "ok", "context": f"{name}:alice/work" if own
                         else "DEFAULT_PRIVATE_SECRET"})

        def _reply(self, payload):
            raw = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env_file = tmp_path / ".env"
    env_file.write_text("AIDUMEM_USER_ID=alice\nAIDUMEI_BANK_ID=work\n", encoding="utf-8")
    env = dict(os.environ)
    for key in ("AIDUMEM_USER_ID", "AIDUMEM_DEFAULT_USER_ID", "AIDUMEI_BANK_ID"):
        env.pop(key, None)
    env.update({"AIDUMEM_URL": f"http://127.0.0.1:{server.server_port}",
                "TMPDIR": str(tmp_path),
                "AIDUMEM_ENV_FILE": str(env_file), "AIDUMEM_HOOK_QUIET": "1",
                "AIDUMEM_API_TOKEN": "", "AIDUMEM_MIN_HISTORY": "0",
                "AIDUMEM_NEW_SESSION_MAX": "8", "AIDUMEM_TIMEOUT": "2"})
    try:
        result = subprocess.run(
            ["bash", str(HOOK)],
            input=json.dumps({"session_id": "session-1",
                              "user_message": "Please recall the prior deployment decision",
                              "conversation_history": [{"role": "user", "content": "x"}] * 4}),
            env=env, text=True, capture_output=True, timeout=15,
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
    assert result.returncode == 0, result.stderr
    context = json.loads(result.stdout)["context"]
    assert "core:alice/work" in context and "checkpoint:alice/work" in context
    assert "DEFAULT_PRIVATE_SECRET" not in context
    search = next(body for path, body in seen if urlsplit(path).path == "/search")
    assert (search["user_id"], search["bank_id"], search["caller_user_id"]) == ("alice", "work", "alice")


@contextmanager
def _probe_server():
    """Record ordinary hook traffic; dropping health replies makes curl fail."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.append(("GET", self.path))
            if self.server.fail_health:
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            self._reply({"status": "ok"})

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            seen.append(("POST", self.path))
            payload = {"status": "ok", "results": []} if self.path == "/search" else {
                "status": "ok", "context": "private-hook-memory"}
            self._reply(payload)

        def _reply(self, payload):
            raw = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.fail_health = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _run_ordinary_hook(base, temp_dir):
    env = {
        "PATH": os.environ.get("PATH", ""), "HOME": str(temp_dir / "empty-home"),
        "TMPDIR": str(temp_dir), "AIDUMEM_URL": base,
        "AIDUMEM_DATA_DIR": str(temp_dir / "data"),
        "AIDUMEM_LOG_DIR": str(temp_dir / "logs"),
        "AIDUMEM_USER_ID": "alice", "AIDUMEI_BANK_ID": "work",
        "AIDUMEM_API_TOKEN": "f29-private-state-token", "AIDUMEM_HOOK_QUIET": "1",
        "AIDUMEM_MIN_HISTORY": "0", "AIDUMEM_TIMEOUT": "2",
    }
    result = subprocess.run(
        ["bash", str(HOOK)],
        input=json.dumps({"session_id": "private-state-session",
                          "user_message": "Recall the private deployment decision",
                          "conversation_history": [{"role": "user", "content": "x"}] * 4}),
        env=env, text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _shared_tmp_state():
    """Read-only baseline: tests must not change another hook's shared state."""
    state = {}
    for name in STATE_NAMES:
        path = Path("/tmp") / name
        state[name] = (path.read_bytes(), path.stat().st_mtime_ns) if path.exists() else None
    return state


def test_failed_probe_counts_only_in_private_tmpdir(tmp_path):
    before = _shared_tmp_state()
    circuit, count = (tmp_path / name for name in STATE_NAMES)
    with _probe_server() as (_, base, seen):
        assert _run_ordinary_hook(base, tmp_path) == {}
        assert seen == [("GET", "/health")]
        assert count.read_text().strip() == "1"
        assert not circuit.exists()
        assert _run_ordinary_hook(base, tmp_path) == {}
        assert seen == [("GET", "/health")] * 2
        assert count.read_text().strip() == "2"
        assert abs(int(time.time()) - int(circuit.read_text())) <= 2
    assert {path.name for path in tmp_path.iterdir()} == set(STATE_NAMES)
    assert _shared_tmp_state() == before


def test_private_cooldown_blocks_requests_until_it_expires(tmp_path):
    circuit, count = (tmp_path / name for name in STATE_NAMES)
    circuit.write_text(str(int(time.time())))
    count.write_text("2")
    with _probe_server() as (server, base, seen):
        server.fail_health = False
        assert _run_ordinary_hook(base, tmp_path) == {}
        assert seen == [], "a cooling hook must not even probe health"
        assert count.read_text() == "2"
        # Advance the stored timestamp instead of sleeping across the boundary.
        circuit.write_text(str(int(time.time()) - 5))
        assert "private-hook-memory" in _run_ordinary_hook(base, tmp_path)["context"]
        assert seen[0] == ("GET", "/health")
        assert any(method == "POST" and path == "/search" for method, path in seen)
        assert count.read_text().strip() == "0"
        assert not circuit.exists()


def test_two_private_tmpdirs_do_not_share_failure_or_cooldown(tmp_path):
    a, b = tmp_path / "space a", tmp_path / "space b"
    a.mkdir()
    b.mkdir()
    with _probe_server() as (server, base, seen):
        assert _run_ordinary_hook(base, a) == {}
        assert _run_ordinary_hook(base, b) == {}
        assert (a / STATE_NAMES[1]).read_text().strip() == "1"
        assert (b / STATE_NAMES[1]).read_text().strip() == "1"
        assert not (b / STATE_NAMES[0]).exists()
        assert _run_ordinary_hook(base, a) == {}
        assert (a / STATE_NAMES[1]).read_text().strip() == "2"
        assert (a / STATE_NAMES[0]).exists()
        server.fail_health = False
        assert "private-hook-memory" in _run_ordinary_hook(base, b)["context"]
        assert (b / STATE_NAMES[1]).read_text().strip() == "0"
        assert not (b / STATE_NAMES[0]).exists()
        calls_before = len(seen)
        assert _run_ordinary_hook(base, a) == {}
        assert len(seen) == calls_before, "space B recovery must not reopen space A"
        assert (a / STATE_NAMES[1]).read_text().strip() == "2"
