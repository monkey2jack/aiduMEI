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
import uuid
from datetime import datetime, timezone
from typing import Any

from ducky.utils import get_facts_conn, parse_iso_timestamp

logger = logging.getLogger("aiduMEM.Federation.Grants")

_GRANTS_DDL = """
CREATE TABLE IF NOT EXISTS federation_grants (
    grant_id        TEXT PRIMARY KEY,
    grantor_agent   TEXT NOT NULL,
    grantee_agent   TEXT NOT NULL,
    resource_scope  TEXT NOT NULL DEFAULT '*',
    actions         TEXT NOT NULL DEFAULT 'read',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
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


def ensure_grants_schema(conn: sqlite3.Connection | None = None) -> None:
    """幂等建立 federation_grants 表与索引。"""
    should_close = False
    if conn is None:
        conn = get_facts_conn()
        should_close = True
    try:
        conn.execute(_GRANTS_DDL)
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


def create_grant(
    grantor_agent: str,
    grantee_agent: str,
    *,
    resource_scope: str = "*",
    actions: str | list[str] = "read",
    expires_at: str | None = None,
    grant_id: str | None = None,
) -> dict[str, Any]:
    """创建或更新一条联邦授权 Grant。"""
    if not grantor_agent or not grantee_agent:
        return {"status": "error", "detail": "grantor_agent 与 grantee_agent 不能为空"}

    gid = grant_id or f"grant_{uuid.uuid4().hex[:16]}"
    if isinstance(actions, (list, set, tuple)):
        act_str = ",".join(sorted(str(a).strip().lower() for a in actions if a))
    else:
        act_str = ",".join(sorted(str(a).strip().lower() for a in str(actions).split(",") if a.strip()))
    if not act_str:
        act_str = "read"

    resource_scope = (resource_scope or "*").strip()

    ensure_grants_schema()
    conn = get_facts_conn()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO federation_grants
               (grant_id, grantor_agent, grantee_agent, resource_scope, actions, expires_at, revoked_at, revoked_by)
               VALUES (?, ?, ?, ?, ?, ?, NULL, '')""",
            (gid, grantor_agent, grantee_agent, resource_scope, act_str, expires_at or None),
        )
        conn.commit()
        return {
            "status": "ok",
            "grant_id": gid,
            "grantor_agent": grantor_agent,
            "grantee_agent": grantee_agent,
            "resource_scope": resource_scope,
            "actions": act_str.split(","),
            "expires_at": expires_at,
        }
    except Exception as exc:
        conn.rollback()
        logger.error("创建联邦授权失败: %s", exc)
        return {"status": "error", "detail": str(exc)}
    finally:
        conn.close()


def revoke_grant(grant_id: str, revoked_by: str = "system") -> dict[str, Any]:
    """即时撤销一条联邦授权 Grant。"""
    if not grant_id:
        return {"status": "error", "detail": "grant_id 不能为空"}

    ensure_grants_schema()
    conn = get_facts_conn()
    try:
        now = _now_iso()
        cur = conn.execute(
            """UPDATE federation_grants
               SET revoked_at=?, revoked_by=?
               WHERE grant_id=? AND (revoked_at IS NULL OR revoked_at='')""",
            (now, revoked_by, grant_id),
        )
        conn.commit()
        if cur.rowcount and cur.rowcount > 0:
            return {"status": "ok", "grant_id": grant_id, "revoked_at": now, "revoked_by": revoked_by}
        # 检查是否存在或已被撤销
        row = conn.execute("SELECT revoked_at FROM federation_grants WHERE grant_id=?", (grant_id,)).fetchone()
        if not row:
            return {"status": "error", "detail": f"未找到 grant_id={grant_id}"}
        return {"status": "ok", "message": "该授权已被撤销", "grant_id": grant_id, "revoked_at": row[0]}
    except Exception as exc:
        conn.rollback()
        logger.error("撤销联邦授权失败: %s", exc)
        return {"status": "error", "detail": str(exc)}
    finally:
        conn.close()


def list_grants(
    grantor_agent: str | None = None,
    grantee_agent: str | None = None,
    include_revoked: bool = False,
) -> list[dict[str, Any]]:
    """查询联邦授权列表。"""
    ensure_grants_schema()
    conn = get_facts_conn()
    try:
        where = []
        params: list[Any] = []
        if grantor_agent:
            where.append("grantor_agent=?")
            params.append(grantor_agent)
        if grantee_agent:
            where.append("grantee_agent=?")
            params.append(grantee_agent)
        if not include_revoked:
            where.append("(revoked_at IS NULL OR revoked_at='')")

        sql = "SELECT grant_id, grantor_agent, grantee_agent, resource_scope, actions, created_at, expires_at, revoked_at, revoked_by FROM federation_grants"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC"

        rows = conn.execute(sql, params).fetchall()
        results = []
        for r in rows:
            results.append({
                "grant_id": r[0],
                "grantor_agent": r[1],
                "grantee_agent": r[2],
                "resource_scope": r[3],
                "actions": (r[4] or "").split(","),
                "created_at": str(r[5]),
                "expires_at": r[6],
                "revoked_at": r[7],
                "revoked_by": r[8],
            })
        return results
    finally:
        conn.close()


def check_grant_permission(
    grantor_agent: str,
    grantee_agent: str,
    action: str = "read",
    *,
    category: str = "",
    tags: str = "",
    tier: str = "",
    user_id: str = "",
) -> bool:
    """检查 grantee_agent 是否获得来自 grantor_agent 的 action 授权。

    如果 grantee_agent == grantor_agent（本 Agent 访问自己的记忆），始终允许。
    如果存在有效、未过期、动作匹配且 scope 匹配的 Grant，允许；否则拒绝。
    """
    if grantee_agent == grantor_agent:
        return True

    ensure_grants_schema()
    conn = get_facts_conn()
    try:
        # 查找匹配 grantor 且 grantee 为具体 agent 或 '*' 的有效授权
        rows = conn.execute(
            """SELECT grant_id, resource_scope, actions, expires_at
               FROM federation_grants
               WHERE grantor_agent=? AND (grantee_agent=? OR grantee_agent='*')
                 AND (revoked_at IS NULL OR revoked_at='')""",
            (grantor_agent, grantee_agent),
        ).fetchall()

        if not rows:
            return False

        now_ts = datetime.now(timezone.utc).timestamp()
        req_action = (action or "read").strip().lower()

        for gid, scope, acts, exp in rows:
            # 1. 检查是否过期
            if exp:
                try:
                    exp_ts = parse_iso_timestamp(str(exp))
                    if exp_ts and now_ts > exp_ts:
                        continue
                except Exception:
                    pass

            # 2. 检查 action 是否包含
            allowed_actions = {a.strip().lower() for a in (acts or "").split(",") if a.strip()}
            if req_action not in allowed_actions and "*" not in allowed_actions:
                continue

            # 3. 检查 resource_scope 是否匹配
            if _match_scope(scope, category=category, tags=tags, tier=tier, user_id=user_id):
                return True

        return False
    finally:
        conn.close()


def _match_scope(
    scope: str,
    *,
    category: str = "",
    tags: str = "",
    tier: str = "",
    user_id: str = "",
) -> bool:
    """判断给定资源特征是否命中 resource_scope 规则。"""
    scope = (scope or "*").strip()
    if scope == "*":
        return True

    # 支持形如 "category:finance", "tier:semantic", "tag:personal", "user:user_123"
    rules = [r.strip() for r in scope.split(";") if r.strip()]
    for rule in rules:
        if ":" in rule:
            k, v = rule.split(":", 1)
            k = k.strip().lower()
            v = v.strip()
            if k in ("category", "cat") and category and category.lower() != v.lower():
                return False
            if k in ("tier", "memory_tier") and tier and tier.lower() != v.lower():
                return False
            if k in ("tag", "tags") and tags:
                tag_list = [t.strip().lower() for t in tags.split(",") if t.strip()]
                if v.lower() not in tag_list:
                    return False
            if k in ("user", "user_id") and user_id and user_id.lower() != v.lower():
                return False
        else:
            if category and rule.lower() != category.lower():
                return False

    return True
