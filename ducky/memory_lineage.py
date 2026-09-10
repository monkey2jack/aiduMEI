"""
ducky.memory_lineage — 记忆密码学谱系与可检测篡改的版本链 (v20.5.0a)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
权威源数据（Facts/Ledger）与可重建派生索引（Vector/BM25/Graph）的二元解耦基础。

每条记忆/事实变更生成一条**可检测篡改**（tamper-evident）的 SHA-256 密码学版本链：
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


# 🟡-5a（v20.5.0 正式版 · 用户审计）：版本号竞态兜底约束——把「沉默的链分叉」
# 变成「显式报错」。独立成语句：存量库若已有重复 (memory_id, version) 对，
# 建索引会失败，这时要 warning 出声（并靠 verify_lineage_integrity 报出），
# 而不是 debug 吞掉。
_LINEAGE_UNIQUE_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_lineage_mem_version_unique "
    "ON memory_lineage(memory_id, version)"
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
        try:
            conn.execute(_LINEAGE_UNIQUE_INDEX)
        except Exception as exc:
            logger.warning("memory_lineage UNIQUE 约束未建立（存量重复对？请跑 verify 对账）: %s", exc)
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

    cur = None
    for _attempt in (1, 2):
        try:
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
            break
        except sqlite3.IntegrityError:
            # 🟡-5a：UNIQUE(memory_id, version) 兜底触发——并发下另一写入者
            # 抢了同一版本号。重读最新版本重试一次；仍冲突则显式上抛，
            # 绝不沉默分叉。
            if _attempt == 2:
                raise
            prev_row = conn.execute(
                "SELECT version, content_hash FROM memory_lineage WHERE memory_id=? ORDER BY version DESC LIMIT 1",
                (memory_id,),
            ).fetchone()
            if prev_row:
                next_version = prev_row[0] + 1
                if not previous_version_hash:
                    previous_version_hash = prev_row[1] or ""
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


def record_terminal_lineage(
    conn: sqlite3.Connection,
    memory_id: str,
    action: str = "DELETE",
    actor: str = "system",
    source: str = "",
    diff_summary: str = "",
) -> dict[str, Any]:
    """记录 DELETE/FORGET 终链（🟡-5b · v20.5.0 正式版）。

    「删过什么」是审计里最需要留痕的一类操作。终链不留正文：
    content_hash 沿用链尾前一版的哈希（链仍可自洽验证，但不保存已删内容
    的任何副本）；无既有链时记单节点终链——证明「该行存在过且被删了」。
    verify_lineage_integrity 视 DELETE/FORGET 链尾为合法闭链（行应已不存在）。
    """
    action = (action or "DELETE").upper()
    if action not in ("DELETE", "FORGET"):
        return {"status": "error", "detail": "终链动作仅支持 DELETE/FORGET"}
    if not memory_id:
        return {"status": "error", "detail": "memory_id 不能为空"}

    actor = actor or "system"
    next_version = 1
    prev_hash = ""
    for _attempt in (1, 2):
        tail = conn.execute(
            "SELECT version, content_hash FROM memory_lineage WHERE memory_id=? ORDER BY version DESC LIMIT 1",
            (memory_id,),
        ).fetchone()
        if tail:
            next_version = tail[0] + 1
            prev_hash = tail[1] or ""
        else:
            next_version, prev_hash = 1, ""
        try:
            cur = conn.execute(
                """INSERT INTO memory_lineage
                   (memory_id, version, content_hash, previous_version_hash, action, actor, source, diff_summary)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (memory_id, next_version, prev_hash, prev_hash, action, actor, source, diff_summary),
            )
            return {
                "status": "ok",
                "lineage_id": cur.lastrowid or 0,
                "memory_id": memory_id,
                "version": next_version,
                "action": action,
                "actor": actor,
            }
        except sqlite3.IntegrityError:
            if _attempt == 2:
                raise
    return {"status": "error", "detail": "unreachable"}


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
    """验证谱系链的哈希连续性、**事实行存在性与内容一致性**。

    v20.5.0 正式版（用户审计 🔴-1）：旧实现只校验链内 previous_version_hash
    咬合与 version 连号——于是「指向不存在事实行的幽灵链」和「链尾哈希与
    facts 行实际内容不符」两种篡改/缺陷全部报绿灯。本版补齐对账：
      · fact:<id> 链指向的 facts 行必须存在（链尾为 DELETE/FORGET 的合法
        终链除外）；
      · facts 行 content_hash 非空时，链尾版本哈希必须等于行内容哈希；
      · fact:<非数字> 形态是 v20.5.0a lastrowid 缺陷路径的产物，一律判 broken。
    """
    ensure_lineage_schema()
    conn = get_facts_conn()
    try:
        if memory_id:
            query = (
                "SELECT memory_id, version, content_hash, previous_version_hash, action FROM memory_lineage "
                "WHERE memory_id=? ORDER BY memory_id, version ASC"
            )
            params = (memory_id,)
        else:
            query = (
                "SELECT memory_id, version, content_hash, previous_version_hash, action FROM memory_lineage "
                "ORDER BY memory_id, version ASC"
            )
            params = ()

        rows = conn.execute(query, params).fetchall()

        chains_checked = 0
        broken_chains: list[dict[str, Any]] = []

        # 按 memory_id 检查链连续性，同时记录每条链的链尾（供 facts 对账）
        current_mem = None
        expected_prev_hash = ""
        expected_version = 1
        chain_tails: dict[str, dict[str, Any]] = {}

        for r in rows:
            m_id, ver, c_hash, prev_hash, action = r[0], r[1], r[2], r[3], r[4]
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
            chain_tails[m_id] = {"version": ver, "content_hash": c_hash, "action": (action or "").upper()}

        # ── facts 对账（存在性 + 链尾内容一致性）──
        if chain_tails:
            facts_rows = conn.execute("SELECT id, content_hash FROM facts").fetchall()
            facts_map = {int(fr[0]): (fr[1] or "") for fr in facts_rows}
            for m_id, tail in chain_tails.items():
                if not m_id.startswith("fact:"):
                    continue  # 非 facts 链（未来形态）不在本对账射程
                fid_str = m_id.split(":", 1)[1]
                if not fid_str.isdigit():
                    broken_chains.append({
                        "memory_id": m_id,
                        "version": tail["version"],
                        "reason": "非法 memory_id 形态：fact:<fact_key> 是 v20.5.0a lastrowid 缺陷路径的产物，正规链一律为 fact:<行id>",
                    })
                    continue
                fid = int(fid_str)
                row_hash = facts_map.get(fid)
                terminal = tail["action"] in ("DELETE", "FORGET")
                if row_hash is None:
                    if not terminal:
                        broken_chains.append({
                            "memory_id": m_id,
                            "version": tail["version"],
                            "reason": f"谱系指向不存在的事实行 fact:{fid}（幽灵链）",
                        })
                elif terminal:
                    broken_chains.append({
                        "memory_id": m_id,
                        "version": tail["version"],
                        "reason": f"链尾为 {tail['action']} 终链但 facts 行 fact:{fid} 仍存在",
                    })
                elif row_hash and tail["content_hash"] != row_hash:
                    broken_chains.append({
                        "memory_id": m_id,
                        "version": tail["version"],
                        "reason": "链尾哈希与 facts 行实际内容不一致（内容被绕过写入路径改写）",
                    })

        return {
            "status": "ok" if not broken_chains else "broken",
            "chains_checked": chains_checked,
            "total_records": len(rows),
            "broken_count": len(broken_chains),
            "broken_details": broken_chains,
        }
    finally:
        conn.close()
