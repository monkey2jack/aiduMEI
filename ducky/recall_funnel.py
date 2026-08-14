#!/usr/bin/env python3
"""
aiduMEM Recall Funnel: 搜索链路可观测模块
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Aletheia Memory 设计哲学：
- 候选池 → 🔥 Ignition（高相似度直达） → 去重 → 时间衰减 → 最终
- Ignition: J-space 启发——高 sim 记忆跳过衰减管道
- 每步决策可追溯、可调试
"""

import time
from ducky.scoring import score_and_rank_candidates, math, logging

from .utils import parse_iso_timestamp, get_salience_conn, get_facts_conn
from .salience.config import LANE_DECAY_MULTIPLIER
from .salience.core import _detect_lane
from .evolve_mem import log_search_quality as _evolve_log_search

logger = logging.getLogger("aiduMEM.funnel")

# ── 配置 ──
RECENCY_LAMBDA = 0.01    # 时间衰减率
MAX_CANDIDATE_MULT = 3   # 候选池倍数
IGNITION_THRESHOLD = 0.85
IGNITION_MAX = 8
IGNITION_BOOST = 1.5


def funnel_search(memory, query: str, user_id: str, limit: int = 10,
                  enable_ignition: bool = True) -> dict:
    """
    搜索记忆 + Recall Funnel trace + Ignition。

    返回 {results, trace: {stages, total_ms, final_count, has_ignition}}
    """
    start = time.time()
    stages = []
    results = []
    ignited = []
    remaining = []

    # Stage 1: 候选池 — 扩大搜索
    t0 = time.time()
    try:
        candidates_raw = memory.search(query, filters={"user_id": user_id}, limit=limit * MAX_CANDIDATE_MULT)
        # mem.search 在 BM25/混合召回内部失败时可能返回 None，必须安全降级。
        if candidates_raw is None:
            logger.warning("候选池: mem.search 返回 None，降级到 hybrid_search")
            try:
                from ducky.mem0_runtime import lazy_import_hybrid
                candidates_raw = lazy_import_hybrid()(memory, query, user_id, limit * MAX_CANDIDATE_MULT) or []
            except Exception as e:
                logger.warning(f"候选池: hybrid_search 降级也失败: {e}")
                candidates_raw = []
        candidates = candidates_raw.get("results", candidates_raw) if isinstance(candidates_raw, dict) else candidates_raw
        if not isinstance(candidates, list):
            candidates = []
    except Exception as e:
        logger.warning(f"候选池搜索失败: {e}")
        candidates = []
    stages.append({"name": "candidate_pool", "count": len(candidates), "ms": int((time.time()-t0)*1000)})

    if not candidates:
        return {"results": [], "trace": {"stages": stages, "total_ms": int((time.time()-start)*1000), "final_count": 0, "has_ignition": False}}

    # Stage 2: 🔥 Ignition — 高相似度记忆点火直达
    if enable_ignition:
        t0 = time.time()
        try:
            from .memory_ignition import ignition_filter
            ign_result = ignition_filter(query, candidates, threshold=IGNITION_THRESHOLD, max_ignited=IGNITION_MAX)
            ignited = ign_result["ignited"]
            remaining = ign_result["remaining"]
            stages.append({
                "name": "ignition",
                "ignited": len(ignited),
                "remaining": len(remaining),
                "threshold": IGNITION_THRESHOLD,
                "ms": ign_result["stats"]["ms"],
            })
        except ImportError:
            logger.debug("Ignition 模块不可用，跳过")
            remaining = candidates
    else:
        remaining = candidates

    if not remaining and not ignited:
        return {"results": [], "trace": {"stages": stages, "total_ms": int((time.time()-start)*1000), "final_count": 0, "has_ignition": len(ignited) > 0}}

    # Stage 3: 去重 — 相同 memory 文本去重，ignition 优先
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
    stages.append({
        "name": "dedup",
        "ignited": len(deduped_ignited),
        "remaining": len(deduped_remaining),
        "ms": int((time.time()-t0)*1000),
    })

    # Stage 4: 时间衰减 — 仅对非 Ignition 记忆降权
    t0 = time.time()
    now_ts = time.time()
    
    # Lethe v9.2.0: 批量获取 lane 映射和 memory_states 状态
    lane_map = {}
    superseded_ids = set()
    candidate_ids = [item.get("id") for item in (deduped_remaining + deduped_ignited) if item.get("id")]
    if candidate_ids:
        try:
            # 批量获取 lane
            conn = get_salience_conn()
            placeholders = ",".join("?" for _ in candidate_ids)
            rows = conn.execute(
                f"SELECT memory_id, lane FROM salience WHERE memory_id IN ({placeholders})",
                candidate_ids
            ).fetchall()
            lane_map = {row[0]: row[1] for row in rows}
            conn.close()
            
            # 批量获取被取代的状态 (from facts.db)
            conn_facts = get_facts_conn()
            states = conn_facts.execute(
                f"SELECT memory_id FROM memory_states WHERE memory_id IN ({placeholders}) AND state = 'superseded'",
                candidate_ids
            ).fetchall()
            superseded_ids = {row[0] for row in states}
            conn_facts.close()
        except Exception as e:
            logger.debug(f"从数据库获取 lane 映射或状态失败: {e}")

    # 过滤掉已被取代的记忆 (Lethe v9.2.0)
    filtered_remaining = []
    for item in deduped_remaining:
        if item.get("id") in superseded_ids:
            logger.info(f"Lethe 过滤已取代记忆: {item.get('id', '')[:8]} '{item.get('memory', '')[:20]}'")
            continue
        filtered_remaining.append(item)

    filtered_ignited = []
    for item in deduped_ignited:
        if item.get("id") in superseded_ids:
            logger.info(f"Lethe 过滤已取代记忆: {item.get('id', '')[:8]} '{item.get('memory', '')[:20]}'")
            continue
        filtered_ignited.append(item)

    # Stage 4: 统一 5 维打分与时效衰减（委托 scoring.py 单一真源）
    candidates_to_score = filtered_ignited + filtered_remaining
    for item in candidates_to_score:
        if item.get("_ignited"):
            # Ignition 特征融合进入 score 供 scoring 引擎归一化
            ign_score = item.get("_ignition_score", 0) or 0
            base_s = item.get("score", 0) or 0
            item["score"] = max(base_s, ign_score)
            item.setdefault("metadata", {})
            if isinstance(item["metadata"], dict):
                item["metadata"]["is_ignited"] = True

    ranked_candidates = score_and_rank_candidates(
        query=query,
        candidates=candidates_to_score,
        user_id=user_id,
        limit=limit * 2,
    )
    stages.append({"name": "unified_scoring", "count": len(ranked_candidates), "ms": int((time.time()-t0)*1000)})

    # Stage 5: 最终排序与 Ignition 增益收敛
    t0 = time.time()
    for item in ranked_candidates:
        if item.get("_ignited"):
            item["_hybrid_score"] = round(item.get("_hybrid_score", 0) * IGNITION_BOOST, 4)

    ranked_candidates.sort(key=lambda x: x.get("_hybrid_score", 0), reverse=True)
    final = ranked_candidates[:limit]

    # 清理内部字段
    for item in final:
        item.pop("_decay", None)
        item.pop("_composite", None)

    stages.append({"name": "final", "count": len(final), "from_ignition": sum(1 for f in final if f.get("_ignited")), "ms": int((time.time()-t0)*1000)})

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
