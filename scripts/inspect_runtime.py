#!/usr/bin/env python3
"""Read-only runtime inspection: no memory probes, restart, migration or repair.

Run in the service repository under its owner. Credentials are read at runtime;
only status, counts, timestamps and Git metadata are returned. Save the JSON on
the monitoring host and pass --previous on the next run to detect manual and
watchdog restarts as well as systemd automatic restarts.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import urllib.error
import urllib.request


def _run(repo: Path, command: list[str]) -> str:
    result = subprocess.run(command, cwd=repo, capture_output=True, text=True, timeout=40)
    if result.returncode:
        raise RuntimeError("command_failed")
    return result.stdout.strip()


def _token(path: Path) -> str:
    token = os.environ.get("AIDUMEM_API_TOKEN", "")
    if token:
        return token
    candidates = [path]
    configured = os.environ.get("AIDUMEM_ENV_FILE", "").strip()
    if configured:
        candidates.append(Path(configured).expanduser())
    candidates.append(Path("~/.aidumem/.env").expanduser())
    for candidate in candidates:
        if not candidate.is_file():
            continue
        for line in candidate.read_text().splitlines():
            key, sep, value = line.strip().removeprefix("export ").partition("=")
            if sep and key.strip() == "AIDUMEM_API_TOKEN":
                token = value.strip().strip("\"'")
                if token:
                    return token
    return ""


def inspect(repo: Path, url: str, *, env_file: Path | None = None,
            data_dir: Path | None = None, service="aiduMEI.service") -> dict:
    repo = repo.resolve()
    out = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    def collect(name, fn):
        try:
            out[name] = fn()
        except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
            out[name] = {"error_type": type(exc).__name__}

    def health():
        req = urllib.request.Request(url.rstrip("/") + "/health")
        token = _token(env_file or repo / ".env")
        if token:
            req.add_header("Authorization", "Bearer " + token)
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.load(response)
        return {"status": result.get("health_status", result.get("status")),
                "version": result.get("version"), "git_sha": result.get("git_sha"),
                "degraded": bool(result.get("degraded")),
                "details_available": bool(token) and not bool((result.get("probes") or {}).get("_redacted"))}

    def code():
        return {"head": _run(repo, ["git", "rev-parse", "HEAD"]),
                "dirty": bool(_run(repo, ["git", "status", "--porcelain"]))}

    def unit():
        text = _run(repo, ["systemctl", "--user", "show", service, "-p", "ActiveState",
                           "-p", "NRestarts", "-p", "MainPID", "-p", "ActiveEnterTimestamp",
                           "-p", "ExecMainStartTimestamp"])
        return dict(line.split("=", 1) for line in text.splitlines() if "=" in line)

    def errors():
        text = _run(repo, ["journalctl", "--user", "-u", service, "--since", "-24h", "--no-pager", "-o", "json"])
        messages = [json.loads(line).get("MESSAGE", "") for line in text.splitlines() if line.strip()]
        return {"traceback": sum("Traceback" in line for line in messages),
                "error": sum(bool(re.search(r"\bERROR\b", line)) for line in messages),
                "http_5xx": sum(bool(re.search(r'" 5\d\d\b|status[=: ]5\d\d', line)) for line in messages)}

    def memory():
        path = (data_dir or repo / "data") / "memory.db"
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            return {"count": conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0],
                    "integrity": conn.execute("PRAGMA quick_check").fetchone()[0],
                    "bytes": path.stat().st_size}
        finally:
            conn.close()

    collect("health", health)
    collect("code", code)
    collect("service", unit)
    collect("errors_24h", errors)
    collect("memory", memory)
    return out


def evaluate(current: dict, previous: dict | None = None, expected_sha="") -> dict:
    previous = previous or {}
    red, yellow = [], []
    h, c, s, errors, memory = (current.get(k, {}) for k in ("health", "code", "service", "errors_24h", "memory"))
    if h.get("status") != "ok":
        red.append("health_failed")
    if s.get("ActiveState") not in (None, "active"):
        red.append("service_inactive")
    if memory.get("integrity") not in (None, "ok"):
        red.append("database_integrity")
    if any(errors.get(k, 0) for k in ("traceback", "error", "http_5xx")):
        red.append("runtime_errors")
    for name, value in current.items():
        if isinstance(value, dict) and value.get("error_type"):
            (red if name in ("health", "memory") else yellow).append(name + "_unavailable")
    if not h.get("details_available"):
        yellow.append("full_health_unavailable")
    if h.get("degraded"):
        yellow.append("degraded")
    if c.get("dirty"):
        yellow.append("uncommitted_production_changes")
    if expected_sha and c.get("head") != expected_sha:
        yellow.append("production_head_drift")
    if h.get("git_sha") and c.get("head") and not c["head"].startswith(h["git_sha"]):
        yellow.append("health_head_mismatch")
    old = previous.get("service", {})
    for key in ("NRestarts", "MainPID", "ExecMainStartTimestamp", "ActiveEnterTimestamp"):
        if old.get(key) and old[key] != s.get(key):
            yellow.append("service_restarted")
            break
    return {"verdict": "red" if red else "yellow" if yellow else "green", "red": red, "yellow": yellow}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--url", default="http://127.0.0.1:8767")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--service", default="aiduMEI.service")
    parser.add_argument("--expected-sha", default="")
    parser.add_argument("--previous", type=Path)
    args = parser.parse_args(argv)
    previous = json.loads(args.previous.read_text()) if args.previous else {}
    report = inspect(args.repo, args.url, env_file=args.env_file, data_dir=args.data_dir, service=args.service)
    report.update(evaluate(report, previous, args.expected_sha))
    print(json.dumps(report, ensure_ascii=False))
    return {"green": 0, "yellow": 2, "red": 3}[report["verdict"]]


if __name__ == "__main__":
    raise SystemExit(main())
