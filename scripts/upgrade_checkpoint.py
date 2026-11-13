#!/usr/bin/env python3
"""Record resumable upgrade steps without blindly repeating uncertain writes.

Run backup verification as a step before cutover. State belongs outside the
repository and data directory. A successful identical step is skipped. A failed
or interrupted step requires explicit inspection and --retry-uncertain; this
tool never restarts a service or rolls back data on its own.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from datetime import datetime, timezone

import fcntl


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _save(path: Path, state: dict) -> None:
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(state, out, indent=2)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def run_step(path: Path, target: str, name: str, command: list[str], *, retry=False) -> dict:
    path = path.resolve()
    if not re.fullmatch(r"[0-9a-f]{40}", target):
        raise ValueError("target must be a full commit SHA")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name) or not command:
        raise ValueError("a safe step name and command are required")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path.exists():
            state = json.loads(path.read_text())
            if state.get("target_sha") != target:
                raise ValueError("state belongs to a different target")
        else:
            state = {"target_sha": target, "created_at": _now(), "steps": {}}
        digest = hashlib.sha256(json.dumps(command).encode()).hexdigest()
        old = state["steps"].get(name, {})
        if old and old.get("command_sha256") != digest:
            raise ValueError("step command changed; use a new step name")
        if old.get("status") == "done":
            return {"status": "already_done", "step": name, "exit_code": 0}
        if old and not retry:
            return {"status": "inspection_required", "step": name, "exit_code": 3}
        step = {"command_sha256": digest, "status": "running", "started_at": _now(),
                "attempt": old.get("attempt", 0) + 1}
        state["steps"][name] = step
        _save(path, state)
        try:
            result = subprocess.run(command, check=False)
        except OSError as exc:
            step.update(status="failed", error_type=type(exc).__name__, exit_code=127, finished_at=_now())
            _save(path, state)
            return {"status": "failed", "step": name, "exit_code": 127}
        step.update(status="done" if result.returncode == 0 else "failed",
                    exit_code=result.returncode, finished_at=_now())
        _save(path, state)
        return {"status": step["status"], "step": name, "exit_code": result.returncode}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--step", required=True)
    parser.add_argument("--retry-uncertain", action="store_true",
                        help="only after inspecting the previous attempt's actual effects")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    try:
        report = run_step(args.state, args.target_sha, args.step, command, retry=args.retry_uncertain)
    except (ValueError, OSError) as exc:
        report = {"status": "blocked", "error_type": type(exc).__name__, "exit_code": 2}
    print(json.dumps(report))
    return report["exit_code"] if report["exit_code"] >= 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
