#!/usr/bin/env python3
"""
aiduMEM Memory J-lens — 记忆可审计性增强
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Anthropic J-lens / J-space 启发：
- 可审计：追踪模型内部思考步骤
- Ignition 标记 + 语义距离矩阵 + 容量可视化

— 功能 ─
1. 搜索链路的完整 trace（Ignition/非Ignition 分列）
2. 最终结果之间的语义距离矩阵
3. Workspace 状态快照（容量、活跃度）
4. collect_jlens_report() 一键收集完整审计报告
"""

import time, logging, math
from typing import Optional

from ducky.utils import quick_sim

logger = logging.getLogger("aiduMEM.jlens")


def collect_jlens_report(
    query: str,
    ignited: list,
    remaining: list,
    final: list,
    workspace_status: dict = None,
    total_ms: int = 0,
) -> dict:
    """
    收集完整的 J-lens 审计报告。

    返回 J-space 风格的 trace：
    {
        query, total_ms,
        ignition: {count, threshold, items},
        pipeline: {candidates, dedup, decay, final},
        workspace: {total, capacity, hits},
        distance_matrix: [[...], ...],
        summary: "一句话总结"
    }
    """
    report = {
        "query": query,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "total_ms": total_ms,
    }

    # ── Ignition 审计 ──
    ignition_items = []
    for item in ignited:
        ignition_items.append({
            "id": (item.get("id", ""))[:24],
            "text": (item.get("memory", ""))[:80],
            "ignition_score": item.get("_ignition_score", 0),
            "ignition_rank": item.get("_ignition_rank", 0),
            "score": item.get("score", 0),
        })
    report["ignition"] = {
        "count": len(ignited),
        "threshold": 0.85,
        "items": ignition_items,
    }

    # ── 管道审计 ──
    pipeline_items = []
    for item in remaining:
        pipeline_items.append({
            "id": (item.get("id", ""))[:24],
            "text": (item.get("memory", ""))[:80],
            "score": item.get("score", 0),
            "ignition_score": item.get("_ignition_score", 0),
        })
    report["pipeline"] = {
        "candidates": len(ignited) + len(remaining),
        "remaining": len(remaining),
        "items": pipeline_items[:10],
    }

    # ── 最终结果按来源分类 ──
    report["final"] = {
        "count": len(final),
        "from_ignition": sum(1 for f in final if f.get("_ignited")),
        "from_pipeline": sum(1 for f in final if not f.get("_ignited")),
        "items": [
            {
                "id": (f.get("id", ""))[:24],
                "text": (f.get("memory", ""))[:80],
                "ignited": f.get("_ignited", False),
                "score": f.get("score", 0),
            }
            for f in final[:10]
        ],
    }

    # ── Workspace 快照 ──
    if workspace_status:
        report["workspace"] = workspace_status
    else:
        report["workspace"] = {"total": 0, "capacity": 20}

    # ── 语义距离矩阵（最终结果之间的 pairwise cos）──
    report["distance_matrix"] = _compute_distance_matrix(final)

    # ── 一句话总结 ──
    ignited_pct = len(ignited) / max(len(ignited) + len(remaining), 1) * 100
    report["summary"] = (
        f"查询「{query[:30]}」→ {len(final)} 条结果, "
        f"Ignition {len(ignited)} 条 ({ignited_pct:.0f}%), "
        f"{total_ms}ms"
    )

    return report


def enhance_funnel_trace(funnel_result: dict, ignited: list) -> dict:
    """
    增强 Funnel trace：在 stages 中插入 ignition 阶段。
    """
    if not funnel_result:
        return {"results": [], "trace": {"stages": [], "total_ms": 0, "final_count": 0}}

    trace = funnel_result.get("trace", {})
    stages = trace.get("stages", [])

    # 在 candidate_pool 之后插入 ignition 阶段
    new_stages = []
    inserted = False
    for s in stages:
        new_stages.append(s)
        if s.get("name") == "candidate_pool" and not inserted:
            new_stages.append({
                "name": "ignition",
                "ignited": len(ignited),
                "remaining": s.get("count", 0) - len(ignited),
                "threshold": 0.85,
            })
            inserted = True

    trace["stages"] = new_stages
    trace["has_ignition"] = len(ignited) > 0

    return funnel_result


def _compute_distance_matrix(items: list) -> list:
    """计算最终结果之间的 pairwise Jaccard 距离"""
    n = min(len(items), 8)  # 最多 8x8
    texts = [(item.get("memory", ""))[:200] for item in items[:n]]
    matrix = []
    for i, ta in enumerate(texts):
        row = []
        for j, tb in enumerate(texts):
            if i == j:
                row.append(1.0)
            else:
                row.append(round(quick_sim(ta, tb), 3))
        matrix.append(row)
    return matrix
