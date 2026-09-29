"""The post-LLM hook must inspect the application receipt inside HTTP 200."""
from __future__ import annotations

import json
import os
import subprocess
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


_HOOK = Path(__file__).resolve().parents[1] / "integrations/aidumem-ingest.sh"


@contextmanager
def _receipt_server(receipt):
    paths = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            paths.append(self.path)
            if self.path == "/search":
                self.send_error(500, "selftest must stop before search")
                return
            payload = json.dumps(receipt).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv.server_port, paths
    finally:
        srv.shutdown()
        thread.join(timeout=5)
        srv.server_close()


@contextmanager
def _selftest_server(add_receipt, *, readable=True, cleanup_receipt=None):
    calls = []
    state = {"marker": ""}
    cleanup_receipt = cleanup_receipt or {"status": "committed"}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            calls.append((self.path, body))
            if self.path == "/add":
                state["marker"] = body["messages"][0]["content"]
                payload = add_receipt
            elif self.path == "/search":
                payload = {"results": [{"memory": state["marker"]}]} if readable else {"results": []}
            elif self.path == "/delete_all":
                payload = cleanup_receipt
            else:
                self.send_error(404)
                return
            raw = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv.server_port, calls
    finally:
        srv.shutdown()
        thread.join(timeout=5)
        srv.server_close()


def _env(tmp_path, port):
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(tmp_path),
            "AIDUMEM_DATA_DIR": str(tmp_path / "data"),
            "AIDUMEM_LOG_DIR": str(tmp_path / "logs"),
            "AIDUMEM_URL": f"http://127.0.0.1:{port}",
            "AIDUMEM_API_TOKEN": "test-token",
            "AIDUMEM_USER_ID": "alice", "AIDUMEI_BANK_ID": "work",
            "AIDUMEI_INGEST_TIMEOUT": "2"}


@pytest.mark.parametrize("receipt, expected", [
    ({"status": "error", "detail": "fake failure"}, "write_unconfirmed"),
    ({"status": "accepted", "durable": False, "job_id": "queued-1"}, "queued"),
    ({"status": "ok"}, ""),
])
def test_normal_hook_reports_business_receipt(tmp_path, receipt, expected):
    payload = {"session_id": "session-1",
               "extra": {"user_message": "A substantive user message",
                         "assistant_response": "A useful assistant response"}}
    with _receipt_server(receipt) as (port, paths):
        run = subprocess.run(["bash", str(_HOOK)], input=json.dumps(payload),
                             text=True, capture_output=True, env=_env(tmp_path, port),
                             timeout=5)

    assert run.returncode == 0 and run.stdout.strip() == "{}"
    assert paths == ["/add"]
    assert expected in run.stderr if expected else not run.stderr


def test_selftest_rejects_http_200_business_error_before_readback(tmp_path):
    with _selftest_server({"status": "error", "detail": "fake failure"}) as (port, calls):
        run = subprocess.run(["bash", str(_HOOK), "--selftest"],
                             text=True, capture_output=True, env=_env(tmp_path, port),
                             timeout=5)

    assert run.returncode == 5
    assert "write_unconfirmed" in run.stderr
    assert [path for path, _ in calls] == ["/add", "/delete_all"]
    assert calls[0][1]["user_id"] == calls[1][1]["user_id"]
    assert calls[1][1]["bank_id"] == "default"
    assert calls[1][1]["confirm"] is True


def test_selftest_uses_unique_user_and_cleans_only_that_scope(tmp_path):
    test_users = []
    for _ in range(2):
        with _selftest_server({"status": "ok"}) as (port, calls):
            run = subprocess.run(["bash", str(_HOOK), "--selftest"],
                                 text=True, capture_output=True, env=_env(tmp_path, port),
                                 timeout=5)
        assert run.returncode == 0, run.stderr
        assert [path for path, _ in calls] == ["/add", "/search", "/delete_all"]
        add, search, cleanup = [body for _, body in calls]
        test_user = add["user_id"]
        test_users.append(test_user)
        assert test_user.startswith("aidumei-ingest-selftest-")
        assert test_user != "alice"
        assert test_user not in run.stdout + run.stderr
        assert "alice" not in run.stdout + run.stderr
        assert f"127.0.0.1:{port}" not in run.stdout + run.stderr
        assert all(body["user_id"] == test_user and body["bank_id"] == "default"
                   for body in (add, search, cleanup))
        assert search["caller_user_id"] == test_user
        assert add["async_mode"] is False
        assert add["metadata"]["force_sync"] is True
        assert cleanup["confirm"] is True
    assert test_users[0] != test_users[1]


def test_selftest_cleans_after_readback_failure(tmp_path):
    with _selftest_server({"status": "ok"}, readable=False) as (port, calls):
        run = subprocess.run(["bash", str(_HOOK), "--selftest"],
                             text=True, capture_output=True, env=_env(tmp_path, port),
                             timeout=5)

    assert run.returncode == 6
    assert "write_not_readable" in run.stderr
    assert [path for path, _ in calls] == ["/add", "/search", "/delete_all"]
    assert calls[-1][1]["user_id"] == calls[0][1]["user_id"]


def test_selftest_cleanup_failure_is_failure(tmp_path):
    with _selftest_server({"status": "ok"},
                          cleanup_receipt={"status": "partial"}) as (port, calls):
        run = subprocess.run(["bash", str(_HOOK), "--selftest"],
                             text=True, capture_output=True, env=_env(tmp_path, port),
                             timeout=5)

    assert run.returncode == 7
    assert "cleanup_unconfirmed" in run.stderr
    assert [path for path, _ in calls] == ["/add", "/search", "/delete_all"]
    assert calls[0][1]["user_id"] not in run.stdout + run.stderr
    assert "alice" not in run.stdout + run.stderr
