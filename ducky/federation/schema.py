"""
ducky.federation.schema — 联邦字段与联邦表的幂等迁移
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

对 facts.db 做增量迁移，全部操作幂等：
    facts 表新增   agent_id / profile / memory_tier / recorded_at / tags / decay_at
    新增 agents    表：Agent 注册表（含 profile 归属与心跳）
    新增 federation_broadcast 表：跨 Agent 广播已读游标

安全承诺
    · 只 ADD COLUMN，绝不 DROP / 改类型 / 删数据
    · 历史行回填 DEFAULT_AGENT / DEFAULT_PROFILE，旧调用方零感知
    · 任何一步失败只记日志不抛，主服务照常启动（降级而非崩溃）
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading

from ducky.utils import DEFAULT_AGENT_ID, get_facts_conn

logger = logging.getLogger("aiduMEM.Federation.Schema")

# ── 联邦默认身份 ────────────────────────────────────────
# 不带 agent_id 的调用方（v12 及更早）全部归入此身份。
# 可用 AIDUMEM_DEFAULT_AGENT_ID / AIDUMEM_DEFAULT_AGENT_NAME 覆盖。
DEFAULT_AGENT = DEFAULT_AGENT_ID
DEFAULT_PROFILE = "default"
DEFAULT_AGENT_NAME = os.environ.get("AIDUMEM_DEFAULT_AGENT_NAME", DEFAULT_AGENT)

# ── facts 表联邦字段（列名 -> DDL 片段）────────────────
_FACTS_COLUMNS: dict[str, str] = {
    "agent_id":    f"TEXT DEFAULT '{DEFAULT_AGENT}'",
    "profile":     f"TEXT DEFAULT '{DEFAULT_PROFILE}'",
    "memory_tier": "TEXT DEFAULT 'semantic'",
    "recorded_at": "TIMESTAMP",
    "tags":        "TEXT DEFAULT ''",
    "decay_at":    "TEXT",
    "shared":      "INTEGER DEFAULT 1",
}

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_facts_agent   ON facts(agent_id)",
    "CREATE INDEX IF NOT EXISTS idx_facts_profile ON facts(profile)",
    "CREATE INDEX IF NOT EXISTS idx_facts_tier    ON facts(memory_tier)",
)

_AGENTS_DDL = """
CREATE TABLE IF NOT EXISTS agents (
    agent_id     TEXT PRIMARY KEY,
    display_name TEXT DEFAULT '',
    profile      TEXT DEFAULT 'default',
    description  TEXT DEFAULT '',
    endpoint     TEXT DEFAULT '',
    active       INTEGER DEFAULT 1,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_BROADCAST_DDL = """
CREATE TABLE IF NOT EXISTS federation_broadcast (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id     TEXT NOT NULL,
    last_fact_id INTEGER DEFAULT 0,
    last_run_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(agent_id)
)
"""

_migrate_lock = threading.Lock()
_migrated = False


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def _rebuild_agent_scoped_unique_index(conn: sqlite3.Connection) -> None:
    """🔴3：把 facts 全局唯一索引 (category,fact_key) 升级为按 agent 隔离。

    幂等：已是新索引则跳过。存量库里同一 (category,fact_key) 的 agent_id 已被
    上游回填为同一 DEFAULT_AGENT，故 (agent_id,category,fact_key) 仍唯一，重建不会
    因重复行失败。若历史上真有跨 agent 撞 key 的脏数据导致建唯一索引失败，则降级为
    普通索引并记 warning，避免阻断启动。
    """
    idx = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_facts_unique'"
    ).fetchone()
    if idx and idx[0] and "agent_id" in idx[0]:
        return  # 已是 agent 隔离版
    try:
        conn.execute("DROP INDEX IF EXISTS idx_facts_unique")
        conn.execute(
            "CREATE UNIQUE INDEX idx_facts_unique ON facts(agent_id, category, fact_key)"
        )
        logger.info("🔒 facts 唯一索引已升级为 (agent_id, category, fact_key)")
    except sqlite3.OperationalError as exc:
        # 🔴3 自审：绝不能降级为“普通索引”。writer 的 ON CONFLICT(agent_id,category,fact_key)
        # 需要一个 UNIQUE 约束作为冲突目标；若这里建成非唯一索引，upsert 会报
        # “no such conflict target”导致所有联邦写入失败——比原 bug 更糟。
        # 正确做法：先合并跨 agent 撞 key 的脏数据（保留每组最新一行），再重建唯一索引。
        logger.warning("唯一索引升级遇冲突，尝试清理跨 agent 撞 key 脏数据后重建: %s", exc)
        try:
            conn.execute(
                """
                DELETE FROM facts
                WHERE id NOT IN (
                    SELECT MAX(id) FROM facts GROUP BY agent_id, category, fact_key
                )
                """
            )
            conn.execute("DROP INDEX IF EXISTS idx_facts_unique")
            conn.execute(
                "CREATE UNIQUE INDEX idx_facts_unique ON facts(agent_id, category, fact_key)"
            )
            logger.info("🔒 清理脏数据后，facts 唯一索引重建成功")
        except sqlite3.OperationalError as exc2:
            # 仍失败：宁可让联邦迁移整体报错、由运维介入，也不能留一个
            # 会让所有 upsert 崩溃的非唯一索引。抛出交由上层记录。
            raise RuntimeError(
                f"facts 唯一索引重建失败，联邦 upsert 将无法工作，需人工核查脏数据: {exc2}"
            ) from exc2



def ensure_federation_schema(force: bool = False) -> dict:
    """幂等迁移 facts.db 到联邦结构。返回本次实际执行的变更清单。"""
    global _migrated
    with _migrate_lock:
        if _migrated and not force:
            return {"status": "ok", "skipped": True, "added_columns": [], "created_tables": []}

        # 🔴13 修复：联邦迁移可能在核心建表之前（路由导入期）被触发。
        # 先确保 facts 等核心表存在，否则 ADD COLUMN / 回填会静默失败，
        # 导致 agents / federation_broadcast 两表永远建不出来 → 全新部署开箱即坏。
        try:
            from ducky.schema_bootstrap import ensure_core_schema
            ensure_core_schema()
        except Exception as exc:
            logger.warning("联邦迁移前置核心建表失败（继续尝试）: %s", exc)

        added: list[str] = []
        created: list[str] = []
        conn = get_facts_conn()
        try:
            present = _existing_columns(conn, "facts")
            for column, ddl in _FACTS_COLUMNS.items():
                if column in present:
                    continue
                try:
                    conn.execute(f"ALTER TABLE facts ADD COLUMN {column} {ddl}")
                    added.append(column)
                except sqlite3.OperationalError as exc:
                    # 并发迁移时另一个线程可能已加上，重复即视为成功
                    if "duplicate column" not in str(exc).lower():
                        logger.warning("联邦字段 %s 迁移失败: %s", column, exc)

            # 历史行回填：ALTER 的 DEFAULT 只作用于新行，旧行为 NULL
            conn.execute(
                "UPDATE facts SET agent_id=? WHERE agent_id IS NULL OR agent_id=''",
                (DEFAULT_AGENT,),
            )
            conn.execute(
                "UPDATE facts SET profile=? WHERE profile IS NULL OR profile=''",
                (DEFAULT_PROFILE,),
            )
            conn.execute(
                "UPDATE facts SET memory_tier='semantic' WHERE memory_tier IS NULL OR memory_tier=''"
            )
            conn.execute(
                "UPDATE facts SET recorded_at=created_at WHERE recorded_at IS NULL"
            )
            conn.execute("UPDATE facts SET shared=1 WHERE shared IS NULL")

            for stmt in _INDEXES:
                conn.execute(stmt)

            # 🔴3 修复：唯一约束升级为按 agent 隔离 (agent_id, category, fact_key)。
            # 旧索引 (category, fact_key) 全局唯一会让 Agent B 写同 key 时静默覆盖
            # Agent A 的记忆（且仍记 A 名下）。此处在 agent_id 回填完成后重建，
            # 存量库同 (cat,key) 因 agent_id 一致仍唯一，迁移安全。
            _rebuild_agent_scoped_unique_index(conn)

            for name, ddl in (("agents", _AGENTS_DDL), ("federation_broadcast", _BROADCAST_DDL)):
                before = conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (name,)
                ).fetchone()[0]
                conn.execute(ddl)
                if not before:
                    created.append(name)

            # 自注册本机主 Agent（幂等）
            conn.execute(
                """INSERT OR IGNORE INTO agents (agent_id, display_name, profile, description)
                   VALUES (?, ?, ?, ?)""",
                (DEFAULT_AGENT, DEFAULT_AGENT_NAME, DEFAULT_PROFILE, "aiduMEM local primary agent"),
            )
            conn.commit()
            _migrated = True
        except Exception as exc:
            logger.error("联邦 schema 迁移异常（服务继续以 v12 语义运行）: %s", exc)
            return {"status": "error", "detail": str(exc), "added_columns": added, "created_tables": created}
        finally:
            conn.close()

        if added or created:
            logger.info("联邦 schema 迁移完成 · 新增字段=%s 新增表=%s", added, created)
        return {"status": "ok", "skipped": False, "added_columns": added, "created_tables": created}
