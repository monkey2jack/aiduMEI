"""
ducky.federation.registry — Agent 注册表
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

联邦的「户口本」：谁在线、属于哪个 profile、上次心跳何时。

激活策略（MoE 的门控信号）
    active=1 且心跳在 STALE_AFTER_HOURS 内 → 可参与联邦广播
    否则视为休眠：不参与广播，但记忆仍可被显式跨 Agent 检索到。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from ducky.federation.schema import DEFAULT_AGENT, DEFAULT_PROFILE
from ducky.utils import get_facts_conn

logger = logging.getLogger("aiduMEM.Federation.Registry")

# 超过这个时长没有心跳，视为休眠 Agent
STALE_AFTER_HOURS = 72


def _now() -> datetime:
    return datetime.now(timezone.utc)


def register_agent(
    agent_id: str,
    *,
    display_name: str = "",
    profile: str = DEFAULT_PROFILE,
    description: str = "",
    endpoint: str = "",
) -> dict[str, Any]:
    """注册或更新一个 Agent（幂等：重复注册即刷新元信息与心跳）。"""
    agent_id = (agent_id or "").strip()
    if not agent_id:
        return {"status": "error", "detail": "agent_id 不能为空"}

    conn = get_facts_conn()
    conn.execute(
        """INSERT INTO agents (agent_id, display_name, profile, description, endpoint, active, last_seen_at)
           VALUES (?,?,?,?,?,1,CURRENT_TIMESTAMP)
           ON CONFLICT(agent_id) DO UPDATE SET
               display_name = COALESCE(NULLIF(excluded.display_name,''), agents.display_name),
               profile      = COALESCE(NULLIF(excluded.profile,''),      agents.profile),
               description  = COALESCE(NULLIF(excluded.description,''),  agents.description),
               endpoint     = COALESCE(NULLIF(excluded.endpoint,''),     agents.endpoint),
               active       = 1,
               last_seen_at = CURRENT_TIMESTAMP""",
        (agent_id, display_name, profile or DEFAULT_PROFILE, description, endpoint),
    )
    conn.commit()
    return {"status": "ok", "agent_id": agent_id, "profile": profile or DEFAULT_PROFILE}


def heartbeat(agent_id: str) -> dict[str, Any]:
    """刷新心跳。未注册的 agent_id 自动补注册（宽容优于报错）。"""
    conn = get_facts_conn()
    cur = conn.execute(
        "UPDATE agents SET last_seen_at=CURRENT_TIMESTAMP, active=1 WHERE agent_id=?", (agent_id,)
    )
    conn.commit()
    if cur.rowcount == 0:
        return register_agent(agent_id)
    return {"status": "ok", "agent_id": agent_id, "heartbeat": _now().isoformat()}


def deactivate_agent(agent_id: str) -> dict[str, Any]:
    """把 Agent 标记为休眠（不删记录、不删记忆）。"""
    conn = get_facts_conn()
    conn.execute("UPDATE agents SET active=0 WHERE agent_id=?", (agent_id,))
    conn.commit()
    return {"status": "ok", "agent_id": agent_id, "active": False}


def list_agents(profile: str | None = None, include_inactive: bool = True) -> list[dict[str, Any]]:
    """列出 Agent，附带各自的事实条数与是否休眠。"""
    conn = get_facts_conn()
    where, params = [], []
    if profile:
        where.append("a.profile = ?")
        params.append(profile)
    if not include_inactive:
        where.append("a.active = 1")

    sql = """
        SELECT a.*, (
            SELECT COUNT(*) FROM facts f
            WHERE COALESCE(f.agent_id, ?) = a.agent_id AND f.archived = 0
        ) AS fact_count
        FROM agents a
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY a.active DESC, a.last_seen_at DESC"

    rows = conn.execute(sql, [DEFAULT_AGENT, *params]).fetchall()
    stale_before = _now() - timedelta(hours=STALE_AFTER_HOURS)

    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["stale"] = _is_stale(item.get("last_seen_at"), stale_before)
        item["available"] = bool(item.get("active")) and not item["stale"]
        out.append(item)
    return out


def _is_stale(last_seen: str | None, stale_before: datetime) -> bool:
    if not last_seen:
        return True
    try:
        seen = datetime.fromisoformat(str(last_seen).replace("Z", "+00:00"))
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        return seen < stale_before
    except ValueError:
        return True


def active_agent_ids(exclude: str | None = None) -> list[str]:
    """可参与联邦广播的 Agent 列表（在线 + 心跳新鲜）。"""
    return [
        a["agent_id"]
        for a in list_agents(include_inactive=False)
        if a["available"] and a["agent_id"] != exclude
    ]


def resolve_profile(agent_id: str) -> str:
    """查 Agent 归属的 profile，查不到回落 default。"""
    conn = get_facts_conn()
    row = conn.execute("SELECT profile FROM agents WHERE agent_id=?", (agent_id,)).fetchone()
    return (row["profile"] if row and row["profile"] else DEFAULT_PROFILE)
