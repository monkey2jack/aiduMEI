"""Bearer 搜索调用必须声明与目标一致的 caller_user_id。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from benchmarks.adapter import AiduMEIBenchmarkAdapter


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "f02-caller-test-token"
USER = "f02-nondefault-user"


@contextmanager
def _gated_server():
    """按真实授权边界处理 /search，并记录三个客户端实际发出的 body。"""
    calls: list[dict] = []
    stored: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            auth = self.headers.get("Authorization", "")
            calls.append({"path": self.path, "body": body, "auth": auth})

            if auth != f"Bearer {TOKEN}":
                status, response = 401, {"detail": "missing bearer"}
            elif self.path == "/search" and body.get("caller_user_id") != body.get("user_id"):
                status, response = 403, {"detail": "caller must own target hall"}
            elif self.path == "/add":
                stored[body["user_id"]] = body["messages"][0]["content"]
                status, response = 200, {"status": "ok"}
            elif self.path == "/search":
                memory = stored.get(body["user_id"])
                status, response = 200, {
                    "status": "ok",
                    "results": [{"memory": memory}] if memory else [],
                }
            elif self.path == "/delete_all":
                stored.pop(body["user_id"], None)
                status, response = 200, {"status": "ok"}
            else:
                status, response = 404, {"detail": "unknown path"}

            data = json.dumps(response).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", calls
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _hook_env(tmp_path: Path, base_url: str) -> dict[str, str]:
    env_file = tmp_path / "service.env"
    env_file.write_text(
        f"AIDUMEM_API_TOKEN={TOKEN}\nAIDUMEM_USER_ID={USER}\n",
        encoding="utf-8",
    )
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path / "empty-home"),
        "AIDUMEM_ENV_FILE": str(env_file),
        "AIDUMEM_DATA_DIR": str(tmp_path / "data"),
        "AIDUMEM_LOG_DIR": str(tmp_path / "logs"),
        "AIDUMEM_URL": base_url,
    }


def test_bearer_search_guard_rejects_an_undeclared_caller():
    """负向对照：服务确实会拒绝旧形态，避免客户端测试假绿。"""
    with _gated_server() as (base_url, calls):
        request = urllib.request.Request(
            base_url + "/search",
            data=json.dumps({"query": "x", "user_id": USER}).encode("utf-8"),
            headers={"Authorization": f"Bearer {TOKEN}",
                     "Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request, timeout=5)
        assert exc.value.code == 403
        assert calls[0]["body"]["user_id"] == USER


def test_benchmark_search_declares_the_case_owner(monkeypatch):
    monkeypatch.setenv("AIDUMEM_API_TOKEN", TOKEN)
    with _gated_server() as (base_url, calls):
        adapter = AiduMEIBenchmarkAdapter(base_url, timeout=5, max_retries=0)
        adapter.reset_case("f02", "nondefault-case")
        result = adapter.search("nondefault-case", "query")

    sent = next(call for call in calls if call["path"] == "/search")
    assert sent["auth"] == f"Bearer {TOKEN}"
    assert sent["body"]["user_id"].startswith("bench-f02-")
    assert sent["body"]["caller_user_id"] == sent["body"]["user_id"]
    assert result["results"] == []


def test_claude_code_search_declares_env_file_user(tmp_path):
    hook = ROOT / "integrations/cursor-hook/claude-code-hook.py"
    with _gated_server() as (base_url, calls):
        proc = subprocess.run(
            [sys.executable, str(hook), "search", "query"],
            env=_hook_env(tmp_path, base_url),
            text=True, capture_output=True, timeout=10,
        )

    assert proc.returncode == 0, proc.stderr
    search = next(call for call in calls if call["path"] == "/search")
    assert search["auth"] == f"Bearer {TOKEN}"
    assert search["body"]["user_id"] == USER
    assert search["body"]["caller_user_id"] == USER


def test_claude_code_stop_selftest_reads_its_own_nondefault_user(tmp_path):
    hook = ROOT / "integrations/cursor-hook/claude-code-stop-hook.py"
    with _gated_server() as (base_url, calls):
        proc = subprocess.run(
            [sys.executable, str(hook), "--selftest"],
            env=_hook_env(tmp_path, base_url),
            text=True, capture_output=True, timeout=10,
        )

    assert proc.returncode == 0, proc.stderr
    assert "selftest OK" in proc.stdout
    assert [call["path"] for call in calls] == ["/add", "/search"]
    search = calls[-1]
    assert search["auth"] == f"Bearer {TOKEN}"
    assert search["body"]["user_id"] == USER
    assert search["body"]["caller_user_id"] == USER
