"""
ducky.federation.recall — 四级无缝降级检索链
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    L1  热通道   本 Agent SQL 精确 / LIKE 命中     亚毫秒，日常 99% 走这里
    L2  分层加权 命中不足 → 加入分层衰减重排序      毫秒级
    L3  联邦通道 仍不足 → 拉入同 profile 其他 Agent 的共享事实
    L4  全局兜底 再不足 → 跨 profile 全局扫描（只读，永不空手）

「降级」是无感的：调用方只拿到 results + 一份 ladder 轨迹说明走到了第几级。
任何一级异常都跳到下一级，绝不整链失败——记忆系统宁可给少，不可给崩。

Rerank 是按需的：只有 rerank=True 且候选 > top_k 时才做，
默认不做，保住热通道的 1ms 手感。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from ducky.federation import sqlbits
from ducky.federation import tier as tier_mod
from ducky.federation.schema import DEFAULT_AGENT, DEFAULT_PROFILE
from ducky.utils import get_facts_conn, jaccard_sim, parse_iso_timestamp

logger = logging.getLogger("aiduMEM.Federation.Recall")

# 每级至少要凑到 top_k 的这个比例，否则继续降级
SUFFICIENCY_RATIO = 0.6

# Rerank 前最多保留的候选数（控制 CPU 上限）
RERANK_CANDIDATE_CAP = 60


def _age_days(row: dict[str, Any]) -> float:
    stamp = row.get("recorded_at") or row.get("updated_at") or row.get("created_at")
    if not stamp:
        return 0.0
    try:
        recorded = parse_iso_timestamp(str(stamp))
    except Exception:
        return 0.0
    if not recorded:
        return 0.0
    return max(0.0, (time.time() - recorded) / 86400.0)


def _sql_search(
    conn,
    needle: str,
    *,
    agent_ids: list[str] | None,
    profile: str | None,
    category: str | None,
    limit: int,
    shared_only: bool = False,
    scope_frag: str = "",
    scope_params: list[Any] | None = None,
) -> list[dict[str, Any]]:
    like = f"%{needle}%"
    where = ["archived=0"]
    params: list[Any] = []

    if needle:
        where.append("(category LIKE ? OR fact_key LIKE ? OR fact_value LIKE ?)")
        params.extend([like, like, like])
    if category:
        where.append("category=?")
        params.append(category)
    if agent_ids:
        frag, frag_params = sqlbits.agent_in(agent_ids)
        where.append(frag)
        params.extend(frag_params)
    if profile:
        frag, frag_params = sqlbits.profile_eq(profile)
        where.append(frag)
        params.extend(frag_params)
    if shared_only:
        frag, frag_params = sqlbits.shared_only()
        where.append(frag)
        params.extend(frag_params)

    now_iso = datetime.now(timezone.utc).isoformat()
    # v20 P0-2：租户作用域片段（tenant_clause 产出，形如 " AND bank_id=? ..."），
    # 与 agent/profile/shared 谓词 AND 复合——梯子哪一级都翻不出域墙
    scope_frag = scope_frag or ""
    params.extend(scope_params or [])
    sql = f"""
        SELECT * FROM facts WHERE {' AND '.join(where)}{scope_frag}
        ORDER BY
          CASE
            WHEN valid_to   IS NOT NULL AND valid_to   < ? THEN 2
            WHEN valid_from IS NOT NULL AND valid_from > ? THEN 2
            ELSE 0
          END,
          CASE WHEN fact_key=? THEN 0 WHEN category=? THEN 1 ELSE 2 END,
          trust_score DESC, updated_at DESC
        LIMIT ?
    """
    params.extend([now_iso, now_iso, needle, needle, limit])
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _apply_tier_weight(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 层权重 × 衰减系数 × trust 重排，铁律(procedural)天然浮顶。"""
    for row in rows:
        row_tier = tier_mod.normalize_tier(row.get("memory_tier"))
        multiplier = tier_mod.score_multiplier(row_tier, _age_days(row))
        trust = float(row.get("trust_score") or 0.5)
        row["memory_tier"] = row_tier
        row["tier_multiplier"] = round(multiplier, 4)
        row["fed_score"] = round(multiplier * trust, 6)
    rows.sort(key=lambda r: r["fed_score"], reverse=True)
    return rows


def _rerank(needle: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    按需 Rerank：词级 Jaccard 与分层得分融合（0.6 语义 + 0.4 分层）。
    纯本地、无模型依赖——保证任何环境都能跑，不引入外部推理成本。
    """
    if not needle:
        return rows
    for row in rows[:RERANK_CANDIDATE_CAP]:
        text = f"{row.get('fact_key','')} {row.get('fact_value','')}"
        sim = jaccard_sim(needle, text)
        row["rerank_sim"] = round(sim, 4)
        row["fed_score"] = round(0.6 * sim + 0.4 * float(row.get("fed_score") or 0.0), 6)
    rows.sort(key=lambda r: r.get("fed_score") or 0.0, reverse=True)
    return rows


def _dedup_by_id(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[Any] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        fid = row.get("id")
        if fid is not None:
            if fid in seen:
                continue
            seen.add(fid)
        out.append(row)
    return out


def federated_recall(
    query: str,
    *,
    agent_id: str = DEFAULT_AGENT,
    profile: str | None = None,
    category: str | None = None,
    top_k: int = 10,
    federated: bool = True,
    rerank: bool = False,
    tier_filter: str | None = None,
    user_id: str = "",
    bank_id: str = "",
) -> dict[str, Any]:
    """
    联邦检索主入口。返回 {status, level, results, ladder, elapsed_ms}。

    federated=False 时最多走到 L2（纯本 Agent 热通道），
    这是日常陪伴的默认路径：一次 SQL，不触碰联邦。

    v20 P0-2（opt-in 作用域）：传了 user_id/bank_id 任一，四级梯子
    （L1 热通道到 L4 全局兜底）全部按 tenant_clause 收窄——「全局」
    只在本域内全局，绝不翻域墙。不传 = v19 管理员全库语义零改动。
    非法作用域在进梯子前抛 BankScopeError，绝不静默降级成全库扫描。
    """
    started = time.perf_counter()
    needle = (query or "").strip()
    top_k = max(1, min(int(top_k), 100))
    need = max(1, int(top_k * SUFFICIENCY_RATIO))
    ladder: list[dict[str, Any]] = []
    reached = "L1"

    scope_uid, scope_bid = "", ""
    if user_id or bank_id:
        from ducky.bank_contract import normalize_bank_id, normalize_user_id
        # 校验放在 try 外：非法域必须炸出去，不许落成 degraded 空手而归
        scope_uid = normalize_user_id(user_id) if user_id else ""
        scope_bid = normalize_bank_id(bank_id or "default")

    conn = get_facts_conn()
    scope_frag: str = ""
    scope_params: list[Any] = []
    if user_id or bank_id:
        from ducky.facts_recall import tenant_clause
        scope_frag, scope_params = tenant_clause(
            scope_uid, bank_id=scope_bid, conn=conn
        )
        ladder.append({"level": "scope",
                       "scope": f"tenant:{scope_uid or 'default'}/{scope_bid}"})
    try:
        # ── L1 热通道：本 Agent ───────────────────────
        rows = _sql_search(
            conn, needle, agent_ids=[agent_id], profile=None,
            category=category, limit=top_k * 3,
            scope_frag=scope_frag, scope_params=scope_params,
        )
        ladder.append({"level": "L1", "scope": f"agent:{agent_id}", "hits": len(rows)})

        # ── L2 分层加权重排 ──────────────────────────
        if rows:
            rows = _apply_tier_weight(rows)
            reached = "L2"
            ladder.append({"level": "L2", "scope": "tier_weighted", "hits": len(rows)})

        # ── L3 联邦通道：同 profile 其他 Agent ────────
        if federated and len(rows) < need:
            target_profile = profile or DEFAULT_PROFILE
            fed_rows = _sql_search(
                conn, needle, agent_ids=None, profile=target_profile,
                category=category, limit=top_k * 3, shared_only=True,
                scope_frag=scope_frag, scope_params=scope_params,
            )
            rows = _apply_tier_weight(_dedup_by_id(rows + fed_rows))
            reached = "L3"
            ladder.append({"level": "L3", "scope": f"profile:{target_profile}", "hits": len(fed_rows)})

        # ── L4 全局兜底：跨 profile ──────────────────
        if federated and len(rows) < need:
            global_rows = _sql_search(
                conn, needle, agent_ids=None, profile=None,
                category=category, limit=top_k * 3, shared_only=True,
                scope_frag=scope_frag, scope_params=scope_params,
            )
            rows = _apply_tier_weight(_dedup_by_id(rows + global_rows))
            reached = "L4"
            ladder.append({"level": "L4", "scope": "global", "hits": len(global_rows)})
    except Exception as exc:
        logger.error("联邦检索异常，返回空结果而非抛错: %s", exc)
        return {
            "status": "degraded",
            "level": reached,
            "query": needle,
            "results": [],
            "count": 0,
            "ladder": ladder + [{"level": reached, "error": str(exc)}],
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        }
    finally:
        conn.close()

    if tier_filter:
        wanted = tier_mod.normalize_tier(tier_filter)
        rows = [r for r in rows if tier_mod.normalize_tier(r.get("memory_tier")) == wanted]
        ladder.append({"level": "filter", "scope": f"tier:{wanted}", "hits": len(rows)})

    if rerank and len(rows) > top_k:
        rows = _rerank(needle, rows)
        ladder.append({"level": "rerank", "scope": "jaccard+tier", "candidates": len(rows)})

    results = rows[:top_k]
    return {
        "status": "ok",
        "level": reached,
        "query": needle,
        "agent_id": agent_id,
        "results": results,
        "count": len(results),
        "ladder": ladder,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
    }
