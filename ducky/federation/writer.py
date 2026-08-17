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

治理与账本（v19.4.0 · 生产审计 🟡-D）
    联邦 insert 是真实外部写入路径（/federation/facts/add），与
    /facts/add 同等对待：写入后过 B1 治理管线（规则 reject 同事务归档、
    待审降权 provisional、commit 后异步评估），三条路径（insert/update/
    merge）全部走 B5 事件账本留痕。治理/账本失败只降级不阻断写入。
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
    from ducky.security.injection_guard import validate_and_sanitize_memory_content
    is_safe, sanitized_val, rejection = validate_and_sanitize_memory_content(fact_value)
    if not is_safe:
        logger.warning("🛡️ [InjectionGuard] 联邦写入拦截注入: %s", rejection)
        return {"status": "error", "detail": f"Fact value rejected: {rejection}"}
    fact_value = sanitized_val

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
            # 📒 事件账本（v19.4.0 🟡-D）：apply_merge 已内部 commit，
            # 账本紧随补记；失败只降级不阻断。
            try:
                from ducky.event_ledger import content_hash, record_event
                record_event(conn, actor=source or "federation", action="update",
                             target_id=f"fact:{verdict.fact_id}",
                             reason=f"federation merge: {category}/{verdict.fact_key}",
                             after_hash=content_hash(fact_value))
                conn.commit()
            except Exception as le:
                logger.debug("merge 账本记录跳过: %s", le)
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
            # 📒 事件账本（v19.4.0 🟡-D）：与更新同事务留痕，同生共死
            try:
                from ducky.event_ledger import content_hash, record_event
                record_event(conn, actor=source or "federation", action="update",
                             target_id=f"fact:{verdict.fact_id}",
                             reason=f"federation update: {category}/{verdict.fact_key}",
                             after_hash=content_hash(fact_value))
            except Exception as le:
                logger.debug("update 账本记录跳过: %s", le)
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
        fact_id = cur.lastrowid or 0
        # 📒 事件账本（v19.4.0 🟡-D）：与写入同事务留痕，同生共死
        try:
            from ducky.event_ledger import content_hash, record_event
            record_event(conn, actor=source or "federation", action="add",
                         target_id=f"fact:{fact_key}",
                         reason=f"federation insert: {category}",
                         after_hash=content_hash(fact_value))
        except Exception as le:
            logger.debug("insert 账本记录跳过: %s", le)
        # 🏛️ 治理管线（v19.4.0 🟡-D）：联邦 insert 是真实外部路径，
        #    与 /facts/add 同等审计；失败只降级不阻断写入。
        gov = {"route": "skipped"}
        try:
            from ducky.governance import govern_fact_write
            gov = govern_fact_write(conn, fact_id, category, fact_key, fact_value,
                                    user_id=source or DEFAULT_USER_ID)
        except Exception as ge:
            logger.debug("联邦治理钩子跳过: %s", ge)
        conn.commit()
    except Exception as exc:
        logger.error("联邦写入失败: %s", exc)
        return {"status": "error", "detail": str(exc)}
    finally:
        conn.close()

    # 独立评估器异步补审（commit 后；失败保守进人审，绝不自动批准）
    if gov.get("route") == "llm_eval" and gov.get("candidate_id"):
        try:
            from ducky.governance import spawn_async_eval
            spawn_async_eval(gov["candidate_id"])
        except Exception as ae:
            logger.debug("联邦异步评估派发跳过: %s", ae)

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
        "governance": gov,
        "message": f"事实已存储: {category}/{fact_key}",
    }
