"""
ducky.federation.writer — 联邦写入（去重 + 分层 + 归属）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

一条事实进来，依次经过四道关：
    1. 归属   agent_id / profile / shared 落定「这是谁的记忆」
    2. 分层   显式 tier 优先，否则从 category/key/value 推断
    3. 去重   同 agent 同 category 内查相似度 → merge / update / insert
    4. 落库   附 recorded_at + decay_at，procedural 层 decay_at 为 NULL

不做的事：不删任何既有行、不跨 Agent 改别人的记忆。
写入永远是加法或就地合并，这是可控性的底线。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ducky.federation import tier as tier_mod
from ducky.federation.dedup import (
    ACTION_INSERT,
    ACTION_MERGE,
    ACTION_UPDATE,
    apply_merge,
    check_duplicate,
)
from ducky.federation.registry import heartbeat
from ducky.federation.schema import DEFAULT_AGENT, DEFAULT_PROFILE
from ducky.utils import DEFAULT_USER_ID, get_facts_conn

logger = logging.getLogger("aiduMEM.Federation.Writer")


def _summary_of(value: str) -> str:
    value = value or ""
    return f"{value[:60]}{'...' if len(value) > 60 else ''}"


def write_fact(
    category: str,
    fact_key: str,
    fact_value: str,
    *,
    agent_id: str = DEFAULT_AGENT,
    profile: str = DEFAULT_PROFILE,
    memory_tier: str | None = None,
    source: str = DEFAULT_USER_ID,
    tags: str = "",
    shared: bool = True,
    dedup: bool = True,
    valid_from: str = "",
    valid_to: str = "",
) -> dict[str, Any]:
    """写入一条联邦事实。返回含 action(insert/update/merge) 的结果。"""
    fact_key = (fact_key or "").strip()
    fact_value = (fact_value or "").strip()
    if not fact_key or not fact_value:
        return {"status": "error", "detail": "fact_key 和 fact_value 不能为空"}

    category = (category or "general").strip()
    agent_id = (agent_id or DEFAULT_AGENT).strip() or DEFAULT_AGENT
    profile = (profile or DEFAULT_PROFILE).strip() or DEFAULT_PROFILE

    resolved_tier = (
        tier_mod.normalize_tier(memory_tier)
        if memory_tier
        else tier_mod.infer_tier(category, fact_key, fact_value)
    )
    now = datetime.now(timezone.utc)
    recorded_at = now.isoformat()
    decay_at = tier_mod.decay_deadline(resolved_tier, now)

    conn = get_facts_conn()
    try:
        verdict = (
            check_duplicate(fact_value, category=category, agent_id=agent_id, conn=conn)
            if dedup
            else None
        )

        # ── 合并：不新增行 ──
        if verdict and verdict.action == ACTION_MERGE and verdict.fact_id:
            merged = apply_merge(verdict.fact_id, fact_value, tags, conn=conn)
            merged.update({
                "dedup": verdict.to_dict(),
                "memory_tier": resolved_tier,
                "agent_id": agent_id,
                "message": f"与既有事实合并: {category}/{verdict.fact_key}",
            })
            return merged

        # ── 更新：视为同一事实的新版本，就地覆盖 ──
        # 🟢25：不重置 recorded_at/decay_at，与 dedup.apply_merge 语义对齐，
        # 避免 0.70-0.85 相似度更新反复刷新衰减时钟让旧事实"无限续命"。
        if verdict and verdict.action == ACTION_UPDATE and verdict.fact_id:
            conn.execute(
                """UPDATE facts
                   SET fact_value=?, overview=?, summary=?, memory_tier=?,
                       source=?, updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (fact_value, fact_value, _summary_of(fact_value), resolved_tier,
                 source, verdict.fact_id),
            )
            conn.commit()
            return {
                "status": "ok", "action": ACTION_UPDATE, "fact_id": verdict.fact_id,
                "memory_tier": resolved_tier, "agent_id": agent_id,
                "dedup": verdict.to_dict(),
                "message": f"事实已更新: {category}/{verdict.fact_key}",
            }

        # ── 新增 ──
        cur = conn.execute(
            """INSERT INTO facts
                 (category, fact_key, fact_value, source, summary, overview,
                  agent_id, profile, memory_tier, recorded_at, decay_at, tags, shared,
                  valid_from, valid_to)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(agent_id, category, fact_key) DO UPDATE SET
                   fact_value=excluded.fact_value,
                   summary=excluded.summary,
                   overview=excluded.overview,
                   memory_tier=excluded.memory_tier,
                   recorded_at=excluded.recorded_at,
                   decay_at=excluded.decay_at,
                   source=excluded.source,
                   updated_at=CURRENT_TIMESTAMP""",
            (category, fact_key, fact_value, source, _summary_of(fact_value), fact_value,
             agent_id, profile, resolved_tier, recorded_at, decay_at, tags,
             1 if shared else 0, valid_from or None, valid_to or None),
        )
        conn.commit()
        fact_id = cur.lastrowid or 0
    except Exception as exc:
        logger.error("联邦写入失败: %s", exc)
        return {"status": "error", "detail": str(exc)}
    finally:
        conn.close()

    try:
        heartbeat(agent_id)
    except Exception:
        pass  # 心跳失败不影响写入结果

    return {
        "status": "ok",
        "action": ACTION_INSERT,
        "fact_id": fact_id,
        "agent_id": agent_id,
        "profile": profile,
        "memory_tier": resolved_tier,
        "decay_at": decay_at,
        "dedup": verdict.to_dict() if verdict else {"action": "skipped"},
        "message": f"事实已存储: {category}/{fact_key}",
    }
