#!/usr/bin/env python3
"""
aiduMEM Hybrid Recall: 加权混合召回模块
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Aion Memory 设计哲学：
- 多信号加权融合：向量 + BM25 + 时效 + 可靠性 + 热度
- 权重可配置、可调试
- 任一信号不可用时自动降级
"""

import time, math, logging
from typing import Optional

from .memory_ignition import ignition_filter
from .utils import normalize_score, parse_iso_timestamp

logger = logging.getLogger("aiduMEM.hybrid")

DEFAULT_WEIGHTS = {
    "vector": 0.45,      # 语义向量相似度
    "bm25": 0.15,        # BM25 关键词匹配
    "time": 0.15,        # 时效性（越新越高）
    "reliability": 0.15, # 可靠性（来源可信度）
    "heat": 0.10,        # 热度（访问次数）
}
RECENCY_LAMBDA = 0.01   # 时间衰减率


def hybrid_search(memory, query: str, user_id: str, limit: int = 10,
                  weights: Optional[dict] = None,
                  before: str = "", after: str = "") -> list:
    """
    加权混合召回（委托给 ducky.engine.RecallEngine 引擎处理）

    P0-4：before/after 时间窗口透传给引擎做时间过滤。
    """
    from ducky.engine import RecallEngine
    engine = RecallEngine(memory_instance=memory)
    return engine.search(
        query=query, user_id=user_id, limit=limit, weights=weights,
        before=before, after=after,
    )