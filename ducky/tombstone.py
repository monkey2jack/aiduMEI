"""
ducky.tombstone — tombstone 遗忘层 (v19.4.0 · Mímir 借鉴 B3)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

为什么需要这一层
    aiduMEI 的删除是物理删除：cascade_delete_memory 把一条记忆从
    mem0 向量库 / FTS5 / facts.db / salience.db / evolve_mem.db 五仓
    全部 DELETE FROM。一旦误删，原话与理由灰飞烟灭，不可恢复。

    Mímir §5.1 的做法是「遗忘不是删除」：活动检索不再返回，但历史
    内容与撤回理由永久保留。本层补上这一课——**别真删，留痕**。

设计取舍（五仓架构下的务实选择）
    五仓里 mem0 向量库是第三方基座，无法在其内部检索路径上加
    「tombstoned 过滤」而不动基座（违背「不碰 mem0 主体」纪律）。
    因此本层采用「删除前快照 + 物理删除 + 可恢复」：
      · 删除前把 facts 行全文 + FTS 原文 + 理由快照进 tombstones 表；
      · 物理删除照常执行（活动检索自然不再返回，无需改任何检索路径）；
      · 恢复时从 tombstones 快照回插 facts + 重建 FTS 索引。
    效果等价于软删（检索不返回 + 全文理由可查 + 一键恢复），
    但不动 mem0 一行代码、不改任何检索路径。与 verbatim 保真层同一灵魂。

设计原则（对齐 aiduMEI 既有纪律）
    · CREATE TABLE IF NOT EXISTS，对既有库 no-op，绝不 DROP
    · 租户硬隔离：快照与恢复都按 user_id 精确匹配
    · f0.4：读取未知态阻断可恢复删除；历史墓碑不自动清理
    · 数据物理位置：tombstones 表落 facts.db（沿用既有分库）

对外符号
    ensure_tombstone_schema()        建表（幂等）
    snapshot_before_delete(...)      删除前快照（cascade_delete_memory 调用）
    restore_tombstone(...)           从快照恢复一条记忆
    list_tombstones(...)             列某租户的遗忘记录（运维/验收用）
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

from ducky.utils import DEFAULT_USER_ID, get_facts_conn, get_text_conn
from ducky.bank_contract import (
    DEFAULT_BANK_ID,
    ensure_bank_registered,
    ensure_memory_banks_schema,
    make_scope,
    scoped_storage_key,
    table_columns,
    legacy_fact_scope_predicate,
)
from ducky.failure_ledger import feature_failed

logger = logging.getLogger("aiduMEM.tombstone")

_TOMBSTONES_DDL = """
CREATE TABLE IF NOT EXISTS tombstones (
    tombstone_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id        TEXT NOT NULL,
    target_type      TEXT DEFAULT 'memory',
    user_id          TEXT NOT NULL,
    bank_id          TEXT NOT NULL DEFAULT 'default',
    content_snapshot TEXT,
    -- f0.6 W-19（hark P1-1 / cue 恢复语义）：快照过期时刻（epoch 秒，NULL=永不过期）。
    -- 过期后 erase_expired_snapshots 把 content_snapshot 擦成 NULL、只留哈希链——
    -- 「可恢复」是有期限的承诺，不是永久明文保存。
    snapshot_expires_at INTEGER,
    facts_snapshot   TEXT,
    reason           TEXT DEFAULT '',
    actor            TEXT DEFAULT 'system',
    tombstoned_at    TEXT,
    restored_at      TEXT,
    vector_snapshot  TEXT
)
"""

_TOMBSTONE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_tombstone_user ON tombstones(user_id, bank_id)",
    "CREATE INDEX IF NOT EXISTS idx_tombstone_target ON tombstones(target_id)",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_tombstone_schema() -> None:
    """幂等建表。对既有库是 no-op，任何异常只记日志不抛。"""
    try:
        conn = get_facts_conn()
        outer_transaction = conn.in_transaction
        ensure_memory_banks_schema(conn)
        conn.execute(_TOMBSTONES_DDL)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(tombstones)").fetchall()}
            if "bank_id" not in cols:
                conn.execute(
                    "ALTER TABLE tombstones ADD COLUMN bank_id TEXT NOT NULL DEFAULT 'default'"
                )
            if "layer_snapshot" not in cols:
                conn.execute("ALTER TABLE tombstones ADD COLUMN layer_snapshot TEXT")
            # f0.3 (C7): the deleted point's vector payload (metadata only --
            # no embedding, no text) so a restore can rebuild the point the
            # way a normal write created it.  Additive; legacy rows stay NULL.
            if "vector_snapshot" not in cols:
                conn.execute("ALTER TABLE tombstones ADD COLUMN vector_snapshot TEXT")
            # f0.6 W-19：快照 TTL 列（存量行 NULL=沿用旧「不自动清理」语义，
            # 只对新建墓碑生效；运维可用 erase_expired_snapshots 全量兜底）
            if "snapshot_expires_at" not in cols:
                conn.execute("ALTER TABLE tombstones ADD COLUMN snapshot_expires_at INTEGER")
        except Exception as mexc:
            logger.debug("tombstone bank_id 迁移跳过: %s", mexc)
        for stmt in _TOMBSTONE_INDEXES:
            try:
                conn.execute(stmt)
            except Exception as exc:
                logger.debug("tombstone 索引跳过: %s", exc)
        if not outer_transaction:
            conn.commit()
    except Exception as exc:
        logger.warning("tombstones 建表跳过（服务继续）: %s", exc)


class SnapshotIncomplete(RuntimeError):
    """A source layer could not be read; absence has NOT been established."""


def facts_delete_selection(memory_id, scope):
    """The single predicate shared by the snapshot and physical DELETE."""
    from ducky.wal_engine import _raw_handle_hash
    raw_hash = _raw_handle_hash(memory_id)
    raw_key = f"raw:{raw_hash}" if raw_hash else None
    own, params = legacy_fact_scope_predicate(scope)
    where = "(id=? OR fact_key=? OR fact_key=? OR fact_key=? OR (? IS NOT NULL AND fact_key=?)) AND (1=1" + own + ")"
    return where, (memory_id, memory_id, f"fact:{memory_id}", f"raw:{memory_id}", raw_key, raw_key, *params)


def _capture_facts_rows(memory_id, user_id, bank_id=DEFAULT_BANK_ID):
    scope = make_scope(user_id, bank_id)
    conn = get_facts_conn()
    try:
        ensure_memory_banks_schema(conn)
        if not table_columns(conn, "facts"):
            return []
        where, params = facts_delete_selection(memory_id, scope)
        return [dict(r) for r in conn.execute("SELECT * FROM facts WHERE " + where, params).fetchall()]
    finally:
        conn.close()


def _capture_facts_row(memory_id, user_id, bank_id=DEFAULT_BANK_ID):
    """Compatibility text lookup. Deletion snapshots use ALL rows."""
    rows = _capture_facts_rows(memory_id, user_id, bank_id)
    return rows[0] if rows else None


def _capture_fts_rows(memory_id, user_id, bank_id=DEFAULT_BANK_ID):
    scope = make_scope(user_id, bank_id)
    conn = get_text_conn()
    try:
        if not table_columns(conn, "memories"):
            return []
        sid = scoped_storage_key(memory_id, scope)
        return [dict(r) for r in conn.execute(
            "SELECT * FROM memories WHERE id IN (?, ?) AND user_id=? AND bank_id=?",
            (sid, f"fact:{sid}", scope.user_id, scope.bank_id)).fetchall()]
    finally:
        conn.close()


def capture_verbatim_rows(memory_id, scope, content="", turn_ids=None):
    """Exact same id/hash predicate as the verbatim delete entry points; turn_ids narrows to the claimed rows."""
    from ducky.verbatim_vault import _content_hash
    conn = get_facts_conn()
    try:
        if not table_columns(conn, "verbatim_turns"):
            return []
        if str(memory_id).lower().startswith("verbatim:"):
            raw = str(memory_id).split(":", 1)[1]
            if not raw.isdigit():
                return []
            predicate, params = "id=?", (int(raw),)
        elif turn_ids is not None:       # 认领清单是具体行号（含多条消息拼成的记忆的逐条原文），按行号取
            claim = sorted({int(i) for i in turn_ids})
            if not claim:
                return []
            predicate, params = f"id IN ({','.join('?' * len(claim))})", tuple(claim)
        elif content.strip():
            predicate, params = "content_hash=?", (_content_hash(content.strip()),)
        else:
            return []
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM verbatim_turns WHERE user_id=? AND bank_id=? AND " + predicate,
            (scope.user_id, scope.bank_id, *params)).fetchall()]
        if turn_ids is not None:
            claim = {int(i) for i in turn_ids}
            rows = [r for r in rows if int(r["id"]) in claim]
        return rows
    finally:
        conn.close()


def _restore_verbatim_rows(conn, rows, scope):
    if not rows:
        return "not_applicable"
    from ducky.verbatim_vault import ensure_verbatim_schema
    ensure_verbatim_schema()
    conn.execute("SAVEPOINT restore_verbatim")
    tc = get_text_conn()
    try:
        valid = table_columns(conn, "verbatim_turns")
        for row in rows:
            if row.get("user_id") != scope.user_id or row.get("bank_id", "default") != scope.bank_id:
                raise ValueError("verbatim scope mismatch")
            cols = [k for k in row if k in valid]
            existing = conn.execute("SELECT * FROM verbatim_turns WHERE id=?", (row["id"],)).fetchone()
            if existing and any(existing[k] != row[k] for k in cols):
                raise ValueError("verbatim restore conflicts with a changed row")
            if not existing:
                conn.execute(f"INSERT INTO verbatim_turns ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", tuple(row[k] for k in cols))
            existing_index = tc.execute("SELECT content,user_id,bank_id FROM verbatim_fts_map WHERE turn_id=?", (row["id"],)).fetchone()
            expected = (row["content"], scope.user_id, scope.bank_id)
            if existing_index and tuple(existing_index) != expected:
                raise ValueError("verbatim index conflict")
            if not existing_index:
                tc.execute("INSERT INTO verbatim_fts_map(turn_id,content,user_id,bank_id) VALUES(?,?,?,?)", (row["id"], *expected))
        tc.commit()
        conn.execute("RELEASE restore_verbatim")
        return "restored"
    except Exception as exc:
        tc.rollback()
        conn.execute("ROLLBACK TO restore_verbatim")
        conn.execute("RELEASE restore_verbatim")
        return f"failed:{type(exc).__name__}"
    finally:
        tc.close()


def _capture_verbatim_content(
    memory_id: str,
    user_id: str,
    bank_id: str = DEFAULT_BANK_ID,
) -> str:
    """P0-4b：memory_id 形如 "verbatim:<n>" 时，从原文层抓正文做快照。

    否则删除原文条目时 tombstone 抓不到任何内容，快照被跳过，
    「误删可一键恢复」对原文层就不成立。
    """
    raw = str(memory_id or "")
    if not raw.lower().startswith("verbatim:"):
        return ""
    ident = raw.split(":", 1)[1].strip()
    if not ident.isdigit():
        return ""
    try:
        scope = make_scope(user_id, bank_id)
        conn = get_facts_conn()
        row = conn.execute(
            "SELECT content FROM verbatim_turns WHERE id=? AND user_id=? AND bank_id=? LIMIT 1",
            (int(ident), scope.user_id, scope.bank_id),
        ).fetchone()
        return row["content"] if row else ""
    except Exception as exc:
        from ducky.bank_contract import is_legacy_schema_error
        if is_legacy_schema_error(exc):
            return ""
        raise SnapshotIncomplete("verbatim snapshot unavailable") from exc


def _capture_fts_content(
    memory_id: str,
    user_id: str,
    bank_id: str = DEFAULT_BANK_ID,
) -> str:
    """从 text_fts.db 抓该记忆的原文内容；无则空串。"""
    rows = _capture_fts_rows(memory_id, user_id, bank_id)
    return rows[0].get("content", "") if rows else ""


_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# Payload keys rebuilt at restore time (text lives in content_snapshot).
_VECTOR_REBUILT_KEYS = frozenset({"data", "hash", "text_lemmatized", "updated_at"})


def _is_vector_point_id(target_id: Any) -> bool:
    """mem0 memory ids (and every point id this service writes) are UUIDs."""
    return bool(_UUID_RE.match(str(target_id or "").strip()))


def _point_payload(point: Any) -> dict:
    payload = getattr(point, "payload", None)
    if payload is None and isinstance(point, dict):
        payload = point.get("payload", point)
    return dict(payload) if isinstance(payload, dict) else {}


def _payload_in_scope(payload: dict, scope) -> bool:
    from ducky.bank_contract import vector_item_bank
    return (str(payload.get("user_id") or "") == scope.user_id
            and vector_item_bank(payload) == scope.bank_id)


def _capture_vector_payload(memory_id: str, scope) -> dict:
    """f0.3 (C7): the live point's payload, metadata only, *if it is in scope*.

    Never snapshots another tenant's point (vector_store.get is unscoped).
    Returns {} when there is no point, no backend, or the id is not a point id.
    """
    if not _is_vector_point_id(memory_id):
        return {}
    try:
        from ducky.mem0_runtime import get_memory
        _vs = get_memory().vector_store
        _skw = {"user_id": scope.user_id, "bank_id": scope.bank_id} if getattr(_vs, "is_native", False) else {}
        point = _vs.get(vector_id=str(memory_id), **_skw)
    except Exception as exc:
        from ducky.mem0_runtime import Mem0NotConfiguredError
        if isinstance(exc, Mem0NotConfiguredError):
            return {}
        raise SnapshotIncomplete("vector snapshot unavailable") from exc
    if point is None:
        return {}
    payload = _point_payload(point)
    if not payload or not isinstance(payload.get("user_id"), str) or not payload["user_id"]:
        raise SnapshotIncomplete("vector payload/ownership unavailable")
    if not _payload_in_scope(payload, scope):
        return {}
    if not isinstance(payload.get("data"), str):
        raise SnapshotIncomplete("vector source content unavailable")
    return payload


def _capture_raw_vectors(memory_id, scope):
    from ducky.wal_engine import _raw_handle_hash, _scoped_vector_items, _vector_ids_by_content_hash
    raw_hash = _raw_handle_hash(memory_id)
    if not raw_hash:
        return {}
    from ducky.mem0_runtime import get_memory, Mem0NotConfiguredError
    try:
        mem = get_memory()
    except Mem0NotConfiguredError:
        return {}
    items, complete = _scoped_vector_items(mem, scope)
    if not complete:
        raise SnapshotIncomplete("raw vector enumeration unavailable")
    out = {}
    for mid in _vector_ids_by_content_hash(items, scope.bank_id, raw_hash):
        _skw = ({"user_id": scope.user_id, "bank_id": scope.bank_id}
                if getattr(mem.vector_store, "is_native", False) else {})
        point = mem.vector_store.get(vector_id=mid, **_skw)
        payload = _point_payload(point)
        if not payload or not _payload_in_scope(payload, scope):
            raise SnapshotIncomplete("raw vector payload/ownership unavailable")
        if not isinstance(payload.get("data"), str):
            raise SnapshotIncomplete("raw vector source content unavailable")
        out[mid] = payload
    return out


def snapshot_before_delete(
    memory_id: str,
    user_id: str = DEFAULT_USER_ID,
    reason: str = "",
    actor: str = "system",
    bank_id: str = DEFAULT_BANK_ID,
    *, strict: bool = False,
    verbatim_text: str = "",
    verbatim_turn_ids: list | None = None,
) -> int | None:
    """删除前把一条记忆的全文 + 结构化行 + 理由快照进 tombstones 表。

    verbatim_text / verbatim_turn_ids：单删原文层的认领结果（wal_engine._verbatim_claim），
    快照与实际删除认领同一批原文行。

    返回 tombstone_id；任何层读取失败返回 None，strict=True 时抛 SnapshotIncomplete。
    由 cascade_delete_memory 在物理删除前调用。
    """
    if not memory_id or not str(memory_id).strip():
        return None
    try:
        scope = make_scope(user_id, bank_id)
        ensure_tombstone_schema()
        ensure_bank_registered(scope)
        facts_rows = _capture_facts_rows(memory_id, scope.user_id, scope.bank_id)
        facts_row = facts_rows[0] if facts_rows else None
        fts_rows = _capture_fts_rows(memory_id, scope.user_id, scope.bank_id)
        fts_content = (fts_rows[0].get("content", "") if fts_rows else "") or _capture_verbatim_content(
            memory_id, scope.user_id, scope.bank_id
        )
        raw_vectors = _capture_raw_vectors(memory_id, scope)
        vector_payload = _capture_vector_payload(memory_id, scope)
        if raw_vectors:
            vector_payload = next(iter(raw_vectors.values()))
        vector_text = str(vector_payload.get("data") or "").strip()
        # f0.7 复核（外审 haiku N1）：native 档不写 text_fts/facts 正文，原文行定位要退到内核正文；
        # 认领规则（haiku N3）由调用方传入，快照与实际删除认领的是同一批原文行。
        verbatim_rows = capture_verbatim_rows(
            memory_id, scope,
            fts_content or (facts_row or {}).get("fact_value", "") or verbatim_text or vector_text,
            turn_ids=verbatim_turn_ids)

        # 至少抓到一样东西才值得留快照；三者皆空说明这条记忆本就不在结构化仓里
        # （f0.3：向量点本身也算——FTS 索引缺腿的记忆照样留得住快照）
        if not facts_row and not fts_content and not vector_text and not verbatim_rows:
            logger.debug("tombstone 快照跳过（无结构化内容）: %s", memory_id)
            return None

        content_snapshot = fts_content or (facts_row or {}).get("fact_value", "") or vector_text
        # f0.6 W-19：快照 TTL——AIDUMEI_TOMBSTONE_TTL_DAYS（默认 30，0=永不过期）。
        # hark P1-1：永久明文 content_snapshot 让「可删除」承诺打折；到期擦正文留哈希。
        from ducky.env_config import int_env
        _ttl_days = int_env("AIDUMEI_TOMBSTONE_TTL_DAYS", 30, minimum=0)
        _snapshot_expires = int(time.time() + _ttl_days * 86400) if _ttl_days > 0 else None
        vector_meta = {k: v for k, v in vector_payload.items()
                       if k not in _VECTOR_REBUILT_KEYS}
        conn = get_facts_conn()
        cur = conn.execute(
            """INSERT INTO tombstones
               (target_id, target_type, user_id, bank_id, content_snapshot, facts_snapshot,
                reason, actor, tombstoned_at, vector_snapshot, layer_snapshot, snapshot_expires_at)
               VALUES (?, 'memory', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                memory_id,
                scope.user_id,
                scope.bank_id,
                content_snapshot,
                json.dumps(facts_rows, ensure_ascii=False, default=str) if facts_rows else "",
                reason or "",
                actor or "system",
                _now_iso(),
                json.dumps(vector_meta, ensure_ascii=False, default=str) if vector_payload else None,
                json.dumps({"version": 2, "facts": "complete", "fts": "complete",
                            "vector": "complete", "verbatim": "complete", "verbatim_rows": verbatim_rows, "fts_rows": fts_rows,
                            "vector_content": vector_text, "vector_points": raw_vectors}, ensure_ascii=False),
                _snapshot_expires,
            ),
        )
        # 📒 事件账本（B5）：与快照同事务留痕
        try:
            from ducky.event_ledger import content_hash, record_event
            record_event(conn, actor=actor or "system", action="tombstone",
                         target_id=memory_id, reason=reason or "",
                         after_hash=content_hash(content_snapshot),
                         user_id=scope.user_id, bank_id=scope.bank_id)
        except Exception as le:
            logger.debug("ledger 记录跳过: %s", le)
        conn.commit()
        tid = cur.lastrowid
        logger.info(
            "🪦 tombstone 快照 #%s (target=%s user=%s bank=%s reason=%r)",
            tid, memory_id, scope.user_id, scope.bank_id, reason,
        )
        return tid
    except Exception as exc:
        logger.warning("tombstone snapshot incomplete; deletion must wait: %s", exc)
        if strict:
            raise SnapshotIncomplete(str(exc)) from exc
        return None


def _load_vector_snapshot(row) -> dict:
    try:
        raw = row["vector_snapshot"]
    except (IndexError, KeyError):
        return {}
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _facts_row_present(conn, fr: dict, valid: set) -> bool:
    """A facts row equal to the snapshot's identity already exists (retry safety)."""
    keys = [k for k in ("category", "fact_key", "fact_value", "user_id", "bank_id")
            if k in valid and fr.get(k) is not None]
    if "fact_key" not in keys:
        return False
    sql = "SELECT 1 FROM facts WHERE " + " AND ".join(f"{k}=?" for k in keys) + " LIMIT 1"
    return conn.execute(sql, tuple(fr[k] for k in keys)).fetchone() is not None


def _fact_rows(snapshot):
    rows = json.loads(snapshot)
    rows = [rows] if isinstance(rows, dict) else rows
    if not isinstance(rows, list) or not all(isinstance(r, dict) and r for r in rows):
        raise ValueError("invalid facts snapshot")
    return rows


def _restore_facts_row(conn, facts_snapshot: str) -> str:
    """Legacy dict and f0.4 full row arrays, restored atomically and idempotently."""
    conn.execute("SAVEPOINT restore_facts")
    try:
        rows = _fact_rows(facts_snapshot)
        legacy = isinstance(json.loads(facts_snapshot), dict)
        valid = {r[1] for r in conn.execute("PRAGMA table_info(facts)").fetchall()}
        inserted = False
        for fr in rows:
            cols = [k for k in fr if k in valid and (k != "id" or not legacy)]
            if not cols:
                raise ValueError("empty snapshot columns")
            # Compare every restored field; equal text with a different category
            # or provenance must not silently count as this same fact.
            if not legacy:
                same = conn.execute("SELECT 1 FROM facts WHERE " + " AND ".join(f'"{c}" IS ?' for c in cols), tuple(fr[c] for c in cols)).fetchone()
            else:
                same = _facts_row_present(conn, fr, valid)
            if same:
                continue
            conn.execute(f"INSERT INTO facts ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", tuple(fr[c] for c in cols))
            inserted = True
        conn.execute("RELEASE restore_facts")
        return "restored" if inserted else "already_present"
    except Exception as exc:
        conn.execute("ROLLBACK TO restore_facts")
        conn.execute("RELEASE restore_facts")
        return f"failed:{type(exc).__name__}"


def _layer_snapshot(row):
    try:
        raw = row["layer_snapshot"]
    except (KeyError, IndexError):
        return {}
    return json.loads(raw) if raw else {}


def _bm25_text(content: str) -> str:
    """Same BM25 text field mem0 writes (falls back to the raw text)."""
    try:
        from mem0.utils.lemmatization import lemmatize_for_bm25
        return lemmatize_for_bm25(content) or content
    except (ImportError, OSError, RuntimeError, ValueError, TypeError) as exc:
        logger.debug("tombstone 恢复：BM25 词形化不可用，用原文: %s", exc)
        return content


def _restore_payload(row, content: str, scope, snapshot: dict, now: str) -> dict:
    """Payload in mem0's own write shape (data/hash/created_at/updated_at/
    text_lemmatized + metadata keys at top level), plus provenance."""
    payload = {k: v for k, v in snapshot.items() if k not in _VECTOR_REBUILT_KEYS}
    payload.update({
        "data": content,
        "hash": hashlib.md5(content.encode("utf-8"), usedforsecurity=False).hexdigest(),
        "created_at": snapshot.get("created_at") or row["tombstoned_at"] or now,
        "updated_at": now,
        "text_lemmatized": _bm25_text(content),
        "user_id": scope.user_id,
        "bank_id": scope.bank_id,
        "restored_from_tombstone": int(row["tombstone_id"]),
    })
    return payload


def _restore_cloud_point(mem, target_id: str, content: str, payload: dict, scope) -> str:
    """Upsert the point with its ORIGINAL id through the service's own
    embedder + vector store (the same insert mem0 and core-memory use).
    Raises on backend failure; the caller turns that into a retryable status."""
    _native = getattr(mem.vector_store, "is_native", False)
    _skw = {"user_id": scope.user_id, "bank_id": scope.bank_id} if _native else {}   # f0.7 P0-2
    existing = mem.vector_store.get(vector_id=target_id, **_skw)
    if existing is not None:
        # Idempotent: a previous (partial) restore or a live point.  Never
        # overwrite a point that belongs to another scope.
        if not _payload_in_scope(_point_payload(existing), scope):
            return "failed:id_owned_by_another_scope"
        return "already_present"
    if getattr(mem.vector_store, "is_native", False):
        vector = mem._embed(content, "add")   # native:嵌入失败照样落正文(FTS 可召回),与 /add 同口径
    else:
        vector = mem.embedding_model.embed(content, "add")
    mem.vector_store.insert(vectors=[vector], payloads=[payload], ids=[target_id])
    if mem.vector_store.get(vector_id=target_id, **_skw) is None:
        return "failed:not_readable_after_insert"
    return "restored"


def _restore_local_point(target_id: str, content: str, payload: dict) -> str:
    """Same-id local copy (dual index; soft: a failure is queued for replay)."""
    try:
        from ducky.dual_index import upsert_local
    except ImportError as exc:
        return f"failed:{type(exc).__name__}"
    return "restored" if upsert_local(target_id, content[:2000], payload) else "queued"


def _restore_aux_layers(mem, target_id: str, content: str, scope) -> dict:
    """Best-effort ledgers a normal write fills (not required for the stamp)."""
    out: dict = {}
    try:
        from ducky.memory_types import classify_and_sync_memory
        classify_and_sync_memory(target_id, content, user_id=scope.user_id,
                                 bank_id=scope.bank_id)
        out["memory_type"] = "restored"
    except (ImportError, sqlite3.Error, ValueError, TypeError, RuntimeError,
            AttributeError) as exc:
        out["memory_type"] = f"failed:{type(exc).__name__}"
    if hasattr(mem, "restore_copies"):          # f0.7 P5 自查:native 连带清掉的原文副本一并放回
        try:
            out["native_copies"] = mem.restore_copies(target_id, user_id=scope.user_id, bank_id=scope.bank_id)
        except (sqlite3.Error, ValueError) as exc:
            out["native_copies"] = f"failed:{type(exc).__name__}"
    from ducky.mem0_runtime import register_salience_for_add
    register_salience_for_add({"results": [{"id": target_id, "memory": content}]},
                              user_id=scope.user_id, bank_id=scope.bank_id)
    out["salience"] = "restored"
    out["entities"] = _relink_entities(mem, target_id, content, scope)
    return out


def _relink_entities(mem, target_id: str, content: str, scope) -> str:
    """mem0's entity collection links memories to extracted entities; normal
    LLM writes populate it (needs spaCy).  Re-link through mem0's own upsert."""
    upsert = getattr(mem, "_upsert_entity", None)
    if not callable(upsert):
        return "skipped:unsupported"
    try:
        from mem0.utils.entity_extraction import extract_entities
        entities = extract_entities(content) or []
    except (ImportError, OSError, RuntimeError, ValueError, TypeError) as exc:
        return f"skipped:{type(exc).__name__}"
    for entity_type, entity_text in entities:
        upsert(entity_text, entity_type, target_id, {"user_id": scope.user_id})
    return f"linked:{len(entities)}"


def _restore_vector_layers(row, content: str, scope) -> dict:
    """f0.3 (C7): the vector layer of a memory tombstone.

    Required (blocks the stamp) when the target is a vector point id and the
    engine's cloud leg is enabled -- exactly when a normal write would have
    created that point.  The local copy and aux ledgers are best-effort.
    """
    captured = _layer_snapshot(row).get("vector_points", {})
    if captured:
        outcomes = []
        for mid, payload in captured.items():
            child = dict(row)
            child.update(target_id=mid, layer_snapshot=None,
                         vector_snapshot=json.dumps({k: v for k, v in payload.items() if k not in _VECTOR_REBUILT_KEYS}))
            outcomes.append(_restore_vector_layers(child, str(payload.get("data", "")), scope))
        ok = all(o["ok"] for o in outcomes)
        return {"required": any(o["required"] for o in outcomes), "ok": ok,
                "layers": {"vector": "restored" if ok else "failed:raw_vector_set"},
                "point_verified": all(o["point_verified"] for o in outcomes),
                "points": outcomes}
    out = {"required": False, "ok": True, "layers": {}, "point_verified": None}
    target_id = str(row["target_id"] or "").strip()
    if (row["target_type"] or "memory") != "memory" or not _is_vector_point_id(target_id) \
            or not content.strip():
        out["layers"]["vector"] = "not_applicable"
        return out
    from ducky.engine_mode import cloud_leg_enabled, local_leg_enabled
    payload = _restore_payload(row, content, scope, _load_vector_snapshot(row), _now_iso())
    mem = None
    from ducky.mem0_runtime import native_kernel_active
    # f0.7 WP3b（实验机无 mem0 从零实测）：native 内核的 memory.db 是记忆真源，本地档也必须恢复进去；
    # 之前只看云腿，本地档恢复只回了 FTS/原文，记忆本体没回来（native 嵌入失败照样落正文，FTS 可召回）。
    if cloud_leg_enabled() or native_kernel_active():
        out["required"] = True
        try:
            from ducky.mem0_runtime import get_memory
            mem = get_memory()
            status = _restore_cloud_point(mem, target_id, content, payload, scope)
        except Exception as exc:  # init / embed / insert: all retryable, all reported
            logger.warning("tombstone 向量层恢复失败（本次不盖章，可重试）: %s", exc)
            status = f"failed:{type(exc).__name__}"
        out["layers"]["vector"] = status
        out["ok"] = status in ("restored", "already_present")
        out["point_verified"] = out["ok"]
    else:
        out["layers"]["vector"] = "skipped:engine_mode_local"
    if local_leg_enabled() and not native_kernel_active():   # native 无本地备胎索引
        out["required"] = True
        status = _restore_local_point(target_id, content, payload)
        out["layers"]["local_vector"] = status
        out["ok"] = out["ok"] and status in ("restored", "already_present")
    if out["ok"] and mem is not None:
        out["layers"].update(_restore_aux_layers(mem, target_id, content, scope))
    return out


def _verify_restore(conn, row, content: str, scope, vec: dict) -> dict:
    """Read the layers back (what the batch tool reports; not a status echo)."""
    check: dict = {"facts_row": None, "fts_row": None, "vector_point": vec.get("point_verified")}
    if row["facts_snapshot"]:
        try:
            valid = {r[1] for r in conn.execute("PRAGMA table_info(facts)").fetchall()}
            check["facts_row"] = all(_facts_row_present(conn, item, valid) for item in _fact_rows(row["facts_snapshot"]))
        except (sqlite3.Error, TypeError, ValueError):
            check["facts_row"] = False
    if content:
        check["fts_row"] = bool(_capture_fts_content(row["target_id"], scope.user_id,
                                                     scope.bank_id))
    return check


def restore_tombstone(
    tombstone_id: int,
    user_id: str = DEFAULT_USER_ID,
    bank_id: str = DEFAULT_BANK_ID,
) -> dict:
    """从 tombstones 快照恢复一条记忆：回插 facts + 重建 FTS 索引 + 向量层（f0.3）。

    返回 {restored, target_id, detail, status, layers, verification}。
    失败/无权限返回 restored=False。恢复只认未 restored_at 的快照，且严格按
    user_id 归属校验。status: ok | partial（可原样重试）| noop | error。
    """
    result = {"restored": False, "target_id": "", "detail": "", "status": "noop",
              "layers": {}, "verification": {}}
    if not tombstone_id:
        result["detail"] = "tombstone_id 为空"
        return result
    try:
        scope = make_scope(user_id, bank_id)
        ensure_tombstone_schema()
        ensure_bank_registered(scope)
        conn = get_facts_conn()
        row = conn.execute(
            "SELECT * FROM tombstones WHERE tombstone_id=? AND user_id=? "
            "AND bank_id=? AND restored_at IS NULL",
            (tombstone_id, scope.user_id, scope.bank_id),
        ).fetchone()
        if not row:
            result["detail"] = "快照不存在、已恢复或无权限"
            return result

        target_id = row["target_id"]
        facts_snapshot = row["facts_snapshot"] or ""
        content = row["content_snapshot"] or ""
        restored_cols = []
        layers = result["layers"]

        # v20.4.0（三方审计 P2-5 · Codex P2-03）：恢复不再有「部分成功也
        # 盖章」的语义。此前 facts 与 FTS 分别容错，无论谁失败都写
        # restored_at —— 一旦盖章，同一 tombstone 永远不能重试，缺的那一半
        # 就永远缺着。现在：必需组件（有快照就必须回插成功、有正文就必须
        # 重建索引成功）全部到位才盖章；有缺失则不盖章、返回 partial 明细，
        # 调用方可以修复环境后原样重试。
        # f0.3（C7）：向量点也是必需组件 —— 只回 facts+FTS 的「恢复」让记忆
        # 在向量召回里依旧不存在；每一层都幂等，partial 后原样重试是安全的。
        failed: list[str] = []

        # 1. 回插 facts（若有结构化快照）
        if facts_snapshot:
            layers["facts"] = _restore_facts_row(conn, facts_snapshot)
            if layers["facts"] in ("restored", "already_present"):
                restored_cols.append("facts")
            else:
                failed.append("facts(空快照列)" if layers["facts"] == "failed:empty" else "facts")

        # 2. 重建 FTS 索引（让混合召回能再搜到）
        if content:
            try:
                from ducky.text_fts import _index_memory
                snapshot_rows = _layer_snapshot(row).get("fts_rows") or []
                if snapshot_rows:
                    # Reinsert exact stored IDs: indexing with scoped_storage_key
                    # again would double-prefix fact aliases in named banks.
                    tc = get_text_conn()
                    try:
                        valid = table_columns(tc, "memories")
                        for item in snapshot_rows:
                            cols = [k for k in item if k in valid]
                            existing = tc.execute("SELECT * FROM memories WHERE id=?", (item["id"],)).fetchone()
                            if existing and any(existing[k] != item[k] for k in cols):
                                raise ValueError("FTS restore would overwrite changed row")
                            if not existing:
                                tc.execute(f"INSERT INTO memories ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", tuple(item[k] for k in cols))
                        tc.commit()
                    finally:
                        tc.close()
                else:
                    _index_memory(target_id, content, user_id=scope.user_id, bank_id=scope.bank_id)
                restored_cols.append("fts")
                layers["fts"] = "restored"
            except Exception as ie:
                failed.append("fts")
                layers["fts"] = f"failed:{type(ie).__name__}"
                feature_failed("index_memory", ie)
                logger.warning("tombstone FTS 重建失败（本次不盖章，可重试）: %s", ie)

        verbatim_rows = _layer_snapshot(row).get("verbatim_rows", [])
        if verbatim_rows:
            layers["verbatim"] = _restore_verbatim_rows(conn, verbatim_rows, scope)
            (restored_cols if layers["verbatim"] == "restored" else failed).append("verbatim")

        # 3. 向量层（f0.3 C7：原 id 重嵌入 + 同源写入，幂等）
        vec = _restore_vector_layers(row, _layer_snapshot(row).get("vector_content") or content, scope)
        layers.update(vec["layers"])
        if vec["required"]:
            (restored_cols if vec["ok"] else failed).append("vector")
        result["verification"] = _verify_restore(conn, row, content, scope, vec)

        if failed:
            conn.commit()  # 已成功的半边保留（每层都幂等，重试不重复写）
            result["restored"] = False
            result["status"] = "partial"
            result["target_id"] = target_id
            result["detail"] = (f"partial：成功 {','.join(restored_cols) or '无'}；"
                                f"失败 {','.join(failed)} —— 未盖 restored_at，可原样重试")
            logger.warning("🪦 tombstone #%s 恢复不完整（%s），保留可重试状态", tombstone_id, result["detail"])
            return result

        if not restored_cols:
            # P5 自审 S-7：快照已过期擦除（或本就无内容）——不盖 restored_at，列表里不许显示成「已恢复」
            conn.commit()
            result.update(restored=False, status="expired", target_id=target_id,
                          detail="快照已过期擦除，无可恢复内容")
            return result

        # 4. 标记已恢复（只有全部必需组件成功才走到这里）
        conn.execute(
            "UPDATE tombstones SET restored_at=? WHERE tombstone_id=?",
            (_now_iso(), tombstone_id),
        )
        # 📒 事件账本（B5）：与恢复同事务留痕
        try:
            from ducky.event_ledger import content_hash, record_event
            record_event(conn, actor=scope.user_id or "system", action="restore",
                         target_id=target_id, reason=f"tombstone#{tombstone_id}",
                         after_hash=content_hash(content),
                         user_id=scope.user_id, bank_id=scope.bank_id)
        except Exception as le:
            logger.debug("ledger 记录跳过: %s", le)
        conn.commit()

        result["restored"] = bool(restored_cols)
        result["status"] = "ok" if restored_cols else "noop"
        result["target_id"] = target_id
        result["detail"] = ",".join(restored_cols) if restored_cols else "无可恢复内容"
        logger.info("🪦→♻️ tombstone #%s 恢复 (target=%s via %s)", tombstone_id, target_id, result["detail"])
        return result
    except Exception as exc:
        feature_failed("index_memory", exc)
        logger.warning("tombstone 恢复失败: %s", exc)
        result["status"] = "error"
        result["detail"] = str(exc)[:120]
        return result


def list_tombstones(
    user_id: str = DEFAULT_USER_ID,
    limit: int = 50,
    bank_id: str = DEFAULT_BANK_ID,
) -> list:
    """列某租户的遗忘记录（运维/验收用）。失败返回 []。"""
    if not user_id:
        return []
    try:
        scope = make_scope(user_id, bank_id)
        ensure_tombstone_schema()
        conn = get_facts_conn()
        # f0.3 (C8): target_type is part of the listing so batch restore
        # tooling can select memory tombstones without reading the database.
        rows = conn.execute(
            "SELECT tombstone_id, target_id, target_type, content_snapshot, reason, actor, "
            "tombstoned_at, restored_at, user_id, bank_id "
            "FROM tombstones WHERE user_id=? AND bank_id=? "
            "ORDER BY tombstone_id DESC LIMIT ?",
            (scope.user_id, scope.bank_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.debug("list_tombstones 降级返回空: %s", exc)
        return []


# f0.7 复核（外审 sesame N-1）：到期要擦的不只 content_snapshot —— facts/vector/layer 快照里
# 同样有原文（verbatim_rows / fts_rows / vector_content）。到期后只留哈希与元数据。
_SNAPSHOT_TEXT_COLUMNS = ("content_snapshot", "facts_snapshot", "vector_snapshot", "layer_snapshot")


def _erase_expired_in(conn) -> int:
    cols = [c for c in _SNAPSHOT_TEXT_COLUMNS
            if c in {r[1] for r in conn.execute("PRAGMA table_info(tombstones)")}]
    if not cols or "snapshot_expires_at" not in {r[1] for r in conn.execute("PRAGMA table_info(tombstones)")}:
        return 0
    sets = ", ".join(f"{c} = NULL" for c in cols)
    any_left = " OR ".join(f"{c} IS NOT NULL" for c in cols)
    return conn.execute(f"UPDATE tombstones SET {sets} WHERE ({any_left})"
                        " AND snapshot_expires_at IS NOT NULL AND snapshot_expires_at < ?",
                        (int(time.time()),)).rowcount or 0


def _erase_expired_native_snapshots() -> int:
    """f0.7 P0-3：native 内核 memory.db 里的墓碑同受 TTL 约束（此前没有过期列，永久留原文）。"""
    import sqlite3 as _sq
    from ducky.memory_kernel.impl_native import default_db_path
    path = default_db_path()
    if not os.path.exists(path):
        return 0
    c = _sq.connect(path, timeout=15)
    try:
        n = _erase_expired_in(c)
        c.commit()
        return n
    finally:
        c.close()


def erase_expired_snapshots() -> dict:
    """f0.6 W-19：擦除过期墓碑快照的正文（content/facts/vector/layer 四列），只留哈希与元数据。

    定期任务/运维手动调用。到期判定：snapshot_expires_at IS NOT NULL AND
    < now。存量 NULL 行（旧「不自动清理」语义）不动。返回擦除条数供巡检。
    """
    conn = get_facts_conn()
    try:
        n = _erase_expired_in(conn)
        conn.commit()
        n_native = _erase_expired_native_snapshots()
        if n or n_native:
            logger.info("🪦 已擦除 %d 条过期墓碑快照正文（TTL 到期，哈希与元数据保留）", n + n_native)
        return {"erased": n, "erased_native": n_native}
    except Exception as exc:
        conn.rollback()
        logger.warning("过期快照擦除失败: %s", exc)
        return {"erased": 0, "error": str(exc)}
    finally:
        conn.close()
