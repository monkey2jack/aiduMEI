#!/usr/bin/env python3
"""
aiduMEM Memory Ignition — 记忆点火机制
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
J-space 启发：高相似度记忆直接"点火"，跳过衰减管道直达结果。

— 核心逻辑 —
候选池中的每条记忆计算 cos → 超过 IGNITION_THRESHOLD (0.85) 直接入选
— 区别于普通衰减 —
非 Ignition 记忆仍走标准 Funnel 管道
— 可审计 —
每条记忆标注 _ignited: true/false + _ignition_score
"""

import time, math, logging
from typing import Optional

from ducky.utils import tokenize, jaccard_sim, normalize_score

logger = logging.getLogger("aiduMEM.ignition")

# ── 配置 ──
IGNITION_THRESHOLD = 0.85    # cos 超过此值 → 点火
IGNITION_MAX = 8             # 点火通道容量上限（防止爆炸）
IGNITION_BOOST = 1.5         # 点火记忆排序加权系数


def ignition_filter(
    query: str,
    candidates: list[dict],
    threshold: float = IGNITION_THRESHOLD,
    max_ignited: int = IGNITION_MAX,
) -> dict:
    """
    从候选池中筛选"点火"记忆。

    返回 {
        ignited: [...],     # 点火成功的记忆（已标记 _ignited=True）
        remaining: [...],   # 未点火、继续管道处理的
        stats: {ignited_count, threshold, max}
    }
    """
    t0 = time.time()
    ignited = []
    remaining = []

    # ── 对每条候选计算 Jaccard 相似度 ──
    scored = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        text = item.get("memory", "")
        sim = jaccard_sim(query, text) if text else 0.0
        # 融合 mem0 的向量 score
        vector_score = normalize_score(item.get("score", 0))
        combined = 0.6 * sim + 0.4 * vector_score
        scored.append((combined, item))

    # ── 降序排列，选 top-N 点火 ──
    scored.sort(key=lambda x: x[0], reverse=True)

    for rank, (score, item) in enumerate(scored):
        if rank < max_ignited and score >= threshold:
            item["_ignited"] = True
            item["_ignition_score"] = round(score, 4)
            item["_ignition_rank"] = rank + 1
            ignited.append(item)
        else:
            if score > 0:
                item["_ignited"] = False
                item["_ignition_score"] = round(score, 4)
            remaining.append(item)

    elapsed = int((time.time() - t0) * 1000)
    stats = {
        "ignited_count": len(ignited),
        "remaining_count": len(remaining),
        "threshold": threshold,
        "max": max_ignited,
        "ms": elapsed,
    }

    if ignited:
        logger.info(f"🔥 Ignition: {len(ignited)} 条记忆点火 (threshold={threshold})")

    return {"ignited": ignited, "remaining": remaining, "stats": stats}


def ignition_boost_sort(ignited: list, remaining: list, limit: int) -> list:
    """
    最终排序：点火记忆加权优先 + 原有的相关性排序。

    点火记忆被推到前面（乘以 IGNITION_BOOST），
    之后的空位由 remaining 按原 score 填充。
    """
    # 点火记忆按 ignition_score * boost 排序
    ignited_sorted = sorted(
        ignited,
        key=lambda x: (x.get("_ignition_score", 0) or 0) * IGNITION_BOOST,
        reverse=True,
    )
    # 普通记忆按原 score 排序
    remaining_sorted = sorted(
        remaining,
        key=lambda x: x.get("score", 0) or 0,
        reverse=True,
    )

    result = ignited_sorted[:limit]
    # 点火记忆不足时填充普通记忆
    if len(result) < limit:
        needed = limit - len(result)
        result.extend(remaining_sorted[:needed])

    # 清理内部标记（保留给 trace 用）
    for item in result:
        item.pop("_ignition_rank", None)

    return result
