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

import json
import logging
from datetime import datetime, timezone

from ducky.utils import DEFAULT_USER_ID, get_facts_conn, get_text_conn

logger = logging.getLogger("aiduMEM.tombstone")

_TOMBSTONES_DDL = """
CREATE TABLE IF NOT EXISTS tombstones (
    tombstone_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id        TEXT NOT NULL,
    target_type      TEXT DEFAULT 'memory',
    user_id          TEXT NOT NULL,
    content_snapshot TEXT,
    facts_snapshot   TEXT,
    reason           TEXT DEFAULT '',
    actor            TEXT DEFAULT 'system',
    tombstoned_at    TEXT,
    restored_at      TEXT
)
"""

_TOMBSTONE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_tombstone_user ON tombstones(user_id)",
    "CREATE INDEX IF NOT EXISTS idx_tombstone_target ON tombstones(target_id)",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_tombstone_schema() -> None:
    """幂等建表。对既有库是 no-op，任何异常只记日志不抛。"""
    try:
        conn = get_facts_conn()
        conn.execute(_TOMBSTONES_DDL)
        for stmt in _TOMBSTONE_INDEXES:
            try:
                conn.execute(stmt)
            except Exception as exc:
                logger.debug("tombstone 索引跳过: %s", exc)
        conn.commit()
    except Exception as exc:
        logger.warning("tombstones 建表跳过（服务继续）: %s", exc)


def _capture_facts_row(memory_id: str, user_id: str) -> dict | None:
    """从 facts.db 抓该记忆的结构化行（全列），返回 dict；无则 None。"""
    try:
        conn = get_facts_conn()
        exact_keys = (memory_id, f"fact:{memory_id}", f"raw:{memory_id}")
        if user_id == "default":
            row = conn.execute(
                """SELECT * FROM facts
                   WHERE id=? OR fact_key=? OR fact_key=? OR fact_key=?
                   LIMIT 1""",
                (memory_id, exact_keys[0], exact_keys[1], exact_keys[2]),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT * FROM facts
                   WHERE (id=? OR fact_key=? OR fact_key=? OR fact_key=?)
                     AND (source=? OR agent_id=?)
                   LIMIT 1""",
                (memory_id, exact_keys[0], exact_keys[1], exact_keys[2], user_id, user_id),
            ).fetchone()
        return dict(row) if row else None
    except Exception as exc:
        logger.debug("tombstone facts 快照跳过: %s", exc)
        return None


def _capture_fts_content(memory_id: str, user_id: str) -> str:
    """从 text_fts.db 抓该记忆的原文内容；无则空串。"""
    try:
        tconn = get_text_conn()
        if user_id == "default":
            row = tconn.execute(
                "SELECT content FROM memories WHERE id=? OR id=? LIMIT 1",
                (memory_id, f"fact:{memory_id}"),
            ).fetchone()
        else:
            row = tconn.execute(
                "SELECT content FROM memories WHERE (id=? OR id=?) AND (user_id=? OR user_id='default') LIMIT 1",
                (memory_id, f"fact:{memory_id}", user_id),
            ).fetchone()
        return row["content"] if row else ""
    except Exception as exc:
        logger.debug("tombstone FTS 快照跳过: %s", exc)
        return ""


def snapshot_before_delete(
    memory_id: str,
    user_id: str = DEFAULT_USER_ID,
    reason: str = "",
    actor: str = "system",
) -> int | None:
    """删除前把一条记忆的全文 + 结构化行 + 理由快照进 tombstones 表。

    返回 tombstone_id；快照失败返回 None（绝不抛异常阻断删除主链路）。
    由 cascade_delete_memory 在物理删除前调用。
    """
    if not memory_id or not str(memory_id).strip():
        return None
    try:
        ensure_tombstone_schema()
        facts_row = _capture_facts_row(memory_id, user_id)
        fts_content = _capture_fts_content(memory_id, user_id)

        # 至少抓到一样东西才值得留快照；两者皆空说明这条记忆本就不在结构化仓里
        if not facts_row and not fts_content:
            logger.debug("tombstone 快照跳过（无结构化内容）: %s", memory_id)
            return None

        content_snapshot = fts_content or (facts_row or {}).get("fact_value", "")
        conn = get_facts_conn()
        cur = conn.execute(
            """INSERT INTO tombstones
               (target_id, target_type, user_id, content_snapshot, facts_snapshot,
                reason, actor, tombstoned_at)
               VALUES (?, 'memory', ?, ?, ?, ?, ?, ?)""",
            (
                memory_id,
                user_id,
                content_snapshot,
                json.dumps(facts_row, ensure_ascii=False, default=str) if facts_row else "",
                reason or "",
                actor or "system",
                _now_iso(),
            ),
        )
        # 📒 事件账本（B5）：与快照同事务留痕
        try:
            from ducky.event_ledger import content_hash, record_event
            record_event(conn, actor=actor or "system", action="tombstone",
                         target_id=memory_id, reason=reason or "",
                         after_hash=content_hash(content_snapshot))
        except Exception as le:
            logger.debug("ledger 记录跳过: %s", le)
        conn.commit()
        tid = cur.lastrowid
        logger.info("🪦 tombstone 快照 #%s (target=%s user=%s reason=%r)", tid, memory_id, user_id, reason)
        return tid
    except Exception as exc:
        logger.warning("tombstone 快照降级（删除继续）: %s", exc)
        return None


def restore_tombstone(tombstone_id: int, user_id: str = DEFAULT_USER_ID) -> dict:
    """从 tombstones 快照恢复一条记忆：回插 facts + 重建 FTS 索引。

    返回 {restored, target_id, detail}。失败/无权限返回 restored=False。
    恢复只认未 restored_at 的快照，且严格按 user_id 归属校验。
    """
    result = {"restored": False, "target_id": "", "detail": ""}
    if not tombstone_id:
        result["detail"] = "tombstone_id 为空"
        return result
    try:
        ensure_tombstone_schema()
        conn = get_facts_conn()
        if user_id == "default":
            row = conn.execute(
                "SELECT * FROM tombstones WHERE tombstone_id=? AND restored_at IS NULL",
                (tombstone_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM tombstones WHERE tombstone_id=? AND user_id=? AND restored_at IS NULL",
                (tombstone_id, user_id),
            ).fetchone()
        if not row:
            result["detail"] = "快照不存在、已恢复或无权限"
            return result

        target_id = row["target_id"]
        facts_snapshot = row["facts_snapshot"] or ""
        content = row["content_snapshot"] or ""
        restored_cols = []

        # 1. 回插 facts（若有结构化快照）
        if facts_snapshot:
            try:
                fr = json.loads(facts_snapshot)
                # 剔除自增主键与快照元字段，让 facts 表重新分配 id
                fr.pop("id", None)
                cols = [k for k in fr.keys() if k not in ("id",)]
                if cols:
                    placeholders = ",".join("?" for _ in cols)
                    conn.execute(
                        f"INSERT INTO facts ({','.join(cols)}) VALUES ({placeholders})",
                        tuple(fr[c] for c in cols),
                    )
                    restored_cols.append("facts")
            except Exception as fe:
                logger.debug("tombstone facts 回插跳过: %s", fe)

        # 2. 重建 FTS 索引（让混合召回能再搜到）
        if content:
            try:
                from ducky.text_fts import _index_memory
                _index_memory(target_id, content, user_id=user_id)
                restored_cols.append("fts")
            except Exception as ie:
                logger.debug("tombstone FTS 重建跳过: %s", ie)

        # 3. 标记已恢复
        conn.execute(
            "UPDATE tombstones SET restored_at=? WHERE tombstone_id=?",
            (_now_iso(), tombstone_id),
        )
        # 📒 事件账本（B5）：与恢复同事务留痕
        try:
            from ducky.event_ledger import content_hash, record_event
            record_event(conn, actor=user_id or "system", action="restore",
                         target_id=target_id, reason=f"tombstone#{tombstone_id}",
                         after_hash=content_hash(content))
        except Exception as le:
            logger.debug("ledger 记录跳过: %s", le)
        conn.commit()

        result["restored"] = bool(restored_cols)
        result["target_id"] = target_id
        result["detail"] = ",".join(restored_cols) if restored_cols else "无可恢复内容"
        logger.info("🪦→♻️ tombstone #%s 恢复 (target=%s via %s)", tombstone_id, target_id, result["detail"])
        return result
    except Exception as exc:
        logger.warning("tombstone 恢复失败: %s", exc)
        result["detail"] = str(exc)[:120]
        return result


def list_tombstones(user_id: str = DEFAULT_USER_ID, limit: int = 50) -> list:
    """列某租户的遗忘记录（运维/验收用）。失败返回 []。"""
    if not user_id:
        return []
    try:
        ensure_tombstone_schema()
        conn = get_facts_conn()
        if user_id == "default":
            rows = conn.execute(
                "SELECT tombstone_id, target_id, content_snapshot, reason, actor, tombstoned_at, restored_at "
                "FROM tombstones ORDER BY tombstone_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT tombstone_id, target_id, content_snapshot, reason, actor, tombstoned_at, restored_at "
                "FROM tombstones WHERE user_id=? ORDER BY tombstone_id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.debug("list_tombstones 降级返回空: %s", exc)
        return []
