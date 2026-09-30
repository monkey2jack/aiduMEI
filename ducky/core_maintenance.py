"""Maintain project blocks from explicit, scoped, user-confirmed state facts.

No speculative summarization or blind timestamp refresh. Ordinary diary,
inferred facts and unconfirmed project mentions cannot overwrite core memory.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone

from ducky.bank_contract import make_scope
from ducky.utils import get_facts_conn
from ducky.scope_sql import scope_clause

logger = logging.getLogger("aiduMEI.core_maintenance")


def ensure_revision_schema(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS core_memory_revisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL, bank_id TEXT NOT NULL, block_key TEXT NOT NULL,
        old_content TEXT NOT NULL, new_content TEXT NOT NULL,
        evidence_json TEXT NOT NULL, created_at TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_core_revision_scope "
                 "ON core_memory_revisions(user_id,bank_id,block_key,id)")


def _timestamp(value, *, local_naive=False):
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        # SQLite CURRENT_TIMESTAMP is UTC; legacy core datetime.now() is local.
        if stamp.tzinfo is None and not local_naive:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _valid_fact(row: dict) -> bool:
    if row.get("archived") or row.get("superseded_by"):
        return False
    if row.get("epistemic_mode") != "user_provided" or (row.get("confidence") or 0) < 90:
        return False
    if row.get("category") != "core_memory" or row.get("fact_key") != "core_current_project":
        return False
    now = datetime.now(timezone.utc)
    for field, future in (("expires_at", False), ("valid_to", False), ("valid_from", True)):
        if not row.get(field):
            continue
        try:
            stamp = datetime.fromisoformat(str(row[field]).replace("Z", "+00:00"))
            stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp
        except (ValueError, TypeError):
            return False
        if (future and stamp > now) or (not future and stamp <= now):
            return False
    return True


def _check_evidence(conn, scope, content: str, evidence: dict) -> dict:
    clause, params = scope_clause(scope)
    row = conn.execute("SELECT * FROM facts WHERE id=? " + clause,
                       (evidence.get("id"), *params)).fetchone()
    fact = dict(row) if row else {}
    if not _valid_fact(fact) or fact.get("fact_value", "").strip() != content:
        raise ValueError("Core refresh evidence is absent, unconfirmed or no longer current")
    if any(fact.get(k) != evidence.get(k) for k in ("version", "updated_at", "fact_value")):
        raise ValueError("Core refresh evidence changed concurrently")
    others = conn.execute(
        "SELECT * FROM facts WHERE 1=1 " + clause + " AND category='core_memory' "
        "AND fact_key='core_current_project' AND archived=0",
        params,
    ).fetchall()
    if any(_valid_fact(dict(r)) and r["fact_value"].strip() != content for r in others):
        raise ValueError("Core refresh evidence conflicts with another confirmed fact")
    return {"fact_id": fact["id"], "version": fact.get("version"),
            "source": fact.get("source"), "updated_at": fact.get("updated_at"),
            "sha256": hashlib.sha256(content.encode()).hexdigest()}


def record_revision(conn, scope, block_key, content, now, *, expected_content=None,
                    evidence_fact=None) -> None:
    """Called inside put_block's transaction; history and content commit together."""
    ensure_revision_schema(conn)
    # Acquire a write reservation before reading; no read-then-write CAS gap.
    conn.execute("UPDATE core_memory SET content=content WHERE 0")
    from ducky.core_memory import _visible_where, _owner_first_order
    where, args = _visible_where(scope)
    row = conn.execute(
        "SELECT content FROM core_memory WHERE " + where +
        " AND block_key_raw=? " + _owner_first_order() + " LIMIT 1",
        (*args, block_key, scope.user_id),
    ).fetchone()
    old = row[0] if row else ""
    if expected_content is not None and old != expected_content:
        raise ValueError("Core block changed concurrently; refresh deferred")
    if evidence_fact and block_key != "core_current_project":
        raise ValueError("Project evidence cannot update another core block")
    evidence = _check_evidence(conn, scope, content, evidence_fact) if evidence_fact else {"kind": "explicit_core_write"}
    conn.execute(
        "INSERT INTO core_memory_revisions "
        "(user_id,bank_id,block_key,old_content,new_content,evidence_json,created_at) VALUES (?,?,?,?,?,?,?)",
        (scope.user_id, scope.bank_id, block_key, old, content,
         json.dumps(evidence, ensure_ascii=False), now),
    )


def project_refresh(user_id: str, bank_id: str) -> dict:
    """Consume an explicit full project-state fact once; conflicting facts defer."""
    from ducky.core_memory import get_block, put_block
    scope = make_scope(user_id, bank_id)
    conn = get_facts_conn()
    clause, params = scope_clause(scope)
    rows = conn.execute(
        "SELECT * FROM facts WHERE 1=1 " + clause + " AND category='core_memory' "
        "AND fact_key='core_current_project' AND archived=0 ORDER BY id DESC LIMIT 100",
        params,
    ).fetchall()
    facts = [dict(r) for r in rows if _valid_fact(dict(r))]
    if not facts:
        return {"status": "no_confirmed_evidence"}
    if len({f["fact_value"].strip() for f in facts}) != 1:
        return {"status": "conflicting_evidence"}
    facts = [f for f in facts if _timestamp(f.get("updated_at")) is not None]
    if not facts:
        return {"status": "invalid_evidence_time"}
    fact = max(facts, key=lambda f: _timestamp(f["updated_at"]))
    block = get_block("core_current_project", scope.user_id, scope.bank_id)
    old = (block or {}).get("content", "")
    updated = _timestamp(fact["updated_at"])
    verified = _timestamp((block or {}).get("last_verified_at"), local_naive=True)
    if updated > datetime.now(timezone.utc):
        return {"status": "future_evidence_time"}
    if block and verified is None:
        return {"status": "invalid_block_time"}
    if verified and updated <= verified:
        return {"status": "not_newer"}
    result = put_block("core_current_project", fact["fact_value"], scope.user_id, scope.bank_id,
                       expected_content=old, evidence_fact=fact)
    return {"status": "updated", "fact_id": fact["id"], "updated_at": result["updated_at"]}


def refresh_project_blocks() -> dict:
    """Existing daily loop calls this; only explicit scoped state facts opt in."""
    conn = get_facts_conn()
    scopes = conn.execute(
        "SELECT DISTINCT user_id,bank_id FROM facts WHERE category='core_memory' "
        "AND fact_key='core_current_project' AND archived=0 LIMIT 1000"
    ).fetchall()
    counts: dict[str, int] = {}
    for user_id, bank_id in scopes:
        try:
            status = project_refresh(user_id, bank_id)["status"]
        except Exception as exc:
            logger.warning("Core project refresh deferred: %s", type(exc).__name__)
            status = "failed"
        counts[status] = counts.get(status, 0) + 1
    return counts


def revision_history(block_key: str, user_id: str, bank_id: str) -> list:
    scope = make_scope(user_id, bank_id)
    conn = get_facts_conn()
    ensure_revision_schema(conn)
    clause, params = scope_clause(scope)
    rows = conn.execute(
        "SELECT id,old_content,new_content,evidence_json,created_at FROM core_memory_revisions "
        "WHERE block_key=? " + clause + " ORDER BY id DESC LIMIT 50",
        (block_key, *params),
    ).fetchall()
    return [dict(r) for r in rows]
