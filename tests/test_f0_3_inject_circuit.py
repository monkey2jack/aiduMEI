"""f0.3 remediation: the shell hooks must not trust shared state or leak the token.

H-1  aidumem-inject.sh read its circuit/fuse state from ${TMPDIR:-/tmp} and fed
     the raw file content to bash arithmetic, which evaluates expressions such
     as `a[$(cmd)]`: any local user who can write /tmp could run commands as
     the agent. The content is now validated (digits only, bounded) first.
probe The fuse probe hit the full /health (40-157 ms in production) under a
     0.2 s budget; it now hits the O(1) /livez.
argv The probe put `Authorization: Bearer <token>` on curl's command line,
     readable by every local user through ps. Header lines now go on stdin.

Every shell test runs under /bin/bash (3.2 on macOS) and the bash found on
PATH, with private TMPDIRs and fake binaries; nothing touches /tmp state.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_INTEGRATIONS = _ROOT / "integrations"
_HOOK = _INTEGRATIONS / "aidumem-inject.sh"
_STATE = (".aidumem_circuit_broken", ".aidumem_fuse_count")

_SHELLS = [pytest.param("/bin/bash", id="bin-bash")]
if shutil.which("bash"):
    _SHELLS.append(pytest.param(shutil.which("bash"), id="path-bash"))


@contextmanager
def _server(search_delay=0, search_payload=None):
    """Record method, path and Authorization of every request."""
    seen: list[tuple[str, str, str | None]] = []

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, payload):
            raw = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except BrokenPipeError:
                pass  # Expected when the explicit short-deadline control cancels.

        def do_GET(self):  # noqa: N802
            seen.append(("GET", self.path, self.headers.get("Authorization")))
            self._reply({"status": "ok"})

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            seen.append(("POST", self.path, self.headers.get("Authorization")))
            if self.path == "/search":
                time.sleep(search_delay)
                self._reply(search_payload or {"status": "ok", "results": []})
            elif self.path.startswith("/session/distill"):
                self._reply({"status": "ok", "summary": "A stretch worth keeping.",
                             "source_count": 3, "metadata": {}})
            else:
                self._reply({"status": "ok", "context": "remembered-context"})

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05},
                              daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", seen
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _env(tmp_path: Path, base: str, **extra: str) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path),
        "AIDUMEM_URL": base,
        "AIDUMEM_USER_ID": "alice", "AIDUMEI_BANK_ID": "work",
        "AIDUMEM_API_TOKEN": "", "AIDUMEM_HOOK_QUIET": "1",
        "AIDUMEM_MIN_HISTORY": "0", "AIDUMEM_TIMEOUT": "2",
        "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
        "AIDUMEM_DATA_DIR": str(tmp_path / "data"), "AIDUMEM_LOG_DIR": str(tmp_path / "logs"),
    }
    env.update(extra)
    return env


def _payload() -> str:
    return json.dumps({"session_id": "circuit-session",
                       "user_message": "Recall the rollout decision please",
                       "conversation_history": [{"role": "user", "content": "x"}] * 4})


def _run(shell: str, env: dict[str, str], script: Path = _HOOK, stdin: str | None = None):
    return subprocess.run([shell, str(script)], input=_payload() if stdin is None else stdin,
                          env=env, text=True, capture_output=True, timeout=20)


def _fake_bin(tmp_path: Path, names: tuple[str, ...], log: Path) -> Path:
    """Wrappers that log their argv, then run the real program."""
    fake = tmp_path / "fakebin"
    fake.mkdir(exist_ok=True)
    for name in names:
        real = sys.executable if name == "python3" else shutil.which(name)
        assert real, name
        wrapper = fake / name
        wrapper.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n" "--- {name}" "$@" >> "{log}"\n'
            f'exec "{real}" "$@"\n', encoding="utf-8")
        wrapper.chmod(0o755)
    return fake


@pytest.mark.parametrize("override", [None, "0.05"])
def test_cloud_decision_search_outlives_old_deadline_and_honors_override(tmp_path, override):
    response = {"status": "ok", "results": [{"memory": "slow-search-marker", "score": .8}]}
    with _server(search_delay=1.7, search_payload=response) as (base, seen):
        env = _env(tmp_path, base)
        env.pop("AIDUMEM_TIMEOUT")
        if override is not None:
            env["AIDUMEI_SEARCH_TIMEOUT"] = override
        result = _run("/bin/bash", env)
    assert result.returncode == 0
    assert any(path == "/search" for _, path, _ in seen)
    assert ("slow-search-marker" in result.stdout) == (override is None)
    assert "remembered-context" in result.stdout


@pytest.mark.parametrize("query,kind,padding,visible", [
    ("Give the exact wording of the rollout", "VERBATIM", 160, True),
    ("Summarize the rollout", "VERBATIM", 160, False),
    ("Give the exact wording of the rollout", "FACTS", 160, False),
    ("Give the exact wording of the rollout", "VERBATIM", 520, False),
])
def test_injected_source_and_quote_budget_reach_the_model(tmp_path, query, kind, padding, visible):
    marker = "source-evidence-tail"
    response = {"status": "ok", "results": [{"content": "x" * padding + marker,
                                               "memory_type": kind, "score": .8}]}
    payload = json.dumps({"session_id": "source-test", "user_message": query,
                          "conversation_history": [{"role": "user", "content": "x"}] * 4})
    with _server(search_payload=response) as (base, _):
        result = _run("/bin/bash", _env(tmp_path, base), stdin=payload)
    assert result.returncode == 0
    context = json.loads(result.stdout)["context"]
    assert f"[{kind}]" in context
    assert (marker in context) == visible
    assert ("[excerpt]" in context) == (not visible)
    assert "<memory>" in context


# ---------------------------------------------------------------------------
# H-1: state file content never reaches bash arithmetic
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("shell", _SHELLS)
def test_the_attack_class_is_real_on_this_bash(shell, tmp_path):
    """Control: the pattern the hook used to run does execute file content."""
    marker = tmp_path / "pwned"
    state = tmp_path / "state"
    state.write_text(f"a[$(touch {marker})]\n", encoding="utf-8")
    subprocess.run([shell, "-c", 'v=$(cat "$1"); echo $((v + 1))', "_", str(state)],
                   capture_output=True, text=True, timeout=10)
    assert marker.exists(), "control failed: bash did not evaluate the payload"


@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize("probe_up", [True, False], ids=["probe-ok", "probe-fails"])
def test_planted_state_is_not_evaluated_and_is_rewritten(shell, tmp_path, probe_up):
    marker = tmp_path / "pwned"
    payload = f"a[$(touch {marker})]\n"
    circuit, count = (tmp_path / name for name in _STATE)
    circuit.write_text(payload, encoding="utf-8")
    count.write_text(payload, encoding="utf-8")
    with _server() as (base, seen):
        url = base if probe_up else "http://127.0.0.1:9"   # discard port: refused
        result = _run(shell, _env(tmp_path, url))
    assert result.returncode == 0, result.stderr
    assert not marker.exists(), "state file content was executed"
    assert not circuit.exists() or circuit.read_text().strip().isdigit()
    assert count.read_text().strip() == ("0" if probe_up else "1")
    if probe_up:
        assert "remembered-context" in json.loads(result.stdout)["context"]
    else:
        assert json.loads(result.stdout) == {}


@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize("stored", ["99999999999", "1234567890123", "12 34", "-5", "0x10"])
def test_future_oversized_or_malformed_timestamps_do_not_hold_cooldown(shell, tmp_path, stored):
    circuit = tmp_path / _STATE[0]
    circuit.write_text(stored, encoding="utf-8")
    with _server() as (base, seen):
        result = _run(shell, _env(tmp_path, base))
    assert result.returncode == 0, result.stderr
    assert seen and seen[0][:2] == ("GET", "/livez"), "a bogus stamp kept the hook silent"
    assert not circuit.exists()


@pytest.mark.parametrize("shell", _SHELLS)
def test_valid_recent_stamp_still_cools_down(shell, tmp_path):
    """Control: validation must not break the cooldown itself."""
    (tmp_path / _STATE[0]).write_text(str(int(time.time())), encoding="utf-8")
    with _server() as (base, seen):
        result = _run(shell, _env(tmp_path, base))
    assert result.returncode == 0 and json.loads(result.stdout) == {}
    assert seen == [], "a cooling hook must not even probe"


@pytest.mark.parametrize("shell", _SHELLS)
def test_planted_symlink_is_not_written_through(shell, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("1\n", encoding="utf-8")
    (tmp_path / _STATE[1]).symlink_to(victim)
    result = _run(shell, _env(tmp_path, "http://127.0.0.1:9"))   # discard port: refused
    assert result.returncode == 0 and json.loads(result.stdout) == {}
    assert victim.read_text(encoding="utf-8") == "1\n"
    assert not (tmp_path / _STATE[0]).exists(), "a symlinked count must read as 0"


@pytest.mark.parametrize("shell", _SHELLS)
def test_planted_fifo_does_not_hang_the_hook(shell, tmp_path):
    (tmp_path / _STATE[1]).write_text("1\n", encoding="utf-8")   # next failure trips
    os.mkfifo(tmp_path / _STATE[0])
    started = time.monotonic()
    result = _run(shell, _env(tmp_path, "http://127.0.0.1:9"))
    assert time.monotonic() - started < 10
    assert result.returncode == 0 and json.loads(result.stdout) == {}
    assert (tmp_path / _STATE[1]).read_text().strip() == "2"


# ---------------------------------------------------------------------------
# probe: O(1) /livez, token on stdin rather than argv
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("shell", _SHELLS)
def test_probe_uses_livez_and_still_sends_the_token(shell, tmp_path):
    with _server() as (base, seen):
        result = _run(shell, _env(tmp_path, base, AIDUMEM_API_TOKEN="f03-probe-token"))
    assert result.returncode == 0, result.stderr
    assert seen[0] == ("GET", "/livez", "Bearer f03-probe-token")
    assert not any(path == "/health" for _, path, _ in seen)
    assert all(auth == "Bearer f03-probe-token" for _, _, auth in seen)


def _argv_log_after(shell: str, tmp_path: Path, script: Path, stdin: str, base: str,
                    token_via_env_file: bool) -> str:
    log = tmp_path / "argv.log"
    fake = _fake_bin(tmp_path, ("curl", "python3"), log)
    extra = {"PATH": f"{fake}:{os.environ.get('PATH', '')}",
             "AIDUMEM_DATA_DIR": str(tmp_path / "data"), "AIDUMEM_LOG_DIR": str(tmp_path / "logs")}
    if token_via_env_file:
        env_file = tmp_path / "hook.env"
        env_file.write_text("AIDUMEM_API_TOKEN=f03-argv-secret\n", encoding="utf-8")
        extra.update({"AIDUMEM_API_TOKEN": "", "AIDUMEM_ENV_FILE": str(env_file)})
    else:
        extra["AIDUMEM_API_TOKEN"] = "f03-argv-secret"
    result = _run(shell, _env(tmp_path, base, **extra), script=script, stdin=stdin)
    assert result.returncode == 0, result.stderr
    return log.read_text(encoding="utf-8") if log.exists() else ""


@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize("from_env_file", [False, True], ids=["env", "env-file"])
@pytest.mark.parametrize("hook,stdin", [
    ("aidumem-inject.sh", None),
    ("aidumem-ingest.sh", json.dumps({
        "session_id": "argv-session",
        "extra": {"user_message": "Please remember the rollout decision",
                  "assistant_response": "Noted.", "conversation_history": []}})),
    ("aidumem-distill.sh", json.dumps({"session_id": "argv-session"})),
])
def test_no_hook_puts_the_token_on_a_command_line(shell, tmp_path, from_env_file, hook, stdin):
    with _server() as (base, seen):
        argv = _argv_log_after(shell, tmp_path, _INTEGRATIONS / hook,
                               _payload() if stdin is None else stdin, base, from_env_file)
    assert argv, "argv logger never ran: the wrappers are not on the hook's PATH"
    assert "f03-argv-secret" not in argv
    # The token still reaches the service. The /livez probe runs before the
    # .env lookup and /livez is public, so only it may go without one.
    served = [auth for _, path, auth in seen if path != "/livez"]
    assert served and all(auth == "Bearer f03-argv-secret" for auth in served), seen
    if hook == "aidumem-inject.sh":
        assert "--- curl" in argv and "@-" in argv


@pytest.mark.parametrize("shell", _SHELLS)
def test_argv_check_has_discriminating_power(shell, tmp_path):
    """Control: the same logger does catch a token passed with -H on argv."""
    log = tmp_path / "argv.log"
    fake = _fake_bin(tmp_path, ("curl",), log)
    subprocess.run([shell, "-c", 'curl -s -m 1 -H "Authorization: Bearer $T" "$U" >/dev/null'],
                   env={"PATH": f"{fake}:{os.environ.get('PATH', '')}", "T": "f03-argv-secret",
                        "U": "http://127.0.0.1:9/livez",
                        "AIDUMEM_DATA_DIR": str(tmp_path / "data")},
                   capture_output=True, text=True, timeout=10)
    assert "f03-argv-secret" in log.read_text(encoding="utf-8")
