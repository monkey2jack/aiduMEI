#!/usr/bin/env python3
"""scripts/restore_tombstones.py -- batch-restore tombstoned memories (f0.3 C8).

Talks to the *running* service over HTTP (POST /tombstone/restore) and never
opens the vector store itself: the service holds the embedded Qdrant lock,
and the restore must go through the service's own embedder and stores.

Selection comes from one of two read-only sources:
  * the list API    GET /tombstones?user_id=&bank_id=   (one scope)   [default]
  * the database    --db PATH/facts.db, opened read-only (?mode=ro)   (all scopes)
then filters: --reason --actor --target-type (fnmatch patterns, comma lists),
--since/--until (ISO-8601, naive = UTC) and --utc-windows "HH:MM-HH:MM,..."
(time-of-day windows in UTC on tombstoned_at, both ends inclusive at minute
granularity; a window may wrap midnight).  Already restored tombstones are
skipped unless --include-restored.

Default is a DRY RUN: the selection is printed (id, time, scope, reason,
actor, target, 40-character preview) and nothing is changed.  --apply
restores each tombstone with its own user_id/bank_id, verifies the result
(the restore response's read-back of facts row / FTS row / vector point,
then restored_at in a fresh /tombstones listing), writes a JSON receipt and
exits 1 if anything failed.

Credentials come from the repository's single credential source,
ducky.utils.api_auth_headers(): AIDUMEM_API_TOKEN from the environment, else
the .env file named by --env-file (exported to that chain as AIDUMEM_ENV_FILE),
else AIDUMEM_ENV_FILE, else the repository root .env.  AIDUMEM_API_BASE follows
the same chain (ducky.utils.env_or_env_file) unless --base is given.  The .env
file is PARSED by ducky.utils.parse_env_file (KEY=VALUE lines), never sourced
or executed.  The token is never printed.  Importing ducky runs the package's
usual idempotent schema bootstrap on DATA_DIR, exactly like every other script.

Exit codes: 0 ok (or dry run), 1 at least one restore failed, 2 usage /
configuration / selection error.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import json
import os
import pathlib
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request

# cron / systemd / an operator's shell: the cwd is not the repository root, so
# the root must be put on sys.path explicitly before `import ducky`.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

# Single credential source (tests/test_v19_4_1_auth_gate.py): no private
# token reader and no private .env parser in this script.
from ducky.utils import api_auth_headers, env_or_env_file  # noqa: E402

DEFAULT_BASE = "http://127.0.0.1:8767"
_PREVIEW = 40
_LIST_LIMIT = 100000


class UsageError(Exception):
    """Bad arguments / configuration: exit code 2."""


# -- configuration --------------------------------------------------------

def resolve_config(args) -> tuple:
    """(base_url, auth_headers) through the shared chain in ducky.utils.

    --env-file is handed to that chain as AIDUMEM_ENV_FILE (its own knob), so
    the precedence stays the repository-wide one: environment > .env file.
    An explicit --env-file that does not exist is a usage error, not a silent
    fall-through to another file.
    """
    if args.env_file:
        if not os.path.isfile(args.env_file):
            raise UsageError(f"env file not found: {args.env_file}")
        os.environ["AIDUMEM_ENV_FILE"] = os.path.abspath(args.env_file)
    base = (args.base or env_or_env_file("AIDUMEM_API_BASE", DEFAULT_BASE)).rstrip("/")
    headers = api_auth_headers()
    if not headers:
        print("warning: no AIDUMEM_API_TOKEN in the environment or the .env chain; "
              "requests are sent without credentials", file=sys.stderr)
    return base, headers


# -- time filters ----------------------------------------------------------

def parse_iso(value: str) -> dt.datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("empty timestamp")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    stamp = dt.datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp.astimezone(dt.timezone.utc)


def parse_windows(spec: str) -> list:
    """"18:25-18:40,19:55-20:10" -> [(start_s, end_s), ...] seconds of day."""
    windows = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            start, end = part.split("-")
            sh, sm = (int(x) for x in start.strip().split(":"))
            eh, em = (int(x) for x in end.strip().split(":"))
        except ValueError as exc:
            raise UsageError(f"bad --utc-windows entry {part!r} (want HH:MM-HH:MM)") from exc
        if not (0 <= sh < 24 and 0 <= eh < 24 and 0 <= sm < 60 and 0 <= em < 60):
            raise UsageError(f"bad --utc-windows entry {part!r} (out of range)")
        windows.append((sh * 3600 + sm * 60, eh * 3600 + em * 60 + 59))
    return windows


def in_windows(stamp: dt.datetime, windows: list) -> bool:
    if not windows:
        return True
    sec = stamp.hour * 3600 + stamp.minute * 60 + stamp.second
    for start, end in windows:
        if start <= end and start <= sec <= end:
            return True
        if start > end and (sec >= start or sec <= end):   # wraps midnight
            return True
    return False


def _patterns(value: str) -> list:
    return [p.strip() for p in (value or "").split(",") if p.strip()]


def _matches(value, patterns: list) -> bool:
    return not patterns or any(fnmatch.fnmatchcase(str(value or ""), p) for p in patterns)


def select(rows: list, args) -> list:
    """Apply the filters; rows are dicts with the tombstones listing columns."""
    since = parse_iso(args.since) if args.since else None
    until = parse_iso(args.until) if args.until else None
    windows = parse_windows(args.utc_windows)
    reasons, actors, types = (_patterns(args.reason), _patterns(args.actor),
                              _patterns(args.target_type))
    chosen = []
    for row in rows:
        if row.get("restored_at") and not args.include_restored:
            continue
        if not (_matches(row.get("reason"), reasons) and _matches(row.get("actor"), actors)
                and _matches(row.get("target_type") or "memory", types)):
            continue
        try:
            stamp = parse_iso(row.get("tombstoned_at"))
        except ValueError:
            if since or until or windows:
                continue          # time-filtered run: an undated row cannot qualify
            stamp = None
        if stamp is not None and ((since and stamp < since) or (until and stamp > until)
                                  or not in_windows(stamp, windows)):
            continue
        chosen.append(row)
    chosen.sort(key=lambda r: int(r.get("tombstone_id") or 0))
    return chosen[:args.limit] if args.limit else chosen


# -- sources ---------------------------------------------------------------

_COLUMNS = ("tombstone_id", "target_id", "target_type", "user_id", "bank_id", "reason",
            "actor", "tombstoned_at", "restored_at")


def rows_from_db(path: str, user_id: str = "", bank_id: str = "") -> list:
    """Read-only query of facts.db tombstones (never writes, never creates)."""
    if not os.path.isfile(path):
        raise UsageError(f"database not found: {path}")
    uri = pathlib.Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        sql = ("SELECT " + ", ".join(_COLUMNS) + ", substr(content_snapshot, 1, ?) AS preview "
               "FROM tombstones WHERE 1=1")
        params: list = [_PREVIEW * 4]
        if user_id:
            sql += " AND user_id=?"
            params.append(user_id)
        if bank_id:
            sql += " AND bank_id=?"
            params.append(bank_id)
        return [dict(r) for r in conn.execute(sql + " ORDER BY tombstone_id", params)]
    finally:
        conn.close()


class Api:
    """Minimal JSON client for the running service (stdlib only)."""

    def __init__(self, base: str, headers: dict, timeout: float = 120.0):
        self.base = base
        self.headers = dict(headers or {})     # from api_auth_headers(); never printed
        self.timeout = timeout
        host = urllib.parse.urlparse(base).hostname or ""
        handlers = []
        if host in ("127.0.0.1", "localhost", "::1"):
            # Loopback never goes through HTTP(S)_PROXY (a SOCKS/HTTP proxy in
            # the operator's shell would otherwise swallow the request).
            handlers.append(urllib.request.ProxyHandler({}))
        self._opener = urllib.request.build_opener(*handlers)

    def _call(self, method: str, path: str, *, query=None, body=None) -> dict:
        url = self.base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        for name, value in self.headers.items():
            req.add_header(name, value)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"HTTP {exc.code} on {method} {path}: {detail}") from exc

    def list_tombstones(self, user_id: str, bank_id: str, limit: int = _LIST_LIMIT) -> list:
        out = self._call("GET", "/tombstones",
                         query={"user_id": user_id, "bank_id": bank_id, "limit": limit})
        rows = []
        for r in out.get("results") or []:
            row = {k: r.get(k) for k in _COLUMNS}
            row["preview"] = r.get("content_snapshot") or ""
            rows.append(row)
        return rows

    def restore(self, tombstone_id: int, user_id: str, bank_id: str) -> dict:
        return self._call("POST", "/tombstone/restore", body={
            "tombstone_id": int(tombstone_id), "user_id": user_id, "bank_id": bank_id})


# -- apply -----------------------------------------------------------------

def _preview(row: dict) -> str:
    return " ".join(str(row.get("preview") or "").split())[:_PREVIEW]


def restore_one(api: Api, row: dict) -> dict:
    out = {k: row.get(k) for k in ("tombstone_id", "target_id", "target_type",
                                   "user_id", "bank_id", "reason", "tombstoned_at")}
    try:
        res = api.restore(row["tombstone_id"], row.get("user_id") or "default",
                          row.get("bank_id") or "default")
    except (RuntimeError, OSError, ValueError) as exc:
        out.update(ok=False, status="error", error=str(exc)[:300])
        return out
    details = res.get("details") or {}
    verification = details.get("verification") or {}
    failed_checks = sorted(k for k, v in verification.items() if v is False)
    out.update(status=res.get("status"), detail=details.get("detail"),
               layers=details.get("layers") or {}, verification=verification)
    out["ok"] = (res.get("status") == "ok" and details.get("restored") is True
                 and not failed_checks)
    if failed_checks:
        out["error"] = "verification failed: " + ",".join(failed_checks)
    elif not out["ok"]:
        out["error"] = str(details.get("detail") or res.get("status"))[:300]
    return out


def confirm_restored(api: Api, results: list) -> None:
    """Second, independent check: restored_at is set in a fresh listing."""
    scopes = {(r.get("user_id") or "default", r.get("bank_id") or "default")
              for r in results if r.get("ok")}
    stamped: set = set()
    for user_id, bank_id in scopes:
        try:
            for row in api.list_tombstones(user_id, bank_id):
                if row.get("restored_at"):
                    stamped.add(int(row["tombstone_id"]))
        except (RuntimeError, OSError, ValueError) as exc:
            for r in results:
                if (r.get("user_id"), r.get("bank_id")) == (user_id, bank_id):
                    r["listing_error"] = str(exc)[:200]
    for r in results:
        if r.get("ok"):
            r["restored_at_confirmed"] = int(r["tombstone_id"]) in stamped
            if not r["restored_at_confirmed"]:
                r["ok"] = False
                r["error"] = "restored_at not visible in /tombstones after restore"


def write_receipt(path: str, payload: dict) -> str:
    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                      encoding="utf-8")
    return str(target)


# -- cli -------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Select tombstones (dry run by default) and restore them through "
                    "the running service.")
    ap.add_argument("--base", default="", help="service base URL (else AIDUMEM_API_BASE)")
    ap.add_argument("--env-file", default="", help=".env file to PARSE for base/token")
    ap.add_argument("--db", default="", help="read-only facts.db for selection (all scopes)")
    ap.add_argument("--user-id", default="", help="scope filter (API source: default 'default')")
    ap.add_argument("--bank-id", default="", help="scope filter (API source: default 'default')")
    ap.add_argument("--reason", default="", help="fnmatch pattern(s), comma separated")
    ap.add_argument("--actor", default="", help="fnmatch pattern(s), comma separated")
    ap.add_argument("--target-type", default="", help="fnmatch pattern(s), comma separated")
    ap.add_argument("--since", default="", help="ISO-8601 lower bound on tombstoned_at (UTC)")
    ap.add_argument("--until", default="", help="ISO-8601 upper bound on tombstoned_at (UTC)")
    ap.add_argument("--utc-windows", default="",
                    help='time-of-day windows in UTC, e.g. "18:25-18:40,19:55-20:10"')
    ap.add_argument("--include-restored", action="store_true",
                    help="also list tombstones that already have restored_at")
    ap.add_argument("--limit", type=int, default=0, help="restore at most N (0 = all)")
    ap.add_argument("--apply", action="store_true", help="really restore (default: dry run)")
    ap.add_argument("--receipt", default="", help="JSON receipt path (with --apply)")
    ap.add_argument("--timeout", type=float, default=120.0, help="per-request timeout seconds")
    ap.add_argument("--json", action="store_true", help="print the selection as JSON")
    return ap


def _print_selection(chosen: list, as_json: bool) -> None:
    if as_json:
        print(json.dumps([{**{k: r.get(k) for k in _COLUMNS}, "preview": _preview(r)}
                          for r in chosen], ensure_ascii=False, indent=1))
        return
    for r in chosen:
        print(f"#{r.get('tombstone_id')}  {r.get('tombstoned_at')}  "
              f"{r.get('user_id')}/{r.get('bank_id')}  reason={r.get('reason')}  "
              f"actor={r.get('actor')}  {r.get('target_type') or 'memory'}:{r.get('target_id')}  "
              f"| {_preview(r)}")
    print(f"selected {len(chosen)} tombstone(s)")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        base, headers = resolve_config(args)
        api = Api(base, headers, timeout=args.timeout)
        if args.db:
            rows = rows_from_db(args.db, args.user_id, args.bank_id)
        else:
            rows = api.list_tombstones(args.user_id or "default", args.bank_id or "default")
        chosen = select(rows, args)
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except (RuntimeError, OSError, ValueError, sqlite3.Error) as exc:
        print(f"error: cannot build the selection: {exc}", file=sys.stderr)
        return 2
    _print_selection(chosen, args.json)
    if not args.apply:
        print("[dry-run] nothing restored; re-run with --apply after reviewing the list.")
        return 0
    results = [restore_one(api, row) for row in chosen]
    confirm_restored(api, results)
    failed = [r for r in results if not r.get("ok")]
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    receipt = write_receipt(args.receipt or f"tombstone_restore_receipt_{stamp}.json", {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "base": base, "source": "db" if args.db else "api",
        "filters": {k: getattr(args, k) for k in ("user_id", "bank_id", "reason", "actor",
                                                   "target_type", "since", "until",
                                                   "utc_windows", "include_restored",
                                                   "limit")},
        "selected": len(chosen), "restored": len(results) - len(failed),
        "failed": len(failed), "results": results,
    })
    print(f"restored {len(results) - len(failed)}/{len(results)}; receipt: {receipt}")
    for r in failed:
        print(f"FAILED #{r.get('tombstone_id')}: {r.get('error')}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
