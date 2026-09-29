"""The stateless session-end hook retries one summary generation safely."""
from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


_HOOK = Path(__file__).resolve().parents[1] / "integrations/aidumem-distill.sh"


@pytest.mark.parametrize("paraphrased", [False, True])
def test_repeated_end_and_lost_response_reuse_one_write_key(tmp_path, paraphrased):
    requests = []
    committed = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            requests.append((self.path, body))
            if self.path.startswith("/session/distill"):
                n_distills = sum(path.startswith("/session/distill") for path, _ in requests)
                summary = ("Paraphrased insight" if paraphrased and n_distills == 2
                           else "One session insight")
                response = {"status": "ok", "summary": summary,
                            "mode": "llm", "source_count": 5,
                            "user_id": "alice", "bank_id": "work",
                            "metadata": {"kind": "session_distill"}}
            else:
                key = body.get("idempotency_key")
                if key not in committed:
                    committed[key] = body
                    # The server committed the request but the response was
                    # lost; a later session-end event must use the same key.
                    self.close_connection = True
                    return
                if body != committed[key]:
                    self.send_error(409, "idempotency payload conflict")
                    return
                response = {"status": "ok", "idempotency_replayed": True}
            payload = json.dumps(response).encode()
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
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": str(tmp_path),
           "AIDUMEM_DATA_DIR": str(tmp_path / "data"),
           "AIDUMEM_LOG_DIR": str(tmp_path / "logs"),
           "AIDUMEM_URL": f"http://127.0.0.1:{srv.server_port}",
           "AIDUMEM_API_TOKEN": "test-token",
           "AIDUMEM_USER_ID": "alice", "AIDUMEI_BANK_ID": "work",
           "AIDUMEI_DISTILL_TIMEOUT": "2"}
    try:
        runs = [subprocess.run(
            ["bash", str(_HOOK)], input=json.dumps({"session_id": "session-1"}),
            text=True, capture_output=True, env=env, timeout=5,
        ) for _ in range(2)]
    finally:
        srv.shutdown()
        thread.join(timeout=5)
        srv.server_close()

    assert all(run.returncode == 0 and run.stdout.strip() == "{}" for run in runs)
    add_bodies = [body for path, body in requests if path == "/add"]
    assert len(add_bodies) == 2
    keys = [body.get("idempotency_key") for body in add_bodies]
    assert keys[0] and keys[0] == keys[1]
    assert len(committed) == 1
    if paraphrased:
        assert "write_pending_or_conflict status=409" in runs[1].stderr
    else:
        assert "[aidumem-distill] ok " in runs[1].stderr


def test_async_acceptance_is_reported_as_queued_not_stored(tmp_path):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            if self.path.startswith("/session/distill"):
                response = {"status": "ok", "summary": "One session insight",
                            "source_count": 3, "user_id": "alice", "bank_id": "work"}
            else:
                response = {"status": "accepted", "durable": False,
                            "action": "async_queued", "job_id": "test-job"}
            payload = json.dumps(response).encode()
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
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": str(tmp_path),
           "AIDUMEM_DATA_DIR": str(tmp_path / "data"),
           "AIDUMEM_LOG_DIR": str(tmp_path / "logs"),
           "AIDUMEM_URL": f"http://127.0.0.1:{srv.server_port}",
           "AIDUMEM_API_TOKEN": "test-token",
           "AIDUMEM_USER_ID": "alice", "AIDUMEI_BANK_ID": "work",
           "AIDUMEI_DISTILL_TIMEOUT": "2"}
    try:
        run = subprocess.run(
            ["bash", str(_HOOK)], input=json.dumps({"session_id": "session-2"}),
            text=True, capture_output=True, env=env, timeout=5,
        )
    finally:
        srv.shutdown()
        thread.join(timeout=5)
        srv.server_close()

    assert run.returncode == 0 and run.stdout.strip() == "{}"
    assert "queued" in run.stderr
    assert "[aidumem-distill] ok " not in run.stderr
