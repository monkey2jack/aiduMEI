"""
ducky.federation.writer — 联邦写入（去重 + 分层 + 归属）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

一条事实进来，依次经过四道关：
    1. 归属   agent_id / profile / shared + (user_id, bank_id) 落定「这是谁的记忆」
    2. 分层   显式 tier 优先，否则从 category/key/value 推断
    3. 去重   同 agent 同 (user, bank) 同 category 内查相似度 → merge / update / insert
    4. 落库   附 recorded_at + decay_at，procedural 层 decay_at 为 NULL

不做的事：不删任何既有行、不跨 Agent 改别人的记忆、不跨 bank 改别库的记忆。
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
from ducky.bank_contract import DEFAULT_BANK_ID, make_scope
from ducky.federation.registry import heartbeat
from ducky.federation.schema import DEFAULT_AGENT, DEFAULT_PROFILE
from ducky.utils import DEFAULT_USER_ID, get_facts_conn

logger = logging.getLogger("aiduMEM.Federation.Writer")


def _summary_of(value: str) -> str:
    value = value or ""
    return f"{value[:60]}{'...' if len(value) > 60 else ''}"


def _upsert_fact_row(conn, *, category, fact_key, fact_value, source, agent_id,
                     profile, resolved_tier, recorded_at, decay_at, tags, shared,
                     valid_from, valid_to, scope, initial_hash,
                     summary) -> tuple[int, int]:
    """写入/改写 facts 行并返回真实 (row_id, version)。

    v20.5.0 正式版（用户审计 🔴-1）：从 `write_fact` 中抽出——原实现直接用
    `cur.lastrowid`，冲突命中时那不是被更新行的 id，谱系因此串链并产生幽灵链。
    改走 `upsert_returning_id`（RETURNING id / 唯一键回查），绝不信任 lastrowid。

    抽成独立函数的第二个理由：`write_fact` 已是 CC 47 的 F 级函数，任务书
    明令「T1 修复时不得再加重该函数职责」——本次修复把这段整体移出，
    使 `write_fact` 的圈复杂度不升反降。
    """
    from ducky.utils import upsert_returning_id
    # ON CONFLICT 目标必须与 idx_facts_unique 列集完全一致
    # （见 federation/schema.py FACTS_UNIQUE_COLUMNS），否则报
    # "no such conflict target"。
    return upsert_returning_id(
        conn,
        """INSERT INTO facts
             (category, fact_key, fact_value, source, summary, overview,
              agent_id, profile, memory_tier, recorded_at, decay_at, tags, shared,
              valid_from, valid_to, user_id, bank_id, content_hash, version, previous_version_hash, last_actor)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(agent_id, user_id, bank_id, category, fact_key) DO UPDATE SET
               fact_value=excluded.fact_value,
               summary=excluded.summary,
               overview=excluded.overview,
               memory_tier=excluded.memory_tier,
               recorded_at=excluded.recorded_at,
               decay_at=excluded.decay_at,
               source=excluded.source,
               content_hash=excluded.content_hash,
               version=facts.version + 1,
               previous_version_hash=facts.content_hash,
               last_actor=excluded.last_actor,
               updated_at=CURRENT_TIMESTAMP""",
        (category, fact_key, fact_value, source, summary, fact_value,
         agent_id, profile, resolved_tier, recorded_at, decay_at, tags,
         1 if shared else 0, valid_from or None, valid_to or None,
         scope.user_id, scope.bank_id, initial_hash, 1, "", source or agent_id),
        "SELECT id, version FROM facts WHERE agent_id=? AND user_id=? AND bank_id=? AND category=? AND fact_key=?",
        (agent_id, scope.user_id, scope.bank_id, category, fact_key),
    )


# ── 子步骤（v20.5.1 · T-13 圈复杂度整改）─────────────────────────────
#
# write_fact 曾是 CC 44（radon F 级）的巨函数：校验闸门、归属/分层、去重
# 三分支（merge/update/insert）、谱系链、事件账本、治理钩子全在一个函数
# 体里。这里只做**换骨架**（与 scoring.py v20.4.1a 同款打法）：每道子步骤
# 一个可独立测试的函数，编排函数只负责流程组合；SQL、哈希链推进顺序、
# 账本顺序、返回值结构逐行未动 —— 谱系行为一丁点都不能变。


def _strip_and_guard_fact(fact_key, fact_value):
    """入参剥离 + 终审注入防护。返回 (fact_key, fact_value, error|None)。"""
    fact_key = (fact_key or "").strip()
    fact_value = (fact_value or "").strip()
    if not fact_key or not fact_value:
        return fact_key, fact_value, {"status": "error", "detail": "fact_key 和 fact_value 不能为空"}
    from ducky.security.injection_guard import validate_and_sanitize_memory_content
    is_safe, sanitized_val, rejection = validate_and_sanitize_memory_content(fact_value)
    if not is_safe:
        logger.warning("🛡️ [InjectionGuard] 联邦写入拦截注入: %s", rejection)
        return fact_key, fact_value, {"status": "error", "detail": f"Fact value rejected: {rejection}"}
    return fact_key, sanitized_val, None


def _normalize_scope_tier(*, category, agent_id, profile, user_id, bank_id,
                          memory_tier, fact_key, fact_value):
    """归属落定 + 分层解析。返回 (category, agent_id, profile, scope, tier, recorded_at, decay_at)。"""
    category = (category or "general").strip()
    agent_id = (agent_id or DEFAULT_AGENT).strip() or DEFAULT_AGENT
    profile = (profile or DEFAULT_PROFILE).strip() or DEFAULT_PROFILE
    scope = make_scope(user_id, bank_id)  # 规范化 + 非法字符拒绝

    resolved_tier = (
        tier_mod.normalize_tier(memory_tier)
        if memory_tier
        else tier_mod.infer_tier(category, fact_key, fact_value)
    )
    now = datetime.now(timezone.utc)
    recorded_at = now.isoformat()
    decay_at = tier_mod.decay_deadline(resolved_tier, now)
    return category, agent_id, profile, scope, resolved_tier, recorded_at, decay_at


def _verdict_hits(verdict, action: str) -> bool:
    """去重判定命中指定动作且有落点行 id —— 三分支共用同一道闸。"""
    return bool(verdict and verdict.action == action and verdict.fact_id)


def _merge_fact_branch(conn, *, verdict, fact_value, tags, category,
                       resolved_tier, agent_id, source, scope) -> dict:
    """── 合并：不新增行 ──"""
    merged = apply_merge(verdict.fact_id, fact_value, tags, conn=conn)
    # 📒 事件账本（v19.4.0 🟡-D）：apply_merge 已内部 commit，
    # 账本紧随补记；失败只降级不阻断。
    try:
        from ducky.event_ledger import content_hash, record_event
        record_event(conn, actor=source or "federation", action="update",
                     target_id=f"fact:{verdict.fact_id}",
                     reason=f"federation merge: {category}/{verdict.fact_key}",
                     after_hash=content_hash(fact_value),
                     user_id=scope.user_id, bank_id=scope.bank_id)
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


def _update_fact_branch(conn, *, verdict, fact_value, resolved_tier, agent_id,
                        category, source, scope) -> dict:
    """── 更新：视为同一事实的新版本，就地覆盖 ──

    🟢25：不重置 recorded_at/decay_at，与 dedup.apply_merge 语义对齐，
    避免 0.70-0.85 相似度更新反复刷新衰减时钟让旧事实"无限续命"。
    """
    # 🧬 密码学谱系 (v20.5.0a): 计算新 hash 并版本自增
    from ducky.memory_lineage import compute_content_hash, record_lineage
    new_hash = compute_content_hash(fact_value)
    old_row = conn.execute(
        "SELECT content_hash, version FROM facts WHERE id=?", (verdict.fact_id,)
    ).fetchone()
    prev_hash = old_row[0] if old_row else ""
    old_ver = (old_row[1] or 1) if old_row else 1
    next_ver = old_ver + 1

    conn.execute(
        """UPDATE facts
           SET fact_value=?, overview=?, summary=?, memory_tier=?,
               source=?, content_hash=?, version=?, previous_version_hash=?,
               last_actor=?, updated_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (fact_value, fact_value, _summary_of(fact_value), resolved_tier,
         source, new_hash, next_ver, prev_hash, source or agent_id, verdict.fact_id),
    )
    # 🧬 memory_lineage 链式账本记录
    try:
        record_lineage(
            conn,
            memory_id=f"fact:{verdict.fact_id}",
            content=fact_value,
            action="UPDATE",
            actor=source or agent_id,
            previous_version_hash=prev_hash,
            source=source,
            diff_summary=f"federation update: {category} (hash {new_hash[:8]})",
        )
    except Exception as le:
        logger.debug("lineage 记录跳过: %s", le)

    # 📒 事件账本（v19.4.0 🟡-D）：与更新同事务留痕，同生共死
    try:
        from ducky.event_ledger import content_hash, record_event
        record_event(conn, actor=source or "federation", action="update",
                     target_id=f"fact:{verdict.fact_id}",
                     reason=f"federation update: {category}/{verdict.fact_key}",
                     after_hash=content_hash(fact_value),
                     user_id=scope.user_id, bank_id=scope.bank_id)
    except Exception as le:
        logger.debug("update 账本记录跳过: %s", le)
    conn.commit()
    return {
        "status": "ok", "action": ACTION_UPDATE, "fact_id": verdict.fact_id,
        "memory_tier": resolved_tier, "agent_id": agent_id,
        "version": next_ver, "content_hash": new_hash,
        "dedup": verdict.to_dict(),
        "message": f"事实已更新: {category}/{verdict.fact_key}",
    }


def _insert_fact_branch(conn, *, category, fact_key, fact_value, source, agent_id,
                        profile, resolved_tier, recorded_at, decay_at, tags, shared,
                        valid_from, valid_to, scope) -> tuple[int, str, dict]:
    """── 新增（upsert）──：落库 + 谱系 + 事件账本 + 治理钩子，同事务 commit。

    返回 (fact_id, upsert_action, gov)；gov 带出治理结论供编排层在
    commit 后派异步评估。
    """
    from ducky.memory_lineage import compute_content_hash, record_lineage
    initial_hash = compute_content_hash(fact_value)
    # v20.5.0 正式版（用户审计 🔴-1）：upsert 冲突命中时 lastrowid 不是
    # 被更新行的 id——谱系曾因此串链并产生幽灵链。改走 RETURNING/唯一键
    # 回查拿真实行 id（实现见 _upsert_fact_row），version<=1 为首写、>1 为冲突改写。
    fact_id, row_ver = _upsert_fact_row(
        conn, category=category, fact_key=fact_key, fact_value=fact_value,
        source=source, agent_id=agent_id, profile=profile,
        resolved_tier=resolved_tier, recorded_at=recorded_at, decay_at=decay_at,
        tags=tags, shared=shared, valid_from=valid_from, valid_to=valid_to,
        scope=scope, initial_hash=initial_hash, summary=_summary_of(fact_value),
    )
    upsert_action = "CREATE" if row_ver <= 1 else "UPDATE"

    # 🧬 memory_lineage 链式账本记录
    try:
        record_lineage(
            conn,
            memory_id=f"fact:{fact_id}",
            content=fact_value,
            action=upsert_action,
            actor=source or agent_id,
            previous_version_hash="",
            source=source,
            diff_summary=f"federation {upsert_action.lower()}: {category} (hash {initial_hash[:8]})",
        )
    except Exception as le:
        logger.debug("lineage 记录跳过: %s", le)

    # 📒 事件账本（v19.4.0 🟡-D）：与写入同事务留痕，同生共死
    try:
        from ducky.event_ledger import content_hash, record_event
        record_event(conn, actor=source or "federation", action="add",
                     target_id=f"fact:{fact_key}",
                     reason=f"federation insert: {category}",
                     after_hash=content_hash(fact_value),
                     user_id=scope.user_id, bank_id=scope.bank_id)
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
    return fact_id, upsert_action, gov


def _spawn_async_eval(gov: dict) -> None:
    """独立评估器异步补审（commit 后；失败保守进人审，绝不自动批准）。"""
    if gov.get("route") == "llm_eval" and gov.get("candidate_id"):
        try:
            from ducky.governance import spawn_async_eval
            spawn_async_eval(gov["candidate_id"])
        except Exception as ae:
            logger.debug("联邦异步评估派发跳过: %s", ae)


def write_fact(
    category: str,
    fact_key: str,
    fact_value: str,
    *,
    agent_id: str = DEFAULT_AGENT,
    profile: str = DEFAULT_PROFILE,
    memory_tier: str | None = None,
    source: str = DEFAULT_USER_ID,
    user_id: str = DEFAULT_USER_ID,
    bank_id: str = DEFAULT_BANK_ID,
    tags: str = "",
    shared: bool = True,
    dedup: bool = True,
    valid_from: str = "",
    valid_to: str = "",
) -> dict[str, Any]:
    """写入一条联邦事实。返回含 action(insert/update/merge) 的结果。

    v20 P0-2：user_id/bank_id 是行的归属库（作用域），source 仍是「谁写的」
    （行为归因），二者语义不同，不再互相顶替。不传作用域时落 default 库，
    与 v19 行为逐字节一致。
    """
    fact_key, fact_value, err = _strip_and_guard_fact(fact_key, fact_value)
    if err is not None:
        return err

    category, agent_id, profile, scope, resolved_tier, recorded_at, decay_at = (
        _normalize_scope_tier(
            category=category, agent_id=agent_id, profile=profile,
            user_id=user_id, bank_id=bank_id, memory_tier=memory_tier,
            fact_key=fact_key, fact_value=fact_value)
    )

    conn = get_facts_conn()
    try:
        verdict = (
            check_duplicate(fact_value, category=category, agent_id=agent_id,
                            user_id=scope.user_id, bank_id=scope.bank_id, conn=conn)
            if dedup
            else None
        )

        # ── 合并：不新增行 ──
        if _verdict_hits(verdict, ACTION_MERGE):
            return _merge_fact_branch(
                conn, verdict=verdict, fact_value=fact_value, tags=tags,
                category=category, resolved_tier=resolved_tier,
                agent_id=agent_id, source=source, scope=scope)

        # ── 更新：视为同一事实的新版本，就地覆盖 ──
        if _verdict_hits(verdict, ACTION_UPDATE):
            return _update_fact_branch(
                conn, verdict=verdict, fact_value=fact_value,
                resolved_tier=resolved_tier, agent_id=agent_id,
                category=category, source=source, scope=scope)

        # ── 新增 ──
        fact_id, upsert_action, gov = _insert_fact_branch(
            conn, category=category, fact_key=fact_key, fact_value=fact_value,
            source=source, agent_id=agent_id, profile=profile,
            resolved_tier=resolved_tier, recorded_at=recorded_at, decay_at=decay_at,
            tags=tags, shared=shared, valid_from=valid_from, valid_to=valid_to,
            scope=scope)
    except Exception as exc:
        logger.error("联邦写入失败: %s", exc)
        return {"status": "error", "detail": str(exc)}
    finally:
        conn.close()

    _spawn_async_eval(gov)

    try:
        heartbeat(agent_id)
    except Exception:
        pass  # 心跳失败不影响写入结果

    return {
        "status": "ok",
        "action": ACTION_INSERT if upsert_action == "CREATE" else ACTION_UPDATE,
        "fact_id": fact_id,
        "agent_id": agent_id,
        "profile": profile,
        "memory_tier": resolved_tier,
        "decay_at": decay_at,
        "dedup": verdict.to_dict() if verdict else {"action": "skipped"},
        "governance": gov,
        "message": f"事实已存储: {category}/{fact_key}" if upsert_action == "CREATE"
                   else f"事实已更新: {category}/{fact_key}",
    }
