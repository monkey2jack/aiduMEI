#!/usr/bin/env python3
"""Verify or replace owned cron entries against the generated task manifest.

The caller supplies canonical two-line entries from update_crontab.sh. Standard
input is the current crontab; verification emits JSON, merge emits a crontab.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
from pathlib import Path


HEADER = "# aiduMEI:"


def _name(header: str, known: set[str]) -> str:
    body = header[len(HEADER):]
    current = body.split("|", 1)[0].strip()
    if current in known:
        return current
    # Historical headers had a description before "| NAME|".
    for name in known:
        if re.search(r"\|\s*" + re.escape(name) + r"\|", body):
            return name
    return current


def _expected(entries: list[str]) -> dict[str, str]:
    result = {}
    for entry in entries:
        header, sep, cron = entry.partition("\n")
        if not sep or not header.startswith(HEADER) or not cron.strip():
            raise ValueError("invalid expected cron entry")
        name = header[len(HEADER):].split("|", 1)[0]
        if name in result:
            raise ValueError(f"duplicate manifest task: {name}")
        result[name] = cron
    if not result:
        raise ValueError("empty cron manifest")
    return result


def _observed(lines: list[str], known: set[str]) -> tuple[dict[str, list[str]], list[str]]:
    found: dict[str, list[str]] = {}
    unexpected: list[str] = []
    for index, line in enumerate(lines):
        if not line.startswith(HEADER):
            continue
        name = _name(line, known)
        cron = lines[index + 1] if index + 1 < len(lines) else ""
        if name in known:
            found.setdefault(name, []).append(cron)
        else:
            unexpected.append(name)
    return found, unexpected


def _target_ready(cron: str, repo_dir: str) -> bool:
    """A matching cron line is usable only if its interpreter and target exist."""
    try:
        run = cron.split(" && ", 1)[1].split(" >> ", 1)[0]
        args = shlex.split(run)
        executable = args[0]
    except (IndexError, ValueError):
        return False
    target = _task_target(cron, expected_runner=executable)
    if target is None:
        return False
    resolved = executable if os.path.isabs(executable) else shutil.which(executable)
    if not resolved or not os.access(resolved, os.X_OK):
        return False
    path = Path(repo_dir) / target
    return path.is_file() and os.access(path, os.R_OK)


def _task_target(cron: str, *, expected_runner: str = "") -> str | None:
    """Extract the owned script argument from a generated cron command."""
    try:
        run = cron.split(" && ", 1)[1].split(" >> ", 1)[0]
        args = shlex.split(run)
        if len(args) < 2:
            return None
        runner = Path(args[0]).name
        target = args[1]
        if not (runner.startswith("python") or runner == "bash"
                or args[0] == expected_runner):
            return None
        if not re.fullmatch(r"scripts/[A-Za-z0-9_-]+\.(?:py|sh)", target):
            return None
        return target
    except (IndexError, ValueError):
        return None


def verify(entries: list[str], current: str, repo_dir: str) -> dict:
    expected = _expected(entries)
    found, unexpected = _observed(current.splitlines(), set(expected))
    tasks = {}
    for name, command in expected.items():
        seen = found.get(name, [])
        target_ok = _target_ready(command, repo_dir)
        if not seen:
            status = "missing"
        elif len(seen) != 1:
            status = "duplicate"
        elif seen[0] != command:
            status = "drift"
        elif not target_ok:
            status = "target_missing"
        else:
            status = "ok"
        tasks[name] = {"status": status, "target_ok": target_ok}
    installed = sum(task["status"] == "ok" for task in tasks.values())
    return {
        "installed": installed,
        "expected": len(expected),
        "ok": installed == len(expected) and not unexpected,
        "tasks": tasks,
        "unexpected": unexpected,
    }


def merge(entries: list[str], current: str) -> str:
    expected = _expected(entries)
    lines = current.splitlines()
    kept = []
    index = 0
    while index < len(lines):
        if lines[index].startswith(HEADER):
            name = _name(lines[index], set(expected))
            next_line = lines[index + 1] if index + 1 < len(lines) else ""
            expected_runner = ""
            if name in expected:
                run = expected[name].split(" && ", 1)[1].split(" >> ", 1)[0]
                expected_runner = shlex.split(run)[0]
            # A dangling/unknown header can precede an unrelated cron job.
            # Refuse the whole install before crontab is changed unless the
            # next command targets this named aiduMEI task's actual script.
            if (name not in expected or not next_line.strip()
                    or next_line.lstrip().startswith("#")
                    or _task_target(next_line, expected_runner=expected_runner)
                    != _task_target(expected[name], expected_runner=expected_runner)):
                raise ValueError(
                    f"ambiguous aiduMEI cron entry at line {index + 1}; "
                    "refusing to remove an unverified command"
                )
            index += 2
            continue
        kept.append(lines[index])
        index += 1
    while kept and not kept[-1].strip():
        kept.pop()
    prefix = "\n".join(kept)
    body = "\n".join(entries)
    return (prefix + "\n\n" if prefix else "") + body + "\n"


def main() -> int:
    if len(sys.argv) < 4 or sys.argv[1] not in {"verify", "merge"}:
        print("usage: cron_manifest.py verify|merge REPO_DIR ENTRY...", file=sys.stderr)
        return 2
    mode, repo_dir, entries = sys.argv[1], sys.argv[2], sys.argv[3:]
    current = sys.stdin.read()
    try:
        if mode == "verify":
            print(json.dumps(verify(entries, current, repo_dir), ensure_ascii=False))
        else:
            sys.stdout.write(merge(entries, current))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
