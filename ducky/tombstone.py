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
    · 失败干净降级：快照失败只记日志，绝不阻断删除主链路
      （宁可少一份快照，也不能让删除卡死）
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
import re
import sqlite3
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
        ensure_memory_banks_schema(conn)
        conn.execute(_TOMBSTONES_DDL)
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(tombstones)").fetchall()}
            if "bank_id" not in cols:
                conn.execute(
                    "ALTER TABLE tombstones ADD COLUMN bank_id TEXT NOT NULL DEFAULT 'default'"
                )
            # f0.3 (C7): the deleted point's vector payload (metadata only --
            # no embedding, no text) so a restore can rebuild the point the
            # way a normal write created it.  Additive; legacy rows stay NULL.
            if "vector_snapshot" not in cols:
                conn.execute("ALTER TABLE tombstones ADD COLUMN vector_snapshot TEXT")
        except Exception as mexc:
            logger.debug("tombstone bank_id 迁移跳过: %s", mexc)
        for stmt in _TOMBSTONE_INDEXES:
            try:
                conn.execute(stmt)
            except Exception as exc:
                logger.debug("tombstone 索引跳过: %s", exc)
        conn.commit()
    except Exception as exc:
        logger.warning("tombstones 建表跳过（服务继续）: %s", exc)


def _capture_facts_row(
    memory_id: str,
    user_id: str,
    bank_id: str = DEFAULT_BANK_ID,
) -> dict | None:
    """从 facts.db 抓该记忆的结构化行（全列），返回 dict；无则 None。"""
    try:
        scope = make_scope(user_id, bank_id)
        conn = get_facts_conn()
        ensure_memory_banks_schema(conn)
        exact_keys = (memory_id, f"fact:{memory_id}", f"raw:{memory_id}")
        # 同 conflict_resolver：手写 PRAGMA + 宽捕获会把真故障翻译成
        # 「facts 没有 user_id/source/agent_id 列」，于是 owner_terms 全空，
        # 墓碑查询退化成不认所有者 —— 统一走已加固的 table_columns。
        cols = table_columns(conn, "facts")
        owner_terms = ["user_id=?"] if "user_id" in cols else []
        owner_params = [scope.user_id] if "user_id" in cols else []
        if "source" in cols:
            owner_terms.append("source=?")
            owner_params.append(scope.user_id)
        if "agent_id" in cols:
            owner_terms.append("agent_id=?")
            owner_params.append(scope.user_id)
        scope_terms = []
        scope_params = []
        if "bank_id" in cols:
            scope_terms.append("bank_id=?")
            scope_params.append(scope.bank_id)
        if owner_terms:
            scope_terms.append("(" + " OR ".join(owner_terms) + ")")
        key_clause = "(id=? OR fact_key=? OR fact_key=? OR fact_key=?)"
        sql = "SELECT * FROM facts WHERE " + key_clause
        if scope_terms:
            sql += " AND " + " AND ".join(scope_terms)
        sql += " LIMIT 1"
        row = conn.execute(
            sql,
            [memory_id, exact_keys[0], exact_keys[1], exact_keys[2]]
            + scope_params
            + owner_params,
        ).fetchone()
        return dict(row) if row else None
    except Exception as exc:
        logger.debug("tombstone facts 快照跳过: %s", exc)
        return None


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
        logger.debug("tombstone 原文快照跳过: %s", exc)
        return ""


def _capture_fts_content(
    memory_id: str,
    user_id: str,
    bank_id: str = DEFAULT_BANK_ID,
) -> str:
    """从 text_fts.db 抓该记忆的原文内容；无则空串。"""
    try:
        scope = make_scope(user_id, bank_id)
        tconn = get_text_conn()
        # text_fts v20 stores a scoped internal id for named banks.  Query
        # both the scoped and legacy forms, always with exact user/bank
        # predicates; this also fixes the old default-user cross-bank leak.
        scoped_id = scoped_storage_key(memory_id, scope)
        row = tconn.execute(
            "SELECT content FROM memories WHERE id IN (?, ?) "
            "AND user_id=? AND bank_id=? LIMIT 1",
            (scoped_id, f"fact:{scoped_id}", scope.user_id, scope.bank_id),
        ).fetchone()
        return row["content"] if row else ""
    except Exception as exc:
        logger.debug("tombstone FTS 快照跳过: %s", exc)
        return ""


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
        point = get_memory().vector_store.get(vector_id=str(memory_id))
    except Exception as exc:  # no backend / backend down: tombstone without it
        logger.debug("tombstone 向量快照跳过: %s", exc)
        return {}
    payload = _point_payload(point) if point is not None else {}
    if not payload or not _payload_in_scope(payload, scope):
        return {}
    return payload


def snapshot_before_delete(
    memory_id: str,
    user_id: str = DEFAULT_USER_ID,
    reason: str = "",
    actor: str = "system",
    bank_id: str = DEFAULT_BANK_ID,
) -> int | None:
    """删除前把一条记忆的全文 + 结构化行 + 理由快照进 tombstones 表。

    返回 tombstone_id；快照失败返回 None（绝不抛异常阻断删除主链路）。
    由 cascade_delete_memory 在物理删除前调用。
    """
    if not memory_id or not str(memory_id).strip():
        return None
    try:
        scope = make_scope(user_id, bank_id)
        ensure_tombstone_schema()
        ensure_bank_registered(scope)
        facts_row = _capture_facts_row(memory_id, scope.user_id, scope.bank_id)
        fts_content = _capture_fts_content(memory_id, scope.user_id, scope.bank_id) or _capture_verbatim_content(
            memory_id, scope.user_id, scope.bank_id
        )
        vector_payload = _capture_vector_payload(memory_id, scope)
        vector_text = str(vector_payload.get("data") or "").strip()

        # 至少抓到一样东西才值得留快照；三者皆空说明这条记忆本就不在结构化仓里
        # （f0.3：向量点本身也算——FTS 索引缺腿的记忆照样留得住快照）
        if not facts_row and not fts_content and not vector_text:
            logger.debug("tombstone 快照跳过（无结构化内容）: %s", memory_id)
            return None

        content_snapshot = fts_content or (facts_row or {}).get("fact_value", "") or vector_text
        vector_meta = {k: v for k, v in vector_payload.items()
                       if k not in _VECTOR_REBUILT_KEYS}
        conn = get_facts_conn()
        cur = conn.execute(
            """INSERT INTO tombstones
               (target_id, target_type, user_id, bank_id, content_snapshot, facts_snapshot,
                reason, actor, tombstoned_at, vector_snapshot)
               VALUES (?, 'memory', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                memory_id,
                scope.user_id,
                scope.bank_id,
                content_snapshot,
                json.dumps(facts_row, ensure_ascii=False, default=str) if facts_row else "",
                reason or "",
                actor or "system",
                _now_iso(),
                json.dumps(vector_meta, ensure_ascii=False, default=str) if vector_payload else None,
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
        logger.warning("tombstone 快照降级（删除继续）: %s", exc)
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
    keys = [k for k in ("fact_key", "fact_value", "user_id", "bank_id")
            if k in valid and fr.get(k) is not None]
    if "fact_key" not in keys:
        return False
    sql = "SELECT 1 FROM facts WHERE " + " AND ".join(f"{k}=?" for k in keys) + " LIMIT 1"
    return conn.execute(sql, tuple(fr[k] for k in keys)).fetchone() is not None


def _restore_facts_row(conn, facts_snapshot: str) -> str:
    """restored | already_present | failed:<why>.  Idempotent across retries:
    a partial restore used to re-insert the same fact on every retry."""
    try:
        fr = json.loads(facts_snapshot)
        # 剔除自增主键与快照元字段，让 facts 表重新分配 id
        fr.pop("id", None)
        # v21.1（WP-10）：cols 来自快照 JSON 键，若快照被污染则成标识符注入面——
        # 用 facts 表实际列做白名单，剔除未知列（防御纵深）。
        _valid = {r[1] for r in conn.execute("PRAGMA table_info(facts)").fetchall()}
        cols = [k for k in fr.keys() if k != "id" and k in _valid]
        if not cols:
            return "failed:empty"
        if _facts_row_present(conn, fr, _valid):
            return "already_present"
        placeholders = ",".join("?" for _ in cols)
        conn.execute(
            f"INSERT INTO facts ({','.join(cols)}) VALUES ({placeholders})",
            tuple(fr[c] for c in cols),
        )
        return "restored"
    except Exception as fe:
        logger.warning("tombstone facts 回插失败（本次不盖章，可重试）: %s", fe)
        return f"failed:{type(fe).__name__}"


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
    existing = mem.vector_store.get(vector_id=target_id)
    if existing is not None:
        # Idempotent: a previous (partial) restore or a live point.  Never
        # overwrite a point that belongs to another scope.
        if not _payload_in_scope(_point_payload(existing), scope):
            return "failed:id_owned_by_another_scope"
        return "already_present"
    vector = mem.embedding_model.embed(content, "add")
    mem.vector_store.insert(vectors=[vector], payloads=[payload], ids=[target_id])
    if mem.vector_store.get(vector_id=target_id) is None:
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
    out = {"required": False, "ok": True, "layers": {}, "point_verified": None}
    target_id = str(row["target_id"] or "").strip()
    if (row["target_type"] or "memory") != "memory" or not _is_vector_point_id(target_id) \
            or not content.strip():
        out["layers"]["vector"] = "not_applicable"
        return out
    from ducky.engine_mode import cloud_leg_enabled, local_leg_enabled
    payload = _restore_payload(row, content, scope, _load_vector_snapshot(row), _now_iso())
    mem = None
    if cloud_leg_enabled():
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
    if local_leg_enabled():
        out["layers"]["local_vector"] = _restore_local_point(target_id, content, payload)
    if out["ok"] and mem is not None:
        out["layers"].update(_restore_aux_layers(mem, target_id, content, scope))
    return out


def _verify_restore(conn, row, content: str, scope, vec: dict) -> dict:
    """Read the layers back (what the batch tool reports; not a status echo)."""
    check: dict = {"facts_row": None, "fts_row": None, "vector_point": vec.get("point_verified")}
    if row["facts_snapshot"]:
        try:
            fr = json.loads(row["facts_snapshot"])
            valid = {r[1] for r in conn.execute("PRAGMA table_info(facts)").fetchall()}
            check["facts_row"] = _facts_row_present(conn, fr, valid)
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
                _index_memory(
                    target_id,
                    content,
                    user_id=scope.user_id,
                    bank_id=scope.bank_id,
                )
                restored_cols.append("fts")
                layers["fts"] = "restored"
            except Exception as ie:
                failed.append("fts")
                layers["fts"] = f"failed:{type(ie).__name__}"
                feature_failed("index_memory", ie)
                logger.warning("tombstone FTS 重建失败（本次不盖章，可重试）: %s", ie)

        # 3. 向量层（f0.3 C7：原 id 重嵌入 + 同源写入，幂等）
        vec = _restore_vector_layers(row, content, scope)
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
