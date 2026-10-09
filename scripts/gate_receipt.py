"""Bind a push-gate result to the tested source and retained private logs."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


REQUIRED_STEPS = {"tests": "tests.log", "static": "ruff.log", "compile": "compile.log",
                  "tree-scan": "tree-scan.log", "message-scan": "message-scan.log",
                  "metadata-scan": "metadata-scan.log", "workflow": "workflow.log"}


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot(repo: Path) -> dict:
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=repo)
    files = git("ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0")
    manifest = {}
    for raw in sorted(set(files) - {b""}):
        name = os.fsdecode(raw)
        path = repo / name
        if path.is_symlink():
            value = "symlink:" + os.readlink(path)
        else:
            value = _hash(path) if path.is_file() else "missing"
        manifest[name] = value
    return {
        "sha": git("rev-parse", "HEAD").decode().strip(),
        "tree": git("rev-parse", "HEAD^{tree}").decode().strip(),
        "status": git("status", "--porcelain", "--untracked-files=all").decode(),
        "files": manifest,
    }


def _publish(path: Path, value: dict) -> None:
    """Publish once, atomically; never replace an earlier failed receipt."""
    fd, tmp = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=True, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(tmp, path)  # Atomic publication that refuses to overwrite.
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        os.unlink(tmp)


def begin(repo: Path, evidence: Path) -> None:
    source = _snapshot(repo)
    _publish(evidence / "environment.json", {
        "python": sys.version, "executable": str(Path(sys.executable).resolve()),
        "packages": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()},
        "required_steps": REQUIRED_STEPS,
        "trust_boundary": "trusted runner; same-user mutation is not prevented",
    })
    _publish(evidence / "started.json", {
        "schema": 1, "started_at": time.time(), "source": source,
        "command": ["bash", "scripts/push_gate.sh"],
    })
    if source["status"]:
        raise RuntimeError("push gate requires a committed, clean candidate")


def record_step(evidence: Path, name: str, exit_code: int, *, skipped: str = "") -> None:
    """Record a completed command, binding its retained log at completion."""
    logfile = evidence / REQUIRED_STEPS[name]
    if skipped and (name != "static" or skipped != "ruff_unavailable"):
        raise ValueError("unsupported gate skip")
    _publish(evidence / (name + ".step.json"), {
        "name": name, "exit_code": exit_code, "skipped": skipped,
        "log": logfile.name, "log_sha256": _hash(logfile),
    })


def run_step(repo: Path, evidence: Path, name: str, command: list[str]) -> int:
    """Execute a required stage and persist its real process exit code."""
    with (evidence / REQUIRED_STEPS[name]).open("xb") as stream:
        result = subprocess.run(command, cwd=repo, stdout=stream, stderr=subprocess.STDOUT)
    record_step(evidence, name, result.returncode)
    return result.returncode


def _step_errors(evidence: Path, skipped: str) -> list[str]:
    errors = []
    actual_skips = []
    for name, logfile in REQUIRED_STEPS.items():
        try:
            step = json.loads((evidence / (name + ".step.json")).read_text())
            if (step["name"] != name or step["log"] != logfile or step["exit_code"] != 0
                    or step["log_sha256"] != _hash(evidence / logfile)):
                errors.append(name)
            if step["skipped"]:
                actual_skips.append(name)
                if name != "static" or step["skipped"] != "ruff_unavailable":
                    errors.append(name)
        except (OSError, KeyError, ValueError, TypeError):
            errors.append(name)
    if sorted(skipped.split()) != sorted(actual_skips):
        errors.append("skip_set_mismatch")
    return errors


def finish(repo: Path, evidence: Path, exit_code: int, skipped: str = "") -> int:
    started = evidence / "started.json"
    before = json.loads(started.read_text())
    after = _snapshot(repo)
    unchanged = before["source"] == after
    # Flushed command output is retained even when a gate command fails.
    logs = {p.name: {"bytes": p.stat().st_size, "sha256": _hash(p)}
            for p in sorted(evidence.iterdir())
            if p.is_file() and not p.name.startswith(".receipt-")
            and p.name != "receipt.json"}
    step_errors = _step_errors(evidence, skipped)
    result = exit_code or (0 if unchanged and not after["status"] and not step_errors else 1)
    _publish(evidence / "receipt.json", {
        "schema": 1, "finished_at": time.time(),
        "started_sha256": _hash(started), "command_exit_code": exit_code,
        "exit_code": result, "source_unchanged": unchanged,
        "source_sha": after["sha"], "source_tree": after["tree"],
        "logs": logs, "skipped": skipped.split(), "step_errors": step_errors,
        "status": "FAIL" if result else "PASS_WITH_SKIPS" if skipped else "PASS",
    })
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("begin", "finish", "step", "record"))
    parser.add_argument("repo", type=Path)
    parser.add_argument("evidence", type=Path)
    parser.add_argument("--exit-code", type=int, default=1)
    parser.add_argument("--skipped", default="")
    parser.add_argument("--name", choices=tuple(REQUIRED_STEPS))
    argv = sys.argv[1:]
    split = argv.index("--") if "--" in argv else len(argv)
    command = argv[split + 1:]
    args = parser.parse_args(argv[:split])
    if args.action in ("step", "record"):
        if not args.name:
            parser.error("--name required")
        if args.action == "record":
            record_step(args.evidence, args.name, args.exit_code, skipped=args.skipped)
            return args.exit_code
        if not command:
            parser.error("step command required")
        return run_step(args.repo, args.evidence, args.name, command)
    if args.action == "begin":
        begin(args.repo, args.evidence)
        return 0
    return finish(args.repo, args.evidence, args.exit_code, args.skipped)


if __name__ == "__main__":
    raise SystemExit(main())
