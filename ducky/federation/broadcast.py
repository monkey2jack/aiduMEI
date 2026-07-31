"""
ducky.federation.broadcast — 跨 Agent 事实广播
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

联邦感知的「体温」：每个 Agent 定期问一句
「我上次读到 fact #N，之后别人又想明白了什么？」

    · 只广播 shared=1 的事实（私密记忆不出户）
    · 游标制（federation_broadcast.last_fact_id）保证不重不漏
    · 只读聚合，不写入对端库——广播产出摘要，不产出副本

这是 MoE 里「偶尔请出其他专家」的那条路：默认不跑，
由 cron（默认 2 小时）或显式端点触发。
"""
from __future__ import annotations

import logging
from typing import Any

from ducky.federation import sqlbits
from ducky.federation import tier as tier_mod
from ducky.federation.registry import active_agent_ids, resolve_profile
from ducky.federation.schema import DEFAULT_AGENT
from ducky.utils import get_facts_conn

logger = logging.getLogger("aiduMEM.Federation.Broadcast")

# 单次广播最多带回的新事实条数（防止首次运行时刷屏）
BROADCAST_LIMIT = 50


def _get_cursor(conn, agent_id: str) -> int:
    row = conn.execute(
        "SELECT last_fact_id FROM federation_broadcast WHERE agent_id=?", (agent_id,)
    ).fetchone()
    return int(row["last_fact_id"]) if row else 0


def _set_cursor(conn, agent_id: str, fact_id: int) -> None:
    conn.execute(
        """INSERT INTO federation_broadcast (agent_id, last_fact_id, last_run_at)
           VALUES (?,?,CURRENT_TIMESTAMP)
           ON CONFLICT(agent_id) DO UPDATE SET
               last_fact_id=excluded.last_fact_id,
               last_run_at=CURRENT_TIMESTAMP""",
        (agent_id, fact_id),
    )
    conn.commit()


def collect_updates(
    agent_id: str = DEFAULT_AGENT,
    *,
    limit: int = BROADCAST_LIMIT,
    same_profile_only: bool = True,
    advance_cursor: bool = True,
) -> dict[str, Any]:
    """
    拉取「其他 Agent 在我上次读取之后新增的共享事实」。

    advance_cursor=False 时为预览模式（不推进游标），用于人工查看。
    """
    limit = max(1, min(int(limit), 200))
    peers = active_agent_ids(exclude=agent_id)
    if not peers:
        return {
            "status": "ok", "agent_id": agent_id, "peers": [],
            "new_facts": [], "count": 0, "note": "联邦内暂无其他在线 Agent",
        }

    conn = get_facts_conn()
    try:
        cursor = _get_cursor(conn, agent_id)
        peer_frag, peer_params = sqlbits.agent_in(peers)
        params: list[Any] = [cursor, *peer_params]
        shared_frag, _ = sqlbits.shared_only()
        sql = f"""
            SELECT id, agent_id, profile, category, fact_key, fact_value,
                   memory_tier, trust_score, updated_at
            FROM facts
            WHERE archived=0 AND {shared_frag}
              AND id > ? AND {peer_frag}
        """
        if same_profile_only:
            prof_frag, prof_params = sqlbits.profile_eq(resolve_profile(agent_id))
            sql += f" AND {prof_frag}"
            params.extend(prof_params)
        sql += " ORDER BY id ASC LIMIT ?"
        params.append(limit)

        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        if rows and advance_cursor:
            _set_cursor(conn, agent_id, rows[-1]["id"])
    except Exception as exc:
        logger.error("联邦广播失败（不影响本地记忆）: %s", exc)
        return {"status": "degraded", "agent_id": agent_id, "detail": str(exc),
                "new_facts": [], "count": 0}
    finally:
        conn.close()

    by_tier: dict[str, int] = {}
    by_peer: dict[str, int] = {}
    for row in rows:
        t = tier_mod.normalize_tier(row.get("memory_tier"))
        row["memory_tier"] = t
        by_tier[t] = by_tier.get(t, 0) + 1
        peer = row.get("agent_id") or DEFAULT_AGENT
        by_peer[peer] = by_peer.get(peer, 0) + 1

    return {
        "status": "ok",
        "agent_id": agent_id,
        "peers": peers,
        "cursor_advanced": bool(rows and advance_cursor),
        "new_facts": rows,
        "count": len(rows),
        "by_tier": by_tier,
        "by_peer": by_peer,
    }


def awareness_summary(agent_id: str = DEFAULT_AGENT) -> dict[str, Any]:
    """
    联邦感知摘要：给 Agent 看的结构化态势（不推进游标）。
    一句话回答「联邦里现在什么情况」。
    """
    conn = get_facts_conn()
    try:
        rows = conn.execute(
            """SELECT COALESCE(agent_id,?) AS agent_id,
                      COALESCE(profile,'default') AS profile,
                      COALESCE(memory_tier,'semantic') AS memory_tier,
                      COUNT(*) AS cnt
               FROM facts WHERE archived=0
               GROUP BY agent_id, profile, memory_tier""",
            (DEFAULT_AGENT,),
        ).fetchall()
    finally:
        conn.close()

    agents: dict[str, dict[str, Any]] = {}
    total = 0
    for row in rows:
        entry = agents.setdefault(
            row["agent_id"], {"profile": row["profile"], "total": 0, "tiers": {}}
        )
        entry["tiers"][row["memory_tier"]] = row["cnt"]
        entry["total"] += row["cnt"]
        total += row["cnt"]

    pending = collect_updates(agent_id, advance_cursor=False, limit=200)
    return {
        "status": "ok",
        "viewer": agent_id,
        "total_facts": total,
        "agents": agents,
        "pending_broadcast": pending.get("count", 0),
    }
