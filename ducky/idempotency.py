"""Durable write idempotency for client retries."""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from ducky.utils import get_facts_conn

_SCHEMA = """
CREATE TABLE IF NOT EXISTS idempotency_keys (
    idempotency_key TEXT NOT NULL,
    user_id TEXT NOT NULL,
    bank_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    response_json TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (idempotency_key, user_id, bank_id)
)
"""
_PENDING_TTL_SECONDS = 600


def _fingerprint(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()


def claim(key: str, user_id: str, bank_id: str, fingerprint_payload: Any) -> dict:
    """Claim a write key. Returns replay/failure state or an empty claim state."""
    normalized = str(key or "").strip()
    if not normalized:
        return {"action": "new", "key": ""}
    fingerprint = _fingerprint(fingerprint_payload)
    now = time.time()
    conn = get_facts_conn()
    try:
        conn.execute(_SCHEMA)
        row = conn.execute(
            "SELECT fingerprint, response_json, created_at FROM idempotency_keys "
            "WHERE idempotency_key=? AND user_id=? AND bank_id=?",
            (normalized, user_id, bank_id),
        ).fetchone()
        # SQLite Row is available from the production connector, but this
        # module must also work with a plain tuple-based connection in tests.
        def _value(row: Any, key: str, index: int) -> Any:
            try:
                return row[key]
            except (IndexError, KeyError, TypeError):
                return row[index]
        if row is None:
            conn.execute(
                "INSERT INTO idempotency_keys "
                "(idempotency_key,user_id,bank_id,fingerprint,response_json,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (normalized, user_id, bank_id, fingerprint, None, now),
            )
            conn.commit()
            return {"action": "new", "key": normalized}
        if _value(row, "fingerprint", 0) != fingerprint:
            return {"action": "conflict", "key": normalized}
        response = _value(row, "response_json", 1)
        if response:
            return {"action": "replay", "key": normalized, "response": json.loads(response)}
        if now - float(_value(row, "created_at", 2) or 0) < _PENDING_TTL_SECONDS:
            return {"action": "pending", "key": normalized}
        # A crashed request older than TTL releases the key for retry.
        conn.execute(
            "DELETE FROM idempotency_keys WHERE idempotency_key=? AND user_id=? AND bank_id=?",
            (normalized, user_id, bank_id),
        )
        conn.execute(
            "INSERT INTO idempotency_keys "
            "(idempotency_key,user_id,bank_id,fingerprint,response_json,created_at) "
            "VALUES (?,?,?,?,?,?)",
            (normalized, user_id, bank_id, fingerprint, None, now),
        )
        conn.commit()
        return {"action": "new", "key": normalized}
    except Exception as exc:
        # Idempotency is a safety enhancement; it must not turn into a write outage.
        import traceback
        traceback.print_exc()
        return {"action": "disabled", "key": normalized, "error": str(exc)[:120]}
    finally:
        conn.close()


def finalize(key: str, user_id: str, bank_id: str, response: Any) -> None:
    key = str(key or "").strip()
    if not key:
        return
    conn = get_facts_conn()
    try:
        conn.execute(
            "UPDATE idempotency_keys SET response_json=? "
            "WHERE idempotency_key=? AND user_id=? AND bank_id=?",
            (json.dumps(response, ensure_ascii=False, default=str), key, user_id, bank_id),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()


def release(key: str, user_id: str, bank_id: str) -> None:
    key = str(key or "").strip()
    if not key:
        return
    conn = get_facts_conn()
    try:
        conn.execute(
            "DELETE FROM idempotency_keys WHERE idempotency_key=? AND user_id=? AND bank_id=?",
            (key, user_id, bank_id),
        )
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()
