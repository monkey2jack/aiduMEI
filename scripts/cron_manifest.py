#!/usr/bin/env python3
"""Verify or replace owned cron entries against the generated task manifest.

The caller supplies canonical two-line entries from update_crontab.sh. Standard
input is the current crontab; verification emits JSON, merge emits a crontab.

f0.3 (O-3): an entry is owned by one checkout. Its identity is the task name
plus the repository root in its ``cd "<root>"`` part, so an install from a
second checkout (a sandbox next to production) leaves the other root's entries
untouched and reports them instead of silently taking them over. Verification
also reports job lines outside the managed block that run this root's managed
scripts (a legacy unmanaged consolidator line, say) as drift.
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
_USER_RE = r"[a-z_][a-z0-9_-]{0,31}"
_SHELLS = frozenset({"bash", "sh", "dash", "zsh"})
_OPERATORS = frozenset({"&&", "||", ";", "|", "&", "(", ")", ";;", "|&"})


def _command_of(line: str) -> str | None:
    """The command of a crontab job line (after its schedule), else None."""
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    if stripped.startswith("@"):
        parts = stripped.split(None, 1)
        return parts[1] if len(parts) == 2 else None
    parts = stripped.split(None, 5)
    return parts[5] if len(parts) == 6 else None   # env lines have no schedule


def _parse_managed(cron: str) -> dict | None:
    """Split a generated job: ``<sched> cd <root> && [runuser -u U --] <runner> <target> ...``."""
    head, sep, run = cron.partition(" && ")
    cd = _command_of(head) if sep else None
    if cd is None:
        return None
    try:
        cd_args = shlex.split(cd)
        args = shlex.split(run.split(" >> ", 1)[0])
    except ValueError:
        return None
    if len(cd_args) != 2 or cd_args[0] != "cd":
        return None
    run_as, wrapper = "", ""
    if (len(args) >= 4 and Path(args[0]).name == "runuser" and args[1] == "-u"
            and args[3] == "--" and re.fullmatch(_USER_RE, args[2])):
        run_as, wrapper, args = args[2], args[0], args[4:]
    if len(args) < 2:
        return None
    return {"root": cd_args[1], "run_as": run_as, "wrapper": wrapper,
            "runner": args[0], "target": args[1]}


def _run_as_of(cron: str) -> str:
    entry = _parse_managed(cron)
    return entry["run_as"] if entry else ""


_RUN_AS_PREFIX = re.compile(
    r"^(?P<pre>.*? && )(?:\"[^\"]*runuser\"|\S*runuser) -u (?P<user>" + _USER_RE + r") -- "
    r"(?P<rest>.*)$")


def _matches(installed: str, expected: str) -> tuple[bool, str]:
    """Exact match, or -- when the manifest itself asks for no demotion -- the
    same command demoted with ``runuser -u <user> --`` (reported as run_as).

    A verifier that was not told a run-as user (report.py's hourly call, say)
    accepts a deliberate demotion; one that was told enforces it exactly.
    """
    if installed == expected:
        return True, _run_as_of(expected)
    if _run_as_of(expected):
        return False, ""
    m = _RUN_AS_PREFIX.match(installed)
    if m and m.group("pre") + m.group("rest") == expected:
        return True, m.group("user")
    return False, ""


def _same_root(a: str, b: str) -> bool:
    if os.path.normpath(a) == os.path.normpath(b):
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


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


def _foreign_entry(cron: str, repo_dir: str) -> bool:
    """A recognizable aiduMEI task command (python/bash + scripts/<file>) of another root.

    Only a recognizable command counts: a header above an unrelated job stays
    ambiguous whatever directory that job changes into.
    """
    entry = _parse_managed(cron)
    return (entry is not None and not _same_root(entry["root"], repo_dir)
            and _task_target(cron) is not None)


def _observed(lines: list[str], known: set[str], repo_dir: str):
    """Managed entries of *this* root, unknown ones, other roots', consumed lines."""
    found: dict[str, list[str]] = {}
    unexpected: list[str] = []
    foreign: list[dict] = []
    consumed: set[int] = set()
    for index, line in enumerate(lines):
        if not line.startswith(HEADER):
            continue
        name = _name(line, known)
        cron = lines[index + 1] if index + 1 < len(lines) else ""
        consumed.update((index, index + 1))
        if _foreign_entry(cron, repo_dir):
            entry = _parse_managed(cron)
            foreign.append({"line": index + 1, "task": name, "root": entry["root"],
                            "root_present": os.path.isdir(entry["root"])})
            continue
        if name in known:
            found.setdefault(name, []).append(cron)
        else:
            unexpected.append(name)
    return found, unexpected, foreign, consumed


def _target_ready(cron: str, repo_dir: str) -> bool:
    """A matching cron line is usable only if its interpreter and target exist."""
    entry = _parse_managed(cron)
    if entry is None:
        return False
    target = _task_target(cron, expected_runner=entry["runner"])
    if target is None:
        return False
    for executable in filter(None, (entry["wrapper"], entry["runner"])):
        resolved = executable if os.path.isabs(executable) else shutil.which(executable)
        if not resolved or not os.access(resolved, os.X_OK):
            return False
    path = Path(repo_dir) / target
    return path.is_file() and os.access(path, os.R_OK)


def _task_target(cron: str, *, expected_runner: str = "") -> str | None:
    """Extract the owned script argument from a generated cron command."""
    entry = _parse_managed(cron)
    if entry is None:
        return None
    runner = Path(entry["runner"]).name
    if not (runner.startswith("python") or runner == "bash"
            or entry["runner"] == expected_runner):
        return None
    if not re.fullmatch(r"scripts/[A-Za-z0-9_-]+\.(?:py|sh)", entry["target"]):
        return None
    return entry["target"]


# -- job lines outside the managed block ------------------------------------

def _simple_commands(command: str) -> list[list[str]]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        tokens = list(lexer)
    except ValueError:
        return []
    commands, current, skip = [], [], False
    for token in tokens:
        if skip:
            skip = False
            continue
        if token in _OPERATORS:
            if current:
                commands.append(current)
            current = []
        elif token[:1] in ("<", ">") or token in ("&>", "&>>"):
            skip = True                                 # drop the redirect target
            if current and current[-1].isdigit():
                current.pop()                           # and the fd of 2>&1
        else:
            current.append(token)
    if current:
        commands.append(current)
    return commands


def _resolve(token: str, cwd: str | None) -> str | None:
    token = os.path.expanduser(token)
    if os.path.isabs(token):
        return os.path.normpath(token)
    return os.path.normpath(os.path.join(cwd, token)) if cwd else None


def _invocations(command: str, depth: int = 0) -> list[tuple[str, list[str]]]:
    """(script path, its arguments) for each program a crontab command runs.

    Understands ``cd DIR &&``, leading VAR=value, a ``runuser -u U --`` /
    ``sudo -u U`` / ``su ... -c '...'`` wrapper, an interpreter (python*,
    bash, sh) followed by its script, ``sh -c '...'`` (one level down) and a
    script executed directly. A path merely passed as an argument (``echo
    /x/scripts/report.py``) is not an invocation.
    """
    found: list[tuple[str, list[str]]] = []
    cwd: str | None = None
    for args in _simple_commands(command):
        while args and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", args[0]):
            args = args[1:]
        if not args:
            continue
        if args[0] == "cd":
            cwd = _resolve(args[1], cwd) if len(args) > 1 else None
            continue
        head = Path(args[0]).name
        if head in {"runuser", "sudo"} and "--" in args:
            args = args[args.index("--") + 1:]
        elif head == "sudo" and len(args) >= 4 and args[1] == "-u":
            args = args[3:]
        if not args:
            continue
        head = Path(args[0]).name
        if head in {"su", *(_SHELLS)} and "-c" in args[1:] and depth < 2:
            nested = args.index("-c", 1) + 1
            if nested < len(args):
                found.extend(_invocations(args[nested], depth + 1))
            continue
        for position, token in enumerate(args):
            name = Path(token).name
            if name.startswith("python") or name in _SHELLS:
                rest = list(args[position + 1:])
                while rest and rest[0].startswith("-"):
                    rest = rest[1:]
                if rest:
                    script = _resolve(rest[0], cwd)
                    if script:
                        found.append((script, rest[1:]))
                break
        else:
            script = _resolve(args[0], cwd)
            if script:
                found.append((script, args[1:]))
    return found


def _unmanaged(lines: list[str], consumed: set[int], repo_dir: str,
               expected: dict[str, str]) -> list[dict]:
    """Job lines outside the managed block that run one of this root's tasks."""
    by_script: dict[str, list[tuple[str, list[str]]]] = {}
    for name, cron in expected.items():
        entry = _parse_managed(cron)
        if entry is None:
            continue
        try:
            task_args = shlex.split(cron.split(" && ", 1)[1].split(" >> ", 1)[0])
        except ValueError:
            task_args = []
        extra = task_args[task_args.index(entry["target"]) + 1:] if entry["target"] in task_args else []
        by_script.setdefault(entry["target"], []).append((name, extra))
    scripts_dir = os.path.join(repo_dir, "scripts")
    found: list[dict] = []
    for index, line in enumerate(lines):
        command = None if index in consumed else _command_of(line)
        if command is None:
            continue
        for path, args in _invocations(command):
            if not _same_root(os.path.dirname(path), scripts_dir):
                continue
            script = "scripts/" + os.path.basename(path)
            candidates = by_script.get(script, [])
            tasks = [name for name, extra in candidates if extra and args[:1] == extra[:1]]
            tasks = tasks or [name for name, _extra in candidates]
            if tasks:
                found.append({"line": index + 1, "script": script, "tasks": tasks})
    return found


def verify(entries: list[str], current: str, repo_dir: str) -> dict:
    expected = _expected(entries)
    lines = current.splitlines()
    found, unexpected, foreign, consumed = _observed(lines, set(expected), repo_dir)
    unmanaged = _unmanaged(lines, consumed, repo_dir, expected)
    tasks = {}
    for name, command in expected.items():
        seen = found.get(name, [])
        target_ok = _target_ready(command, repo_dir)
        extra_lines = [u["line"] for u in unmanaged if name in u["tasks"]]
        matched, run_as = _matches(seen[0], command) if len(seen) == 1 else (False, "")
        if not seen:
            status = "missing"
        elif len(seen) != 1:
            status = "duplicate"
        elif not matched:
            status = "drift"
        elif not target_ok:
            status = "target_missing"
        elif extra_lines:
            status = "drift"        # the task also runs from an unmanaged line
        else:
            status = "ok"
        task = {"status": status, "target_ok": target_ok}
        if run_as:
            task["run_as"] = run_as
        if extra_lines:
            task["unmanaged_lines"] = extra_lines
        tasks[name] = task
    installed = sum(task["status"] == "ok" for task in tasks.values())
    return {
        "installed": installed,
        "expected": len(expected),
        "ok": installed == len(expected) and not unexpected and not unmanaged,
        "tasks": tasks,
        "unexpected": unexpected,
        "unmanaged": unmanaged,
        "foreign": foreign,
    }


def merge(entries: list[str], current: str, repo_dir: str,
          notes: list[str] | None = None) -> str:
    expected = _expected(entries)
    notes = [] if notes is None else notes
    lines = current.splitlines()
    kept = []
    index = 0
    while index < len(lines):
        if lines[index].startswith(HEADER):
            name = _name(lines[index], set(expected))
            next_line = lines[index + 1] if index + 1 < len(lines) else ""
            entry = _parse_managed(next_line)
            if _foreign_entry(next_line, repo_dir):
                # Another checkout owns this entry (production next to a
                # sandbox, say). Its identity is name + root: never replace
                # it. verify() reports it as "foreign" after the install.
                kept.extend(lines[index:index + 2])
                index += 2
                continue
            expected_entry = _parse_managed(expected[name]) if name in expected else None
            expected_runner = expected_entry["runner"] if expected_entry else ""
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
            if entry["run_as"] and not _run_as_of(expected[name]):
                notes.append(f"{name} ran as '{entry['run_as']}'; this install runs it as "
                             "the installing user (set AIDUMEI_CRON_RUN_AS to keep it)")
            index += 2
            continue
        kept.append(lines[index])
        index += 1
    while kept and not kept[-1].strip():
        kept.pop()
    prefix = "\n".join(kept)
    body = "\n".join(entries)
    return (prefix + "\n\n" if prefix else "") + body + "\n"


def explain(report: dict) -> list[str]:
    """Human lines for what an operator has to act on (update_crontab.sh prints them)."""
    lines = []
    for item in report.get("unmanaged", []):
        lines.append(f"drift: crontab line {item['line']} runs {item['script']} outside the "
                     f"managed block (duplicates {', '.join(item['tasks'])}); remove it by hand")
    for item in report.get("foreign", []):
        state = "" if item.get("root_present") else " (that root no longer exists)"
        lines.append(f"note: {item['task']} at line {item['line']} belongs to another "
                     f"checkout: {item['root']}{state}; left untouched")
    return lines


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "explain":
        for line in explain(json.loads(sys.stdin.read() or "{}")):
            print(line, file=sys.stderr)
        return 0
    if len(sys.argv) < 4 or sys.argv[1] not in {"verify", "merge"}:
        print("usage: cron_manifest.py verify|merge REPO_DIR ENTRY... | explain", file=sys.stderr)
        return 2
    mode, repo_dir, entries = sys.argv[1], sys.argv[2], sys.argv[3:]
    current = sys.stdin.read()
    try:
        if mode == "verify":
            print(json.dumps(verify(entries, current, repo_dir), ensure_ascii=False))
        else:
            notes: list[str] = []
            merged = merge(entries, current, repo_dir, notes)
            for note in notes:
                print(f"note: {note}", file=sys.stderr)
            sys.stdout.write(merged)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
