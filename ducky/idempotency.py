"""Durable write idempotency for client retries."""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from typing import Any

from ducky import utils as _utils

logger = logging.getLogger("aiduMEM.idempotency")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS idempotency_keys (
    idempotency_key TEXT NOT NULL,
    user_id TEXT NOT NULL,
    bank_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    response_json TEXT,
    created_at REAL NOT NULL,
    state TEXT,
    PRIMARY KEY (idempotency_key, user_id, bank_id)
)
"""
_PENDING_TTL_SECONDS = 600

# f0.3 (C2): receipt states.  NULL is a legacy (pre-f0.3) row.
#   accepted -- async hand-off receipt.  It is *provisional*: replayable only
#               while the job lease (_PENDING_TTL_SECONDS) is alive, because
#               the job lives in process memory and a restart loses it.
#   done     -- durable outcome written by the job (or by a sync request).
# A job that fails releases its key, so a retry executes instead of being
# answered "accepted" forever (the S-2/S-8 defect).
STATE_ACCEPTED = "accepted"
STATE_DONE = "done"

# Everything below runs SQLite on a dedicated short connection; these are the
# failure shapes it has (lock / I/O / corrupt row).  f0.3 narrowed the former
# bare `except Exception` handlers to them (except-density ratchet).
_DB_ERRORS = (sqlite3.Error, OSError)
_WORK_ERRORS = (sqlite3.Error, OSError, ValueError, TypeError)


class ClaimState(dict):
    """claim() result.  The mapping is the public contract; ``claimed_at`` is
    an out-of-band claim token (the row's created_at) so an async job can
    settle exactly the claim it inherited -- kept off the mapping so existing
    consumers and equality checks see the same dict as before."""

    claimed_at: float | None = None


def _new_claim(key: str, now: float) -> ClaimState:
    state = ClaimState(action="new", key=key)
    state.claimed_at = now
    return state


def claim_token(state: Any) -> float | None:
    """The claim token of a claim() result (None for plain/legacy mappings)."""
    return getattr(state, "claimed_at", None)

# f0.3 (H-10): settled receipts expire.  Validated through env_config
# (invalid values fall back to the default and are reported, never raised).
_TTL_DEFAULT_DAYS = 7
_TTL_MAX_DAYS = 3650
_PURGE_INTERVAL_SECONDS = 3600.0
_purge_lock = threading.Lock()
_last_purge: dict = {"at": 0.0, "db": ""}

# f0.3: stored receipts never carry user content.  A replay only needs ids
# and states; previews / merged text / extracted memories stay out of the
# idempotency table (which otherwise outlives a delete of that content).
_REDACT_KEYS = frozenset({
    "preview", "text_preview", "payload_preview", "messages", "message_text",
    "memory", "memories", "data", "content", "text", "summary", "merged",
    "merged_text", "parts", "old_memory", "new_memory", "prev_value",
    "previous_memory", "vision_caption", "snippet", "raw",
})

# v20.4.0（Kimi P3-2/P3-3）：幂等层改用**独立短连接** + 建表 once 化。
# 原实现借请求线程的共享连接（get_facts_conn 的 _ConnProxy），claim 里的
# commit/rollback 会连带提交/回滚同一请求在途的其它 facts 写入 ——
# utils 花大篇幅治理的「悬挂事务」雷区，幂等层自己踩在同一形态上。
# 独立连接物理隔离事务边界；每请求一次的 CREATE TABLE 也一并消掉
# （按库路径记账：测试沙箱切库后新库仍会建表）。
_schema_lock = threading.Lock()
_schema_ready_for: str = ""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_utils.FACTS_DB, timeout=10.0)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    global _schema_ready_for
    db_path = str(_utils.FACTS_DB)
    if _schema_ready_for == db_path:
        return
    with _schema_lock:
        if _schema_ready_for == db_path:
            return
        conn.execute(_SCHEMA)
        # f0.3: additive state column (NULL = legacy row); never rebuild.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(idempotency_keys)")}
        if "state" not in cols:
            conn.execute("ALTER TABLE idempotency_keys ADD COLUMN state TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_idempotency_created "
                     "ON idempotency_keys(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_idempotency_scope "
                     "ON idempotency_keys(user_id, bank_id)")
        conn.commit()
        _schema_ready_for = db_path


def _fingerprint(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()


def ttl_days() -> int:
    """Settled-receipt lifetime in days (AIDUMEI_IDEMPOTENCY_TTL_DAYS, default 7)."""
    from ducky.env_config import int_env
    return int_env("AIDUMEI_IDEMPOTENCY_TTL_DAYS", _TTL_DEFAULT_DAYS,
                   minimum=1, maximum=_TTL_MAX_DAYS)


def ttl_seconds() -> float:
    return float(ttl_days()) * 86400.0


def redact_receipt(value: Any) -> Any:
    """Return a copy of a receipt without content-bearing fields (recursive)."""
    if isinstance(value, dict):
        return {k: redact_receipt(v) for k, v in value.items()
                if str(k) not in _REDACT_KEYS}
    if isinstance(value, list):
        return [redact_receipt(v) for v in value]
    return value


def _row_value(row: Any, key: str, index: int) -> Any:
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        try:
            return row[index]
        except (IndexError, KeyError, TypeError):
            return None


def _is_provisional(state: Any, response: Any) -> bool:
    """accepted rows, and legacy rows whose receipt said durable:false."""
    if state == STATE_ACCEPTED:
        return True
    if state is None and isinstance(response, dict):
        return response.get("durable") is False
    return False


def _row_verdict(row: Any, now: float) -> str:
    """replay | pending | expired for an existing row with the same fingerprint."""
    created = float(_row_value(row, "created_at", 2) or 0)
    raw = _row_value(row, "response_json", 1)
    if not raw:
        return "pending" if now - created < _PENDING_TTL_SECONDS else "expired"
    try:
        response = json.loads(raw)
    except (TypeError, ValueError):
        return "expired"
    if _is_provisional(_row_value(row, "state", 3), response):
        # The async job owning this receipt lives in process memory; past the
        # lease it is presumed lost (restart / eviction) and a retry must run.
        return "replay" if now - created < _PENDING_TTL_SECONDS else "expired"
    return "replay" if now - created < ttl_seconds() else "expired"


def purge_expired(now: float | None = None, conn: sqlite3.Connection | None = None) -> int:
    """Delete receipts older than the TTL.  Returns the number of rows removed."""
    now = time.time() if now is None else now
    own = conn is None
    if own:
        conn = _connect()
    try:
        cur = conn.execute("DELETE FROM idempotency_keys WHERE created_at < ?",
                           (now - ttl_seconds(),))
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        if own:
            conn.close()


def _maybe_purge(conn: sqlite3.Connection, now: float) -> None:
    """Opportunistic TTL cleanup: at most once per interval per database."""
    db = str(_utils.FACTS_DB)
    with _purge_lock:
        if _last_purge["db"] == db and now - _last_purge["at"] < _PURGE_INTERVAL_SECONDS:
            return
        _last_purge.update(at=now, db=db)
    try:
        removed = purge_expired(now, conn)
        if removed:
            logger.info("idempotency TTL purge removed %d expired receipts", removed)
    except sqlite3.Error as exc:
        logger.warning("idempotency TTL purge skipped: %s", exc)


def delete_scope(user_id: str, bank_id: str) -> int:
    """delete_all leg: drop every receipt of exactly (user_id, bank_id)."""
    conn = _connect()
    try:
        cur = conn.execute(
            "DELETE FROM idempotency_keys WHERE user_id=? AND bank_id=?",
            (user_id, bank_id))
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def claim(key: str, user_id: str, bank_id: str, fingerprint_payload: Any) -> dict:
    """Claim a write key. Returns replay/failure state or an empty claim state."""
    normalized = str(key or "").strip()
    if not normalized:
        return {"action": "new", "key": ""}
    fingerprint = _fingerprint(fingerprint_payload)
    now = time.time()
    try:
        conn = _connect()
    except _DB_ERRORS as exc:
        logger.error("幂等层不可用（本次请求按无幂等处理，可能重复落库）：%s", exc)
        return {"action": "disabled", "key": normalized, "error": str(exc)[:120]}
    try:
        # f0.3 (H-10): opportunistic TTL cleanup rides on real traffic.
        _maybe_purge(conn, now)
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
            # claimed_at is the claim token: settle/release of an async job
            # only touches the row it claimed (a later takeover is not ours).
            return _new_claim(normalized, now)
        row = conn.execute(
            "SELECT fingerprint, response_json, created_at, state FROM idempotency_keys "
            "WHERE idempotency_key=? AND user_id=? AND bank_id=?",
            (normalized, user_id, bank_id),
        ).fetchone()
        if row is None:
            # 抢占失败却读不到行：对手在这两步之间 release 了。让调用方稍后重试。
            return {"action": "pending", "key": normalized}
        verdict = _row_verdict(row, now)
        if _row_value(row, "fingerprint", 0) != fingerprint and verdict != "expired":
            return {"action": "conflict", "key": normalized}
        if verdict == "replay":
            response = json.loads(_row_value(row, "response_json", 1))
            return {"action": "replay", "key": normalized,
                    "response": redact_receipt(response),
                    "state": _row_value(row, "state", 3)}
        if verdict == "pending":
            return {"action": "pending", "key": normalized}
        # 过期（pending 租约 / 异步 accepted 租约 / 已结清回执超 TTL）：条件
        # UPDATE 接管 —— created_at 仍是旧值才算抢到，两个同时发现「过期」的
        # 请求只有一个 rowcount==1。
        old_created = float(_row_value(row, "created_at", 2) or 0)
        cur = conn.execute(
            "UPDATE idempotency_keys SET fingerprint=?, response_json=NULL, state=NULL, "
            "created_at=? WHERE idempotency_key=? AND user_id=? AND bank_id=? "
            "AND created_at=?",
            (fingerprint, now, normalized, user_id, bank_id, old_created),
        )
        conn.commit()
        if cur.rowcount == 1:
            return _new_claim(normalized, now)
        return {"action": "pending", "key": normalized}
    except _WORK_ERRORS as exc:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        logger.error("幂等层不可用（本次请求按无幂等处理，可能重复落库）：%s", exc)
        return {"action": "disabled", "key": normalized, "error": str(exc)[:120]}
    finally:
        conn.close()


def _settle_sql(provisional: bool, claimed_at: float | None) -> str:
    """UPDATE for a receipt.  A provisional (accepted) receipt never replaces a
    settled one; a durable receipt may replace pending or provisional rows
    of *its own* claim (created_at token) but not a later takeover."""
    sql = ("UPDATE idempotency_keys SET response_json=?, state=? "
           "WHERE idempotency_key=? AND user_id=? AND bank_id=?")
    if claimed_at is not None:
        sql += " AND created_at=?"
        sql += (" AND response_json IS NULL" if provisional else
                " AND (response_json IS NULL OR state='accepted')")
    elif provisional:
        sql += " AND response_json IS NULL"
    return sql


def finalize(key: str, user_id: str, bank_id: str, response: Any, *,
             claimed_at: float | None = None, provisional: bool = False) -> None:
    """Store the receipt for ``key``.

    f0.3 (C2): ``provisional=True`` is the async hand-off ("accepted", not
    durable yet); the job later settles it with :func:`settle_job`.  The
    stored receipt is always passed through :func:`redact_receipt`.
    """
    key = str(key or "").strip()
    if not key:
        return
    try:
        conn = _connect()
    except _DB_ERRORS as exc:
        logger.error("幂等 finalize 失败（key=%s），释放该 key 以免客户端被永久 409：%s", key, exc)
        release(key, user_id, bank_id, claimed_at=claimed_at)
        return
    try:
        params = [json.dumps(redact_receipt(response), ensure_ascii=False, default=str),
                  STATE_ACCEPTED if provisional else STATE_DONE, key, user_id, bank_id]
        if claimed_at is not None:
            params.append(claimed_at)
        conn.execute(_settle_sql(provisional, claimed_at), params)
        conn.commit()
    except _WORK_ERRORS as exc:
        # v20.3.2 正式版（P1-10）：原来是裸 pass。finalize 失败（典型：database is
        # locked）会把 key 留成 response_json=NULL —— 之后 10 分钟内同 key 合法重试
        # 全部 409。写已经落库了，宁可放弃「重放」也不能把客户端锁死：释放该 key。
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        logger.error("幂等 finalize 失败（key=%s），释放该 key 以免客户端被永久 409：%s", key, exc)
        release(key, user_id, bank_id, claimed_at=claimed_at)
    finally:
        conn.close()


def release(key: str, user_id: str, bank_id: str, *,
            claimed_at: float | None = None) -> None:
    """Drop a claim so a retry executes.

    With ``claimed_at`` (async jobs) only the claim this caller owns is
    dropped, and never a settled ("done") receipt.
    """
    key = str(key or "").strip()
    if not key:
        return
    try:
        conn = _connect()
    except _DB_ERRORS as exc:
        logger.warning("幂等 release 失败（key=%s）：%s", key, exc)
        return
    try:
        sql = "DELETE FROM idempotency_keys WHERE idempotency_key=? AND user_id=? AND bank_id=?"
        params: list = [key, user_id, bank_id]
        if claimed_at is not None:
            sql += " AND created_at=? AND (response_json IS NULL OR state='accepted')"
            params.append(claimed_at)
        conn.execute(sql, params)
        conn.commit()
    except _DB_ERRORS as exc:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        logger.warning("幂等 release 失败（key=%s）：%s", key, exc)
    finally:
        conn.close()


# -- f0.3 (C2): async job ownership -------------------------------------------

def job_binding(state: dict, key: str, user_id: str, bank_id: str) -> dict | None:
    """What an async job needs to settle the claim it inherits (None = no key)."""
    normalized = str(key or "").strip()
    if not normalized or (state or {}).get("action") != "new":
        return None
    return {"key": normalized, "user_id": user_id, "bank_id": bank_id,
            "claimed_at": claim_token(state)}


def _durable_receipt(result: Any, job_id: str, key: str) -> dict:
    res = result if isinstance(result, dict) else {}
    receipt = {"status": str(res.get("status") or "ok"), "durable": True,
               "action": res.get("action") or "async_done",
               "job_id": job_id, "request_id": key}
    for name in ("distillation", "infer", "engine_mode", "coalesce_follower",
                 "primary_job_id"):
        if name in res:
            receipt[name] = res[name]
    return receipt


def settle_job(binding: dict | None, *, ok: bool, result: Any = None,
               job_id: str = "") -> str:
    """Settle the claim of a finished async job.

    ok=True  -> the provisional receipt becomes the durable outcome;
    ok=False -> the claim is released so the client's retry executes
                (a failed async write must never replay as "accepted").
    Returns "finalized" | "released" | "skipped".
    """
    if not binding or not binding.get("key"):
        return "skipped"
    key = binding["key"]
    user_id = binding.get("user_id") or ""
    bank_id = binding.get("bank_id") or ""
    claimed_at = binding.get("claimed_at")
    if ok:
        finalize(key, user_id, bank_id, _durable_receipt(result, job_id, key),
                 claimed_at=claimed_at)
        return "finalized"
    release(key, user_id, bank_id, claimed_at=claimed_at)
    return "released"
