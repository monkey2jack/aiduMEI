"""
ducky.federation.grants — 细粒度联邦授权与动态撤销引擎 (v20.5.0a)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
彻底告别单薄的 shared: bool 标记，建立细粒度、基于策略与凭据的联邦访问控制模型。

核心实体：
  - grant_id: 授权唯一凭据标识 (ULID / UUID4)
  - grantor_agent: 授权方 Agent (如 "local", "aiduBOT")
  - grantee_agent: 被授权方 Agent ("*" 代表全局信任域)
  - resource_scope: 资源范围 ("*", "category:xxx", "tags:yyy", "tier:zzz", "user_id:uuu")
  - actions: 允许的动作集合 (逗号分隔: "read,write,export,delete")
  - expires_at: 过期时间 (ISO 8601 或 NULL 为永久)
  - revoked_at: 撤销时间 (ISO 8601 或 NULL 为有效)
  - revoked_by: 撤销执行者 Agent ID
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from ducky.utils import get_facts_conn

logger = logging.getLogger("aiduMEM.Federation.Grants")

_GRANTS_DDL = """
CREATE TABLE IF NOT EXISTS federation_grants (
    grant_id        TEXT PRIMARY KEY,
    grantor_agent   TEXT NOT NULL,
    grantee_agent   TEXT NOT NULL,
    resource_scope  TEXT NOT NULL DEFAULT '*',
    actions         TEXT NOT NULL DEFAULT 'read',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_by      TEXT DEFAULT '',
    expires_at      TEXT,
    revoked_at      TEXT,
    revoked_by      TEXT DEFAULT ''
)
"""

_GRANTS_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_grants_lookup ON federation_grants(grantor_agent, grantee_agent, revoked_at)",
    "CREATE INDEX IF NOT EXISTS idx_grants_grantee ON federation_grants(grantee_agent)",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# 已知 scope 维度白名单（v20.5.0 正式版 · 用户审计 🟡-4）：
# 维度名拼错或未来新增维度绝不可被静默当成通配——不认识的 key 一律拒绝。
_SCOPE_KNOWN_KEYS = frozenset((
    "category", "cat", "tier", "memory_tier", "tag", "tags", "user", "user_id",
))


def ensure_grants_schema(conn: sqlite3.Connection | None = None) -> None:
    """幂等建立 federation_grants 表与索引（含存量库 created_by 补列）。"""
    should_close = False
    if conn is None:
        conn = get_facts_conn()
        should_close = True
    try:
        conn.execute(_GRANTS_DDL)
        # v20.5.0 正式版：created_by 补列迁移（审计主体自认证身份派生）
        cols = {r[1] for r in conn.execute("PRAGMA table_info(federation_grants)").fetchall()}
        if "created_by" not in cols:
            conn.execute("ALTER TABLE federation_grants ADD COLUMN created_by TEXT DEFAULT ''")
        for stmt in _GRANTS_INDEXES:
            try:
                conn.execute(stmt)
            except Exception as exc:
                logger.debug("federation_grants 索引跳过: %s", exc)
        if should_close:
            conn.commit()
    except Exception as exc:
        if should_close:
            conn.rollback()
        logger.warning("federation_grants 表初始化跳过: %s", exc)
    finally:
        if should_close:
            conn.close()


