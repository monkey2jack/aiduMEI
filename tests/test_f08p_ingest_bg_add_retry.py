"""f0.8+ C: the hooks' background /add has a sane, configurable budget and retries once."""
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = [ROOT / "integrations/claude-code/aidumem-cc-ingest.sh",
           ROOT / "integrations/codex/aidumem-codex-ingest.sh"]


def _add_snippet(path: Path) -> str:
    """The python -c '...' block that posts the background /add."""
    blocks = re.findall(r"python3 -c '\n(.*?)\n'", path.read_text(encoding="utf-8"), re.S)
    found = [b for b in blocks if 'url + "/add"' in b]
    assert len(found) == 1, path
    return found[0]


class _Stub:
    def __init__(self, plan):
        self.plan = list(plan)      # per request: (delay_s, status)
        self.seen = []
        self.seen_keys = []
        stub = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("content-length") or 0)
                stub.seen.append(json.loads(self.rfile.read(n) or b"{}"))
                stub.seen_keys.append(self.headers.get("Idempotency-Key"))
                delay, status = stub.plan.pop(0) if stub.plan else (0, 200)
                time.sleep(delay)
                try:
                    self.send_response(status)
                    self.end_headers()
                    self.wfile.write(b'{"status":"queued"}')
                except OSError:
                    pass

            def log_message(self, *a):
                pass
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def close(self):
        self.srv.shutdown()


def _run(script, stub, timeout_env):
    parsed = {"user_id": "u-test", "bank_id": "default", "session_id": "s-test",
              "messages": [{"role": "user", "content": "synthetic turn"}]}
    env = dict(os.environ, AIDUMEI_HOOK_PARSED=json.dumps(parsed), AIDUMEI_HOOK_TOKEN="",
               AIDUMEI_HOOK_URL=stub.url, AIDUMEI_INGEST_TIMEOUT=timeout_env)
    return subprocess.run([sys.executable, "-c", _add_snippet(script)], env=env,
                          capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.parent.name)
def test_timeout_is_retried_once_and_success_is_silent(script):
    stub = _Stub([(1.6, 200), (0, 200)])
    try:
        res = _run(script, stub, "1")
    finally:
        stub.close()
    assert len(stub.seen) == 2
    assert stub.seen_keys[0] and stub.seen_keys[0] == stub.seen_keys[1]
    assert stub.seen[0] == stub.seen[1]
    assert res.stderr == ""


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.parent.name)
def test_client_error_is_final_and_logged_once(script):
    stub = _Stub([(0, 400), (0, 200)])
    try:
        res = _run(script, stub, "5")
    finally:
        stub.close()
    assert len(stub.seen) == 1
    assert res.stderr.count("add_failed") == 1 and "HTTP400" in res.stderr


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.parent.name)
def test_bad_timeout_value_falls_back_to_default(script):
    stub = _Stub([(0, 200)])
    try:
        res = _run(script, stub, "not-a-number")
    finally:
        stub.close()
    assert len(stub.seen) == 1 and res.returncode == 0 and res.stderr == ""


def test_old_three_second_budget_is_gone():
    for script in SCRIPTS:
        assert "timeout=3)" not in _add_snippet(script)


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.parent.name)
def test_identical_words_in_distinct_turns_have_distinct_receipts(script):
    stub = _Stub([(0, 200), (0, 200)])
    try:
        _run(script, stub, "5")
        _run(script, stub, "5")
    finally:
        stub.close()
    assert len(stub.seen_keys) == 2
    assert stub.seen_keys[0] and stub.seen_keys[0] != stub.seen_keys[1]
