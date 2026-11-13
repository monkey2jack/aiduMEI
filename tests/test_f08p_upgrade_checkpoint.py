"""A resumed upgrade must not repeat a completed or uncertain mutation."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from scripts import upgrade_checkpoint as checkpoint

SHA = "a" * 40


def test_successful_step_is_not_executed_twice(tmp_path):
    count, state = tmp_path / "count", tmp_path / "state.json"
    command = [sys.executable, "-c", "import pathlib,sys;p=pathlib.Path(sys.argv[1]);"
               "p.write_text(str(int(p.read_text())+1) if p.exists() else '1')", str(count)]
    assert checkpoint.run_step(state, SHA, "cutover", command)["status"] == "done"
    assert checkpoint.run_step(state, SHA, "cutover", command)["status"] == "already_done"
    assert count.read_text() == "1"
    assert state.stat().st_mode & 0o777 == 0o600


def test_failure_requires_inspection_before_retry(tmp_path):
    state = tmp_path / "state.json"
    command = [sys.executable, "-c", "raise SystemExit(7)"]
    assert checkpoint.run_step(state, SHA, "verify", command)["exit_code"] == 7
    assert checkpoint.run_step(state, SHA, "verify", command)["status"] == "inspection_required"
    checkpoint.run_step(state, SHA, "verify", command, retry=True)
    assert json.loads(state.read_text())["steps"]["verify"]["attempt"] == 2


def test_interrupted_process_does_not_blindly_retry(tmp_path):
    state = tmp_path / "state.json"
    command = [sys.executable, "-c", "import time;time.sleep(1)"]
    p = subprocess.Popen([sys.executable, str(Path(checkpoint.__file__)), "--state", str(state),
                          "--target-sha", SHA, "--step", "cutover", "--", *command],
                         stdout=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 5
        while not state.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert state.exists()
        p.terminate()
        p.wait(timeout=5)
        time.sleep(1.1)  # let the simulated external operation release inherited descriptors
        assert checkpoint.run_step(state, SHA, "cutover", command)["status"] == "inspection_required"
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()


def test_state_never_records_command_arguments(tmp_path):
    state = tmp_path / "state.json"
    command = [sys.executable, "-c", "pass", "private-argument"]
    checkpoint.run_step(state, SHA, "verify", command)
    assert "private-argument" not in state.read_text()
    assert os.stat(state).st_mode & 0o077 == 0
