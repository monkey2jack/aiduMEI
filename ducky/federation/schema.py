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


def ensure_federation_schema(force: bool = False) -> dict:
    """幂等迁移 facts.db 到联邦结构。返回本次实际执行的变更清单。"""
    global _migrated
    with _migrate_lock:
        if _migrated and not force:
            return {"status": "ok", "skipped": True, "added_columns": [], "created_tables": []}

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
