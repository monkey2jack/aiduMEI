"""Durable write idempotency for client retries."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any

from ducky.utils import get_facts_conn

logger = logging.getLogger("aiduMEM.idempotency")

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
        # v20.3.2 正式版（P1-10 · Codex F-01 / Gemini P1-3）：原实现 SELECT→INSERT
        # 两步，两个并发请求都读到 None、都 INSERT（第二个才撞主键，且撞了走
        # except → "disabled" → 业务照写）。改为一条 INSERT ... ON CONFLICT DO NOTHING
        # 原子抢占：rowcount==1 才是 new，其余一律回头读行判定。
        cur = conn.execute(
            "INSERT INTO idempotency_keys "
            "(idempotency_key,user_id,bank_id,fingerprint,response_json,created_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(idempotency_key,user_id,bank_id) DO NOTHING",
            (normalized, user_id, bank_id, fingerprint, None, now),
        )
        conn.commit()
        if cur.rowcount == 1:
            return {"action": "new", "key": normalized}
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
            # 抢占失败却读不到行：对手在这两步之间 release 了。让调用方稍后重试。
            return {"action": "pending", "key": normalized}
        if _value(row, "fingerprint", 0) != fingerprint:
            return {"action": "conflict", "key": normalized}
        response = _value(row, "response_json", 1)
        if response:
            return {"action": "replay", "key": normalized, "response": json.loads(response)}
        if now - float(_value(row, "created_at", 2) or 0) < _PENDING_TTL_SECONDS:
            return {"action": "pending", "key": normalized}
        # 过期的 pending：条件 UPDATE 接管 —— created_at 仍是旧值且仍无响应才算抢到，
        # 两个同时发现「过期」的请求只有一个 rowcount==1。
        cur = conn.execute(
            "UPDATE idempotency_keys SET fingerprint=?, response_json=NULL, created_at=? "
            "WHERE idempotency_key=? AND user_id=? AND bank_id=? "
            "AND response_json IS NULL AND created_at=?",
            (fingerprint, now, normalized, user_id, bank_id, float(_value(row, "created_at", 2) or 0)),
        )
        conn.commit()
        if cur.rowcount == 1:
            return {"action": "new", "key": normalized}
        return {"action": "pending", "key": normalized}
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.error("幂等层不可用（本次请求按无幂等处理，可能重复落库）：%s", exc)
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
    except Exception as exc:
        # v20.3.2 正式版（P1-10）：原来是裸 pass。finalize 失败（典型：database is
        # locked）会把 key 留成 response_json=NULL —— 之后 10 分钟内同 key 合法重试
        # 全部 409。写已经落库了，宁可放弃「重放」也不能把客户端锁死：释放该 key。
        try:
            conn.rollback()
        except Exception:
            pass
        logger.error("幂等 finalize 失败（key=%s），释放该 key 以免客户端被永久 409：%s", key, exc)
        release(key, user_id, bank_id)
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
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning("幂等 release 失败（key=%s）：%s", key, exc)
    finally:
        conn.close()
