#!/usr/bin/env python3
"""
aiduMEM Recall Funnel: 搜索链路可观测模块
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Aletheia Memory 设计哲学：
- 候选池 → 🔥 Ignition（高相似度直达） → 去重 → 时间衰减 → 最终
- Ignition: J-space 启发——高 sim 记忆跳过衰减管道
- 每步决策可追溯、可调试
"""

import os
import time
from ducky.scoring import score_and_rank_candidates, logging

from .utils import get_facts_conn
from .evolve_mem import log_search_quality as _evolve_log_search

logger = logging.getLogger("aiduMEM.funnel")

# ── 配置 ──
MAX_CANDIDATE_MULT = 3   # 候选池倍数
IGNITION_THRESHOLD = 0.85
IGNITION_MAX = 8
IGNITION_BOOST = 1.5


# ── 子步骤（v20.5.1 · T-13 圈复杂度整改）─────────────────────────────
#
# funnel_search 曾是 CC 41（radon F 级）的巨函数：候选池降级链、Ignition、
# 文本去重、superseded 批量过滤、点火分融合、增益收敛全在一个函数体里。
# 这里只做**换骨架**（与 scoring.py v20.4.1a 同款打法）：每个 Stage 一个
# 可独立测试的函数，编排函数只负责流程组合与 trace 组装；判据、取字段
# 顺序、stage 遥测键、返回值结构逐行未动。


def _fetch_candidate_pool(memory, query: str, user_id: str, bank_id: str, limit: int,
                          session_id: str = ""):
    """Stage 1: 候选池 — 扩大搜索。返回 (candidates, stage)。"""
    t0 = time.time()
    try:
        # 🔴v20：默认域不下推 bank_id（存量向量 payload 无此字段，下推即清零），
        # 命名域下推；两种情况都在拿到候选后按域复筛。
        from ducky.bank_contract import vector_item_in_bank, vector_scope_filters
        candidates_raw = memory.search(query, filters=vector_scope_filters(user_id, bank_id), limit=limit * MAX_CANDIDATE_MULT)
        # mem.search 在 BM25/混合召回内部失败时可能返回 None，必须安全降级。
        if candidates_raw is None:
            logger.warning("候选池: mem.search 返回 None，降级到 hybrid_search")
            try:
                from ducky.mem0_runtime import lazy_import_hybrid
                # v21.2.0 审计整改轮：降级腿此前既没传 bank_id 也没传 session_id
                # —— 命名域下打分会用 default 去查类型/出身/信用三张账本（一条
                # 都查不到，六型加权与出身乘数在这条腿上静默失效，正是 v20.2.4
                # F-15 修过的病在降级路径复发），回声抑制同样不生效。
                candidates_raw = lazy_import_hybrid()(
                    memory, query, user_id, limit * MAX_CANDIDATE_MULT,
                    bank_id=bank_id, session_id=session_id) or []
            except Exception as e:
                logger.warning(f"候选池: hybrid_search 降级也失败: {e}")
                candidates_raw = []
        candidates = candidates_raw.get("results", candidates_raw) if isinstance(candidates_raw, dict) else candidates_raw
        if not isinstance(candidates, list):
            candidates = []
        candidates = [c for c in candidates if vector_item_in_bank(c, bank_id)]
    except Exception as e:
        logger.warning(f"候选池搜索失败: {e}")
        candidates = []
    stage = {"name": "candidate_pool", "count": len(candidates), "ms": int((time.time()-t0)*1000)}
    return candidates, stage


def _apply_ignition(query: str, candidates: list, enable_ignition: bool):
    """Stage 2: 🔥 Ignition — 高相似度记忆点火直达。返回 (ignited, remaining, stage|None)。"""
    if not enable_ignition:
        return [], candidates, None
    try:
        from .memory_ignition import ignition_filter
        ign_result = ignition_filter(query, candidates, threshold=IGNITION_THRESHOLD, max_ignited=IGNITION_MAX)
        ignited = ign_result["ignited"]
        remaining = ign_result["remaining"]
        stage = {
            "name": "ignition",
            "ignited": len(ignited),
            "remaining": len(remaining),
            "threshold": IGNITION_THRESHOLD,
            "ms": ign_result["stats"]["ms"],
        }
        return ignited, remaining, stage
    except ImportError:
        logger.debug("Ignition 模块不可用，跳过")
        return [], candidates, None


def _dedup_candidates(ignited: list, remaining: list):
    """Stage 3: 去重 — 相同 memory 文本去重，ignition 优先。返回 (deduped_ignited, deduped_remaining, stage)。"""
    t0 = time.time()
    seen = set()
    deduped_ignited = []
    for item in ignited:
        if not isinstance(item, dict):
            continue
        text = item.get("memory", "")
        key = text[:100]
        if key not in seen:
            seen.add(key)
            deduped_ignited.append(item)

    deduped_remaining = []
    for item in remaining:
        if not isinstance(item, dict):
            continue
        text = item.get("memory", "")
        key = text[:100]
        if key not in seen:
            seen.add(key)
            deduped_remaining.append(item)
    stage = {
        "name": "dedup",
        "ignited": len(deduped_ignited),
        "remaining": len(deduped_remaining),
        "ms": int((time.time()-t0)*1000),
    }
    return deduped_ignited, deduped_remaining, stage


def _load_superseded_ids(candidate_ids: list) -> set:
    """Lethe v9.2.0: 批量获取 memory_states 状态（被取代集合）。失败按无取代降级。"""
    superseded_ids = set()
    if not candidate_ids:
        return superseded_ids
    try:
        # 批量获取被取代的状态 (from facts.db)
        conn_facts = get_facts_conn()
        placeholders = ",".join("?" for _ in candidate_ids)
        states = conn_facts.execute(
            f"SELECT memory_id FROM memory_states WHERE memory_id IN ({placeholders}) AND state = 'superseded'",
            candidate_ids
        ).fetchall()
        superseded_ids = {row[0] for row in states}
        conn_facts.close()
    except Exception as e:
        logger.debug(f"从数据库获取 lane 映射或状态失败: {e}")
    return superseded_ids


def _drop_superseded(items: list, superseded_ids: set) -> list:
    """过滤掉已被取代的记忆 (Lethe v9.2.0)。"""
    kept = []
    for item in items:
        if item.get("id") in superseded_ids:
            logger.info(f"Lethe 过滤已取代记忆: {item.get('id', '')[:8]} '{item.get('memory', '')[:20]}'")
            continue
        kept.append(item)
    return kept


def _fuse_ignition_scores(candidates_to_score: list) -> None:
    """Stage 4 前半：Ignition 特征融合进入 score 供 scoring 引擎归一化。"""
    for item in candidates_to_score:
        if item.get("_ignited"):
            ign_score = item.get("_ignition_score", 0) or 0
            base_s = item.get("score", 0) or 0
            item["score"] = max(base_s, ign_score)
            item.setdefault("metadata", {})
            if isinstance(item["metadata"], dict):
                item["metadata"]["is_ignited"] = True


def _rollup_enabled() -> bool:
    """v21.2 M7：episode rollup 开关。**默认关** —— 它依赖 M1 攒够轨迹数据
    才有意义，先让数据跑一段时间，观察 /evolve/report 的 episodes 维度再开。"""
    return os.getenv("AIDUMEI_ROLLUP_ENABLED", "0").strip() in ("1", "true", "True")


ROLLUP_MAX_STEPS = 6
ROLLUP_LINE_CHARS = 200


def _apply_episode_rollup(final: list, limit: int, spare: list | None = None) -> tuple:
    """同 episode 命中 ≥2 条时，拼一个「轨迹摘要」块替代其中的低分单条。

    借鉴 Memmy 的 episode rollup 设计（≤6 步、与单条去重）；**拼接式摘要，
    零 LLM**。返回 (结果列表, 本次生成的 rollup 数)。

    v21.2.0 审计整改轮，修掉两处「默认关掩盖着的空转」：

    1. **`limit` 原本从未被使用**。同组成员被折叠掉之后没有回填，而调用点
       在此之前已经按 limit 截断过了 —— 于是一开开关，请求 10 条、命中 3 条
       同 episode 就只回 8 条：聚合本该「换一种呈现」，却变成了「少给你两条」。
       现在用 ``spare``（落榜候选，按分降序）回填到 limit。
    2. **去重只覆盖了前 6 条**。`members[:ROLLUP_MAX_STEPS]` 先截到 6，
       drop 集只从这 6 条里取，第 7 条及以后既不进摘要、也不被移除 ——
       结果里「轨迹摘要」和原始单条同时出现，与 docstring 说的「与单条去重」
       正好相反。现在摘要仍只展示前 6 步（避免注入预算爆掉），但**整组**去重。
    """
    if not _rollup_enabled() or len(final) < 2:
        return final, 0
    try:
        from ducky.evolve_mem import get_episode_groups
        from ducky.memory_types import memory_type_ref
        groups = get_episode_groups([memory_type_ref(it) for it in final])
        if not groups:
            return final, 0
        buckets: dict = {}
        for it in final:
            g = groups.get(memory_type_ref(it))
            if g:
                buckets.setdefault(g[0], []).append((g[1], it))
        made = 0
        for ep_id, members in buckets.items():
            if len(members) < 2:
                continue
            members.sort(key=lambda x: x[0])
            shown = members[:ROLLUP_MAX_STEPS]          # 摘要只展示前 6 步
            lines = [f"{idx}. {str(it.get('memory', ''))[:ROLLUP_LINE_CHARS]}"
                     for idx, it in shown]
            all_items = [it for _idx, it in members]    # 去重覆盖**整组**
            # 用组内最高分那条的位置承载 rollup，其余同组条目移除（去重）
            ranked = sorted(all_items, key=lambda x: x.get("_hybrid_score", 0),
                            reverse=True)
            host = ranked[0]
            host["memory"] = "【轨迹摘要】\n" + "\n".join(lines)
            host["_rollup"] = {"episode_id": ep_id, "steps": len(shown),
                               "folded": len(all_items)}
            drop = {id(x) for x in ranked[1:]}
            final = [x for x in final if id(x) not in drop]
            made += 1

        # 折叠腾出的名额用落榜候选回填 —— 聚合是换一种呈现，不是少给结果。
        if made and limit and len(final) < limit:
            _seen = {id(x) for x in final}
            for cand in (spare or []):
                if len(final) >= limit:
                    break
                if id(cand) in _seen:
                    continue
                final.append(cand)
                _seen.add(id(cand))
        return final, made
    except Exception as e:
        logger.warning("episode rollup 降级为不聚合: %s: %s",
                       type(e).__name__, str(e)[:160])
        return final, 0


def _finalize_ranking(ranked_candidates: list, limit: int):
    """Stage 5: 最终排序与 Ignition 增益收敛。返回 (final, stage)。"""
    t0 = time.time()
    for item in ranked_candidates:
        if item.get("_ignited"):
            item["_hybrid_score"] = round(item.get("_hybrid_score", 0) * IGNITION_BOOST, 4)

    ranked_candidates.sort(key=lambda x: x.get("_hybrid_score", 0), reverse=True)
    # v21.2 M4：本模块向 scoring 要的是 limit*2 —— 真正的截断在这里，
    # 所以多样性选择也必须在这里再做一次；只在 scoring 里做一次，
    # 会被这里的「按分截断」把挑出来的异簇候选重新挤掉。
    from ducky.scoring import mmr_select
    final = mmr_select(ranked_candidates, limit)

    # 清理内部字段
    for item in final:
        item.pop("_decay", None)
        item.pop("_composite", None)

    # v21.2 M7：同 episode 多条命中聚合为轨迹摘要（默认关）。
    # 落榜候选按分降序交给它做回填 —— 折叠不该让返回条数变少。
    _chosen_ids = {id(x) for x in final}
    _spare = [x for x in ranked_candidates if id(x) not in _chosen_ids]
    final, _rollups = _apply_episode_rollup(final, limit, _spare)
    for item in final:            # 回填进来的条目同样要清内部字段
        item.pop("_decay", None)
        item.pop("_composite", None)

    stage = {"name": "final", "count": len(final), "from_ignition": sum(1 for f in final if f.get("_ignited")), "rollups": _rollups, "ms": int((time.time()-t0)*1000)}
    return final, stage


def funnel_search(memory, query: str, user_id: str, limit: int = 10,
                  enable_ignition: bool = True, bank_id: str = "default",
                  session_id: str = "") -> dict:
    """
    搜索记忆 + Recall Funnel trace + Ignition。

    返回 {results, trace: {stages, total_ms, final_count, has_ignition}}
    """
    start = time.time()
    stages = []

    # Stage 1: 候选池 — 扩大搜索
    candidates, stage = _fetch_candidate_pool(memory, query, user_id, bank_id, limit,
                                             session_id=session_id)
    stages.append(stage)

    if not candidates:
        return {"results": [], "trace": {"stages": stages, "total_ms": int((time.time()-start)*1000), "final_count": 0, "has_ignition": False}}

    # Stage 2: 🔥 Ignition — 高相似度记忆点火直达
    ignited, remaining, stage = _apply_ignition(query, candidates, enable_ignition)
    if stage is not None:
        stages.append(stage)

    if not remaining and not ignited:
        return {"results": [], "trace": {"stages": stages, "total_ms": int((time.time()-start)*1000), "final_count": 0, "has_ignition": len(ignited) > 0}}

    # Stage 3: 去重 — 相同 memory 文本去重，ignition 优先
    deduped_ignited, deduped_remaining, stage = _dedup_candidates(ignited, remaining)
    stages.append(stage)

    # Stage 4: 时间衰减 — 仅对非 Ignition 记忆降权
    t0 = time.time()

    candidate_ids = [item.get("id") for item in (deduped_remaining + deduped_ignited) if item.get("id")]
    superseded_ids = _load_superseded_ids(candidate_ids)

    filtered_remaining = _drop_superseded(deduped_remaining, superseded_ids)
    filtered_ignited = _drop_superseded(deduped_ignited, superseded_ids)

    # Stage 4: 统一 5 维打分与时效衰减（委托 scoring.py 单一真源）
    candidates_to_score = filtered_ignited + filtered_remaining
    _fuse_ignition_scores(candidates_to_score)

    ranked_candidates = score_and_rank_candidates(
        query=query,
        candidates=candidates_to_score,
        user_id=user_id,
        bank_id=bank_id,              # v20.2.4 F-15：此前断在这里
        limit=limit * 2,
        # v21.2 M2：回声抑制与 M4 MMR 都住在 scoring 单一真源里 —— 本模块
        # 与 RecallEngine 两条召回路都经过它，改一处两条路同时生效。
        session_id=session_id,
    )
    stages.append({"name": "unified_scoring", "count": len(ranked_candidates), "ms": int((time.time()-t0)*1000)})

    # Stage 5: 最终排序与 Ignition 增益收敛
    final, stage = _finalize_ranking(ranked_candidates, limit)
    stages.append(stage)

    total_ms = int((time.time() - start) * 1000)

    # ── EvolveMem: 记录搜索质量信号（异步安全）──
    try:
        _evolve_log_search(query, final, latency_ms=total_ms, gate_passed=True)
    except Exception as e:
        logger.debug(f"evolve search-quality log skip: {e}")

    return {
        "results": final,
        "trace": {
            "stages": stages,
            "total_ms": total_ms,
            "final_count": len(final),
            "has_ignition": len(ignited) > 0,
        }
    }
