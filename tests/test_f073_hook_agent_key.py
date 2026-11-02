"""f0.7.3 · one key per agent, wired into the hooks.

Two agents on one host share the same hook scripts and the same .env; before
f0.7.3 every hook sent the .env main token, so the server could not tell the
agents apart.  Now a hook that resolves identity ``<uid>`` and finds
``$AIDUMEI_KEYS_DIR/<uid>.key`` (default ``~/.aidumei-keys``) sends that key.
The key is read from the file at run time — never from the command line.

Asserted on the wire (stub server), for the three Claude Code lines
(read / write / distill), the same way test_v19_4_2_identity_env_fallback does.
"""
import json
import os
import pathlib
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

_INTEGRATIONS = pathlib.Path(__file__).resolve().parents[1] / "integrations"
HOOKS = {
    "cc-inject": (_INTEGRATIONS / "claude-code" / "aidumem-cc-inject.sh", "/search"),
    "cc-ingest": (_INTEGRATIONS / "claude-code" / "aidumem-cc-ingest.sh", "/search"),
    "distill": (_INTEGRATIONS / "aidumem-distill.sh", "/session/distill"),
}


class _Capture(BaseHTTPRequestHandler):
    captured: list = []

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        type(self).captured.append({"path": self.path, "auth": self.headers.get("Authorization")})
        body = b'{"results": [], "status": "ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def stub():
    handler = type("H", (_Capture,), {"captured": []})
    srv = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", handler
    srv.shutdown()
    srv.server_close()


def _run(hook, tmp_path, url, uid, keys_dir=None):
    envf = tmp_path / "deploy" / ".env"
    envf.parent.mkdir(parents=True, exist_ok=True)
    envf.write_text('AIDUMEM_API_TOKEN="main-token"\n')
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path / "home"),
           "AIDUMEM_ENV_FILE": str(envf), "AIDUMEM_URL": url, "AIDUMEM_USER_ID": uid,
           "AIDUMEM_DATA_DIR": str(tmp_path / "_d"), "AIDUMEM_LOG_DIR": str(tmp_path / "_l"),
           "AIDUMEI_DISTILL_FOREGROUND": "1"}
    if keys_dir is not None:
        env["AIDUMEI_KEYS_DIR"] = str(keys_dir)
    return subprocess.run(["bash", str(hook), "--selftest"], env=env, capture_output=True,
                          text=True, timeout=40)


def _key(dirpath, name, value):
    dirpath.mkdir(parents=True, exist_ok=True, mode=0o700)
    p = dirpath / f"{name}.key"
    p.write_text(value + "\n")
    p.chmod(0o600)


@pytest.mark.parametrize("line", sorted(HOOKS))
def test_identity_key_file_is_sent_instead_of_main_token(line, tmp_path, stub):
    url, handler = stub
    hook, endpoint = HOOKS[line]
    _key(tmp_path / "home" / ".aidumei-keys", "helper", "amk_helper_key")   # default dir
    proc = _run(hook, tmp_path, url, "helper")
    assert handler.captured, proc.stderr[-500:]
    sent = handler.captured[-1]
    assert sent["path"].startswith(endpoint)
    assert sent["auth"] == "Bearer amk_helper_key"
    assert "amk_helper_key" not in proc.stdout + proc.stderr     # never echoed


@pytest.mark.parametrize("line", sorted(HOOKS))
def test_no_key_file_keeps_main_token(line, tmp_path, stub):
    url, handler = stub
    hook, _ = HOOKS[line]
    _key(tmp_path / "keys", "helper", "amk_helper_key")
    _run(hook, tmp_path, url, "owner", keys_dir=tmp_path / "keys")
    assert handler.captured and handler.captured[-1]["auth"] == "Bearer main-token"


@pytest.mark.parametrize("line", sorted(HOOKS))
def test_identity_cannot_walk_out_of_the_keys_dir(line, tmp_path, stub):
    url, handler = stub
    hook, _ = HOOKS[line]
    (tmp_path / "secret.key").write_text("amk_outside\n")
    (tmp_path / "keys").mkdir()
    _run(hook, tmp_path, url, "../secret", keys_dir=tmp_path / "keys")
    assert handler.captured and handler.captured[-1]["auth"] == "Bearer main-token"


def test_hooks_read_the_key_file_not_the_command_line():
    for hook, _ in HOOKS.values():
        src = hook.read_text(encoding="utf-8")
        assert '"$_kdir/$_uid.key"' in src and "AIDUMEI_KEYS_DIR" in src
        assert json.dumps("amk_") not in src
