#!/usr/bin/env python3
"""Generate a machine-readable operation report for aiduMEI.

The script is deliberately read-only. It queries the running API for health
and optionally extends that with local maintenance state when credentials are
available. It never prints raw credentials.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ducky.version import SERVICE_VERSION  # noqa: E402
from ducky.utils import api_auth_headers  # noqa: E402

SCHEMA_VERSION = 1


def _base_url() -> str:
    return (os.environ.get("AIDUMEM_API_BASE", "http://127.0.0.1:8767")).rstrip("/")


def _get_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def _git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[1],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
        return result.stdout.strip()
    except Exception:
        return None


def _crontab_task_count() -> int | None:
    try:
        result = subprocess.run(
            [str(Path(__file__).resolve().parent / "update_crontab.sh"), "--list"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout or "{}")
        return len(data.get("tasks", []))
    except Exception:
        return None


def _latest_backup(root: Path) -> dict[str, Any]:
    if not root.exists():
        return {"path": None, "age_hours": None, "verified": None}
    candidates = [p for p in root.iterdir() if p.is_dir()]
    if not candidates:
        return {"path": None, "age_hours": None, "verified": None}
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    verified = (latest / ".backup_verified").exists()
    age_hours = round((time.time() - latest.stat().st_mtime) / 3600, 2)
    return {"path": latest.name, "age_hours": age_hours, "verified": verified}


def _safe_next_actions(health: dict[str, Any]) -> list[str]:
    actions: list[str] = []
    if health.get("health_status") != "ok":
        actions.append("Inspect the authenticated /health response and resolve degraded components.")
    if health.get("degraded"):
        actions.append("Resolve degraded components before relying on semantic recall.")
    if health.get("warming_up"):
        actions.append("Wait for warm-up components or trigger a normal request before deep diagnosis.")
    if _crontab_task_count() is None or (_crontab_task_count() or 0) < 9:
        actions.append("Run scripts/update_crontab.sh --list and install the required maintenance jobs.")
    backup = _latest_backup(Path(os.environ.get("AIDUMEM_BACKUP_ROOT", "backups")))
    if not backup.get("verified"):
        actions.append("Create and verify a backup with scripts/backup_gate.sh.")
    if not actions:
        actions.append("System is healthy; run scripts/e2e_smoke.py after the next deployment or restore.")
    return actions


def _public_report(health: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": int(time.time()),
        "service_version": SERVICE_VERSION,
        "git_commit": _git_commit(),
        "health_status": health.get("health_status"),
        "status": health.get("status"),
        "engine_mode": health.get("engine_mode"),
        "degraded": health.get("degraded", []),
        "warming_up": health.get("warming_up", []),
        "next_actions": _safe_next_actions(health),
    }


def _full_report(health: dict[str, Any]) -> dict[str, Any]:
    probes = health.get("probes") or {}
    report = _public_report(health)
    report.update({
        "capacity": {
            "facts_active_count": probes.get("facts_active_count"),
            "facts_watermark_effective": probes.get("facts_watermark_effective"),
            "wal_total_bytes": probes.get("wal_total_bytes"),
            "wal_alert_dbs": probes.get("wal_alert_dbs"),
            "process_rss_mb": probes.get("process_rss_mb"),
            "process_max_rss_mb": probes.get("process_max_rss_mb"),
        },
        "maintenance": {
            "crontab_task_count": _crontab_task_count(),
            "latest_backup": _latest_backup(Path(os.environ.get("AIDUMEM_BACKUP_ROOT", "backups"))),
        },
        "anomalies": {
            "warnings": health.get("warnings", []),
            "feature_failures": probes.get("feature_failures"),
            "feature_failures_by_name": probes.get("feature_failures_by_name"),
        },
        "health": health,
    })
    return report


def _exit_code(report: dict[str, Any]) -> int:
    if report.get("health_status") != "ok" or report.get("degraded"):
        return 3
    if report.get("warming_up") or report.get("anomalies", {}).get("warnings"):
        return 2
    if report.get("maintenance", {}).get("crontab_task_count") in (None, 0):
        return 2
    if not report.get("maintenance", {}).get("latest_backup", {}).get("verified"):
        return 2
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit a single JSON object")
    args = parser.parse_args()
    headers = {"Accept": "application/json"}
    headers.update(api_auth_headers())
    try:
        public = _get_json(f"{_base_url()}/health", {"Accept": "application/json"})
        full = _get_json(f"{_base_url()}/health", headers)
    except urllib.error.HTTPError as exc:
        print(json.dumps({"schema_version": SCHEMA_VERSION, "status": "fail", "http_error": exc.code}))
        return 3
    except Exception as exc:
        print(json.dumps({"schema_version": SCHEMA_VERSION, "status": "fail", "error": str(exc)[:200]}))
        return 3
    report = _full_report(full) if headers.get("Authorization") else _public_report(public)
    print(json.dumps(report, ensure_ascii=False, indent=None if args.json else 2, sort_keys=True))
    return _exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
