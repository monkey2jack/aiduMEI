#!/usr/bin/env python3
"""f0.8+ ④ data cleanup.  Every sub-command is a dry run unless --apply is given.
Output is counts, ids and paths only, never memory text or secrets.

  wash       --data-dir D          redact (principle 7) reflections / memory_edits / autodream_log
                                   text in facts.db; --apply backs facts.db up first (sqlite backup
                                   API, 0600) into --backup-dir.
  reasoning  --data-dir D          list stored session summaries that are model reasoning text.
                                   --apply deletes them through the running service's /delete
                                   (tombstone, restorable for the tombstone TTL): needs --api URL
                                   and the key in env AIDUMEM_API_TOKEN.
  repack     --root R              redact old backups in place and re-pack them (never delete a
                                   backup): every TEXT cell of every SQLite file (FTS indexes rebuilt,
                                   then VACUUM INTO so no plaintext stays in free pages), every
                                   .json/.jsonl/.md/.txt file, and *.tar.gz / *.tgz archives (re-packed
                                   with the same members).  Skips backups newer than --keep-days and
                                   names matching --keep; never touches mem0_config_local.json / .env
                                   (restore needs them; reported).  Rewrites SHA256SUMS / the bundle
                                   manifest digests when present.
  transcripts --root R             Claude Code session transcripts (*.jsonl, including the
                                   2.1.322 "api-request" records): files older than --retain-days
                                   are deleted, the rest are redacted line by line; api-request
                                   bodies older than --api-request-days are reduced to a stub.
                                   Files touched in the last 10 minutes (live sessions) are skipped.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SECRET_FILES = {"mem0_config_local.json", ".env"}
TEXT_SUFFIXES = {".json", ".jsonl", ".md", ".txt", ".log"}
WASH_TABLES = {          # table -> text columns (facts.db), plan ④
    "reflections": ("content", "evidence"),
    "memory_edits": ("old_content", "new_content", "reason"),
    "autodream_log": ("new_content", "reason"),
}


def _redact(text: str) -> tuple[str, list]:
    from ducky.write_policy import redact_forbidden
    return redact_forbidden(text)


def _redact_value(value):
    """Strings anywhere in a JSON value; returns (new, changed)."""
    if isinstance(value, str):
        new, kinds = _redact(value)
        return new, bool(kinds) and new != value
    if isinstance(value, list):
        out, ch = [], False
        for v in value:
            nv, c = _redact_value(v)
            out.append(nv)
            ch |= c
        return out, ch
    if isinstance(value, dict):
        out, ch = {}, False
        for k, v in value.items():
            nv, c = _redact_value(v)
            out[k] = nv
            ch |= c
        return out, ch
    return value, False


def _redact_cell(value):
    """A TEXT cell: JSON documents are redacted value-wise (keys kept), else as text."""
    if not isinstance(value, str) or not value:
        return value, False
    s = value.lstrip()
    if s[:1] in "{[":
        try:
            doc = json.loads(value)
        except ValueError:
            doc = None
        if doc is not None:
            new, ch = _redact_value(doc)
            return (json.dumps(new, ensure_ascii=False), True) if ch else (value, False)
    new, kinds = _redact(value)
    return (new, True) if kinds and new != value else (value, False)


def _sqlite_backup(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    s = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    d = sqlite3.connect(str(dest))
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()
    os.chmod(dest, 0o600)


# ── wash ───────────────────────────────────────────────────────────────────
def wash(data_dir: Path, *, apply: bool, backup_dir: Path | None) -> dict:
    db = data_dir / "facts.db"
    rep = {"db": str(db), "tables": {}, "applied": False}
    if not db.exists():
        rep["error"] = "facts.db not found"
        return rep
    if apply:
        bdir = backup_dir or data_dir.parent / "cleanup-backups"
        dest = bdir / f"facts-before-wash-{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        _sqlite_backup(db, dest)
        rep["backup"] = str(dest)
    conn = sqlite3.connect(str(db) if apply else f"file:{db}?mode=ro", uri=not apply, timeout=30)
    try:
        for table, cols in WASH_TABLES.items():
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            cols = [c for c in cols if c in have]
            if not cols:
                continue
            rows = conn.execute(f"SELECT rowid, {', '.join(cols)} FROM {table}").fetchall()
            changed = 0
            for row in rows:
                upd = {}
                for c, v in zip(cols, row[1:]):
                    nv, ch = _redact_cell(v)
                    if ch:
                        upd[c] = nv
                if upd:
                    changed += 1
                    if apply:
                        conn.execute(f"UPDATE {table} SET {', '.join(c + '=?' for c in upd)} WHERE rowid=?",
                                     (*upd.values(), row[0]))
            rep["tables"][table] = {"rows": len(rows), "would_change" if not apply else "changed": changed}
        if apply:
            conn.commit()
            rep["applied"] = True
    finally:
        conn.close()
    return rep


# ── reasoning rows ────────────────────────────────────────────────────────
def reasoning_rows(data_dir: Path) -> list[dict]:
    from ducky.write_policy import looks_like_reasoning
    db = data_dir / "memory.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out = []
    try:
        for mid, uid, bank, content, md in conn.execute(
                "SELECT id, user_id, bank_id, content, metadata FROM memories"):
            try:
                meta = json.loads(md or "{}")
            except ValueError:
                meta = {}
            derived = meta.get("kind") == "session_distill" or meta.get("lane") == "distill"
            why = looks_like_reasoning(content or "") if derived else None
            if why:
                out.append({"id": mid, "user_id": uid, "bank_id": bank, "why": why})
    finally:
        conn.close()
    return out


def delete_via_api(rows: list[dict], api: str) -> dict:
    import requests
    from ducky.utils import api_auth_headers
    request_auth_headers = api_auth_headers()
    if not request_auth_headers:
        return {"error": "AIDUMEM_API_TOKEN not set"}
    ok = failed = 0
    for r in rows:
        resp = requests.post(api.rstrip("/") + "/delete", timeout=30,
                             headers=request_auth_headers,
                             json={"memory_id": r["id"], "user_id": r["user_id"], "bank_id": r["bank_id"]})
        if resp.status_code == 200:
            ok += 1
        else:
            failed += 1
    return {"deleted": ok, "failed": failed}


# ── repack old backups ────────────────────────────────────────────────────
def _is_sqlite(p: Path) -> bool:
    try:
        with p.open("rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


_FTS_SHADOW = re.compile(r"_(data|idx|content|docsize|config)$")


def redact_sqlite_file(path: Path, *, apply: bool) -> dict:
    """Redact every TEXT cell.  apply: write a redacted VACUUM INTO copy, check it, swap it in."""
    work = path
    tmpdir = None
    if apply:
        tmpdir = Path(tempfile.mkdtemp(prefix="aidumei-repack-", dir=str(path.parent)))
        work = tmpdir / "work.db"
        _sqlite_backup(path, work)
    # dry run: immutable=1 so even opening a WAL-mode file leaves no -wal/-shm beside the backup
    conn = sqlite3.connect(str(work)) if apply else sqlite3.connect(f"file:{work}?mode=ro&immutable=1", uri=True)
    cells = 0
    try:
        tables = conn.execute("SELECT name, sql FROM sqlite_master WHERE type='table'").fetchall()
        fts = {n: (s or "") for n, s in tables if "VIRTUAL TABLE" in (s or "").upper() and "FTS" in (s or "").upper()}
        shadow = {n for n, _ in tables if any(n.startswith(v + "_") and _FTS_SHADOW.search(n) for v in fts)}
        for name, _sql in tables:
            if name in shadow or name.startswith("sqlite_"):
                continue
            ext = name in fts and "content=" in fts[name].replace(" ", "").lower()
            if ext:
                continue          # external-content FTS: rebuilt from its base table below
            cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')]
            try:
                rows = conn.execute(f'SELECT rowid, * FROM "{name}"').fetchall()
            except sqlite3.OperationalError:
                continue          # WITHOUT ROWID or unreadable virtual table
            for row in rows:
                upd = {}
                for c, v in zip(cols, row[1:]):
                    nv, ch = _redact_cell(v)
                    if ch:
                        upd[c] = nv
                if upd:
                    cells += len(upd)
                    if apply:
                        conn.execute(f'UPDATE "{name}" SET {", ".join(chr(34) + c + chr(34) + "=?" for c in upd)} '
                                     "WHERE rowid=?", (*upd.values(), row[0]))
        if apply:
            for name, sql in fts.items():
                if "content=" in sql.replace(" ", "").lower():
                    try:
                        conn.execute(f'INSERT INTO "{name}"("{name}") VALUES(\'rebuild\')')
                    except sqlite3.OperationalError:
                        pass
            conn.commit()
    finally:
        conn.close()
    rep = {"path": str(path), "cells": cells}
    if apply:
        try:
            if cells:
                packed = tmpdir / "packed.db"
                c2 = sqlite3.connect(str(work))
                c2.execute(f"VACUUM INTO '{packed}'")
                c2.close()
                chk = sqlite3.connect(str(packed))
                ok = chk.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                chk.close()
                if not ok:
                    raise RuntimeError("integrity_check failed on redacted copy")
                os.chmod(packed, path.stat().st_mode & 0o777)
                os.replace(packed, path)
                rep["replaced"] = True
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    return rep


def redact_text_file(path: Path, *, apply: bool) -> int:
    try:
        raw = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return 0
    if path.suffix == ".jsonl":
        out, n = [], 0
        for line in raw.splitlines(keepends=True):
            body = line.rstrip("\n")
            nv, ch = _redact_cell(body)
            n += ch
            out.append(nv + ("\n" if line.endswith("\n") else ""))
        new = "".join(out)
    else:
        new, ch = _redact_cell(raw)
        n = int(ch)
    if n and apply:
        st = path.stat()
        tmp = path.with_name(path.name + ".redact-tmp")
        tmp.write_text(new, encoding="utf-8")
        os.chmod(tmp, st.st_mode & 0o777)
        os.replace(tmp, path)
        os.utime(path, (st.st_atime, st.st_mtime))
    return n


_SIDECAR = re.compile(r"-(wal|shm|journal)$")


def _walk_tree(root: Path, *, apply: bool, rep: dict) -> None:
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.is_symlink() or _SIDECAR.search(p.name):
            continue
        if p.name in SECRET_FILES:
            rep["secret_files_kept"].append(str(p))
            continue
        if _is_sqlite(p):
            r = redact_sqlite_file(p, apply=apply)
            rep["sqlite_cells"] += r["cells"]
            rep["sqlite_files_hit"] += bool(r["cells"])
        elif p.name.endswith((".tar.gz", ".tgz")):
            rep["archives_hit"] += _repack_archive(p, apply=apply, rep=rep)
        elif p.suffix in TEXT_SUFFIXES and p.name not in ("SHA256SUMS",):
            n = redact_text_file(p, apply=apply)
            rep["text_files_hit"] += bool(n)


def _repack_archive(path: Path, *, apply: bool, rep: dict) -> int:
    # dry run: extract to the system temp dir, never next to the backup
    tmp = Path(tempfile.mkdtemp(prefix="aidumei-arch-", dir=str(path.parent) if apply else None))
    try:
        with tarfile.open(path, "r:gz") as tf:
            members = [m for m in tf.getmembers() if m.isfile() or m.isdir()]
            tf.extractall(tmp, members=members, filter="data")
        sub = {"sqlite_cells": 0, "sqlite_files_hit": 0, "text_files_hit": 0, "archives_hit": 0,
               "secret_files_kept": []}
        _walk_tree(tmp, apply=True, rep=sub)     # always on the extracted copy
        rep["secret_files_kept"] += [f"{path}!{Path(s).relative_to(tmp)}" for s in sub["secret_files_kept"]]
        hit = sub["sqlite_cells"] + sub["text_files_hit"] + sub["archives_hit"]
        rep["sqlite_cells"] += sub["sqlite_cells"]
        if hit and apply:
            out = path.with_name(path.name + ".repack-tmp")
            with tarfile.open(out, "w:gz") as tf:
                for m in members:
                    tf.add(tmp / m.name, arcname=m.name, recursive=False)
            os.chmod(out, path.stat().st_mode & 0o777)
            os.replace(out, path)
        return int(bool(hit))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _reseal(bdir: Path) -> None:
    sums = bdir / "SHA256SUMS"
    manifest = next(iter(sorted(bdir.glob("*manifest*.json"))), None)
    digests = {}
    for p in sorted(bdir.rglob("*")):
        if p.is_file() and p.name != "SHA256SUMS" and p != manifest:
            digests[p.relative_to(bdir).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    if manifest is not None:
        try:
            doc = json.loads(manifest.read_text(encoding="utf-8"))
            for row in doc.get("files", []):
                path = bdir / row.get("path", "")
                if path.is_file():
                    row["sha256"] = digests.get(row["path"], row.get("sha256"))
                    row["size"] = path.stat().st_size
            doc["redacted_at"] = datetime.now(timezone.utc).isoformat()
            manifest.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
            digests[manifest.relative_to(bdir).as_posix()] = hashlib.sha256(manifest.read_bytes()).hexdigest()
        except ValueError:
            pass
    if sums.exists():
        sums.write_text("".join(f"{d}  {n}\n" for n, d in sorted(digests.items())), encoding="utf-8")


def repack(root: Path, *, apply: bool, keep_days: float, keep: list[str]) -> dict:
    now = time.time()
    out = {"root": str(root), "backups": [], "skipped_recent": [], "skipped_keep": []}
    if not root.is_dir():
        out["error"] = "root not found"
        return out
    for entry in sorted(root.iterdir()):
        if entry.is_symlink() or _SIDECAR.search(entry.name):
            continue
        if any(re.search(k, entry.name) for k in keep):
            out["skipped_keep"].append(entry.name)
            continue
        if now - entry.stat().st_mtime < keep_days * 86400:
            out["skipped_recent"].append(entry.name)
            continue
        rep = {"name": entry.name, "sqlite_cells": 0, "sqlite_files_hit": 0, "text_files_hit": 0,
               "archives_hit": 0, "secret_files_kept": []}
        if entry.is_dir():
            _walk_tree(entry, apply=apply, rep=rep)
            if apply and (rep["sqlite_files_hit"] or rep["text_files_hit"] or rep["archives_hit"]):
                _reseal(entry)
        elif entry.name.endswith((".tar.gz", ".tgz")):
            rep["archives_hit"] = _repack_archive(entry, apply=apply, rep=rep)
        elif _is_sqlite(entry):          # e.g. a daily folder of memory-YYYYMMDD.db files
            r = redact_sqlite_file(entry, apply=apply)
            rep["sqlite_cells"], rep["sqlite_files_hit"] = r["cells"], int(bool(r["cells"]))
        else:
            continue
        out["backups"].append(rep)
    return out


# ── Claude Code transcripts ───────────────────────────────────────────────
def _record_ts(obj) -> float:
    ts = obj.get("timestamp") if isinstance(obj, dict) else None
    if not isinstance(ts, str):
        return 0.0
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def transcripts(root: Path, *, apply: bool, retain_days: float, api_request_days: float) -> dict:
    now = time.time()
    rep = {"root": str(root), "files": 0, "bytes": 0, "expired_files": 0, "expired_bytes": 0,
           "redacted_files": 0, "redacted_lines": 0, "api_request_records": 0, "api_request_stubbed": 0,
           "api_request_bytes_freed": 0, "skipped_live": 0, "applied": apply}
    if not root.is_dir():
        rep["error"] = "root not found"
        return rep
    for p in sorted(root.rglob("*.jsonl")):
        if not p.is_file() or p.is_symlink():
            continue
        st = p.stat()
        rep["files"] += 1
        rep["bytes"] += st.st_size
        if now - st.st_mtime < 600:
            rep["skipped_live"] += 1
            continue
        if now - st.st_mtime > retain_days * 86400:
            rep["expired_files"] += 1
            rep["expired_bytes"] += st.st_size
            if apply:
                p.unlink()
            continue
        out, changed, red = [], False, 0
        with p.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                body = line.rstrip("\n")
                try:
                    obj = json.loads(body)
                except ValueError:
                    obj = None
                if isinstance(obj, dict) and obj.get("type") == "api-request":
                    rep["api_request_records"] += 1
                    if now - (_record_ts(obj) or st.st_mtime) > api_request_days * 86400:
                        stub = {k: obj[k] for k in ("type", "timestamp", "uuid", "sessionId") if k in obj}
                        stub["_pruned"] = "aidumei-f08p"
                        new = json.dumps(stub, ensure_ascii=False)
                        rep["api_request_stubbed"] += 1
                        rep["api_request_bytes_freed"] += len(body.encode()) - len(new.encode())
                        out.append(new + "\n")
                        changed = True
                        continue
                nv, ch = _redact_cell(body) if obj is not None else (_redact(body)[0], False)
                if ch:
                    red += 1
                    changed = True
                out.append(nv + ("\n" if line.endswith("\n") else ""))
        if red:
            rep["redacted_files"] += 1
            rep["redacted_lines"] += red
        if changed and apply:
            tmp = p.with_name(p.name + ".sweep-tmp")
            tmp.write_text("".join(out), encoding="utf-8")
            os.chmod(tmp, st.st_mode & 0o777)
            os.replace(tmp, p)
            os.utime(p, (st.st_atime, st.st_mtime))      # keep age for the retention clock
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["wash", "reasoning", "repack", "transcripts"])
    ap.add_argument("--data-dir", default="")
    ap.add_argument("--backup-dir", default="")
    ap.add_argument("--root", default="")
    ap.add_argument("--api", default="")
    ap.add_argument("--keep-days", type=float, default=7)
    ap.add_argument("--keep", action="append", default=[], help="regex of backup names to leave alone")
    ap.add_argument("--retain-days", type=float, default=30)
    ap.add_argument("--api-request-days", type=float, default=7)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd in ("wash", "reasoning"):
        if not a.data_dir:
            ap.error("--data-dir required")
        data = Path(a.data_dir).resolve()
        if a.cmd == "wash":
            res = wash(data, apply=a.apply, backup_dir=Path(a.backup_dir) if a.backup_dir else None)
        else:
            rows = reasoning_rows(data)
            res = {"count": len(rows), "by_reason": {}, "ids": [r["id"] for r in rows]}
            for r in rows:
                res["by_reason"][r["why"]] = res["by_reason"].get(r["why"], 0) + 1
            if a.apply:
                if not a.api:
                    ap.error("--apply needs --api (deletes go through the service's tombstone chain)")
                res["delete"] = delete_via_api(rows, a.api)
    else:
        if not a.root:
            ap.error("--root required")
        root = Path(os.path.expanduser(a.root)).resolve()
        if a.cmd == "repack":
            res = repack(root, apply=a.apply, keep_days=a.keep_days, keep=a.keep)
        else:
            res = transcripts(root, apply=a.apply, retain_days=a.retain_days, api_request_days=a.api_request_days)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
