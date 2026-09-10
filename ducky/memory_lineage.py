"""
ducky.memory_lineage — 记忆密码学谱系与不可篡改历史链 (v20.5.0a)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
权威源数据（Facts/Ledger）与可重建派生索引（Vector/BM25/Graph）的二元解耦基础。

每条记忆/事实变更生成一条不可篡改的 SHA-256 密码学版本链：
  - memory_id: 记忆标识（如 "fact:123" 或 "fact:user_profile"）
  - version: 递增版本号 (1, 2, 3...)
  - content_hash: 内容 SHA-256 哈希值 (64位 hex)
  - previous_version_hash: 前序版本哈希（第一版为空串 ""）
  - action: CREATE / UPDATE / MERGE / CONFLICT_RESOLVE / FORGET / DELETE
  - actor: 操作主体
  - source: 事实来源
  - diff_summary: 变更摘要
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
from typing import Any

from ducky.utils import get_facts_conn

logger = logging.getLogger("aiduMEM.MemoryLineage")

_LINEAGE_DDL = """
CREATE TABLE IF NOT EXISTS memory_lineage (
    lineage_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id              TEXT NOT NULL,
    version                INTEGER NOT NULL DEFAULT 1,
    content_hash           TEXT NOT NULL,
    previous_version_hash  TEXT DEFAULT '',
    action                 TEXT NOT NULL,
    actor                  TEXT NOT NULL DEFAULT 'system',
    source                 TEXT DEFAULT '',
    diff_summary           TEXT DEFAULT '',
    created_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_LINEAGE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_lineage_mem ON memory_lineage(memory_id, version)",
    "CREATE INDEX IF NOT EXISTS idx_lineage_hash ON memory_lineage(content_hash)",
)


def compute_content_hash(text: Any) -> str:
    """计算内容完整 SHA-256 散列值（64位 hex 小写字符串）。空内容返回 64 个 0。"""
    s = str(text or "").strip()
    if not s:
        return "0" * 64
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()


def ensure_lineage_schema(conn: sqlite3.Connection | None = None) -> None:
    """幂等建立 memory_lineage 表与索引。"""
    should_close = False
    if conn is None:
        conn = get_facts_conn()
        should_close = True
    try:
        conn.execute(_LINEAGE_DDL)
        for stmt in _LINEAGE_INDEXES:
            try:
                conn.execute(stmt)
            except Exception as exc:
                logger.debug("memory_lineage 索引跳过: %s", exc)
        if should_close:
            conn.commit()
    except Exception as exc:
        if should_close:
            conn.rollback()
        logger.warning("memory_lineage 表初始化跳过: %s", exc)
    finally:
        if should_close:
            conn.close()


def record_lineage(
    conn: sqlite3.Connection,
    memory_id: str,
    content: str,
    action: str,
    actor: str,
    previous_version_hash: str = "",
    source: str = "",
    diff_summary: str = "",
) -> dict[str, Any]:
    """在当前数据库连接/事务中记录一条谱系变更。不主动 commit，由外层调用者统一 commit。"""
    if not memory_id:
        return {"status": "error", "detail": "memory_id 不能为空"}

    c_hash = compute_content_hash(content)
    action = (action or "UPDATE").upper()
    actor = actor or "system"

    # 查询当前该 memory_id 的最新版本与最新 hash
    prev_row = conn.execute(
        "SELECT version, content_hash FROM memory_lineage WHERE memory_id=? ORDER BY version DESC LIMIT 1",
        (memory_id,),
    ).fetchone()

    if prev_row:
        next_version = prev_row[0] + 1
        if not previous_version_hash:
            previous_version_hash = prev_row[1] or ""
    else:
        next_version = 1
        if not previous_version_hash:
            previous_version_hash = ""

    cur = conn.execute(
        """INSERT INTO memory_lineage
           (memory_id, version, content_hash, previous_version_hash, action, actor, source, diff_summary)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            memory_id,
            next_version,
            c_hash,
            previous_version_hash,
            action,
            actor,
            source,
            diff_summary,
        ),
    )
    lineage_id = cur.lastrowid or 0

    return {
        "status": "ok",
        "lineage_id": lineage_id,
        "memory_id": memory_id,
        "version": next_version,
        "content_hash": c_hash,
        "previous_version_hash": previous_version_hash,
        "action": action,
        "actor": actor,
    }


def get_memory_lineage(memory_id: str) -> list[dict[str, Any]]:
    """获取指定 memory_id 的完整历史演化谱系链。"""
    if not memory_id:
        return []
    ensure_lineage_schema()
    conn = get_facts_conn()
    try:
        rows = conn.execute(
            """SELECT lineage_id, memory_id, version, content_hash, previous_version_hash,
                      action, actor, source, diff_summary, created_at
               FROM memory_lineage
               WHERE memory_id=?
               ORDER BY version ASC""",
            (memory_id,),
        ).fetchall()

        chain = []
        for r in rows:
            chain.append({
                "lineage_id": r[0],
                "memory_id": r[1],
                "version": r[2],
                "content_hash": r[3],
                "previous_version_hash": r[4],
                "action": r[5],
                "actor": r[6],
                "source": r[7],
                "diff_summary": r[8],
                "created_at": str(r[9]),
            })
        return chain
    finally:
        conn.close()


def verify_lineage_integrity(memory_id: str | None = None) -> dict[str, Any]:
    """验证谱系链的哈希连续性与完整性。"""
    ensure_lineage_schema()
    conn = get_facts_conn()
    try:
        if memory_id:
            query = (
                "SELECT memory_id, version, content_hash, previous_version_hash FROM memory_lineage "
                "WHERE memory_id=? ORDER BY memory_id, version ASC"
            )
            params = (memory_id,)
        else:
            query = (
                "SELECT memory_id, version, content_hash, previous_version_hash FROM memory_lineage "
                "ORDER BY memory_id, version ASC"
            )
            params = ()

        rows = conn.execute(query, params).fetchall()

        chains_checked = 0
        broken_chains: list[dict[str, Any]] = []

        # 按 memory_id 检查链连续性
        current_mem = None
        expected_prev_hash = ""
        expected_version = 1

        for r in rows:
            m_id, ver, c_hash, prev_hash = r[0], r[1], r[2], r[3]
            if m_id != current_mem:
                current_mem = m_id
                expected_version = 1
                expected_prev_hash = ""
                chains_checked += 1

            # 校验版本是否递增
            if ver != expected_version:
                broken_chains.append({
                    "memory_id": m_id,
                    "version": ver,
                    "reason": f"版本不连续: 期望 v{expected_version} 但遇到 v{ver}",
                })

            # 校验 parent hash 是否匹配
            if prev_hash != expected_prev_hash:
                broken_chains.append({
                    "memory_id": m_id,
                    "version": ver,
                    "reason": f"父哈希断链: 期望 '{expected_prev_hash}' 但记录为 '{prev_hash}'",
                })

            expected_version = ver + 1
            expected_prev_hash = c_hash

        return {
            "status": "ok" if not broken_chains else "broken",
            "chains_checked": chains_checked,
            "total_records": len(rows),
            "broken_count": len(broken_chains),
            "broken_details": broken_chains,
        }
    finally:
        conn.close()
