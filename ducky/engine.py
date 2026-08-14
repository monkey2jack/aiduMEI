"""ducky.engine — 记忆检索主引擎（v19.2.0 统一 Scoring 重构版）

五维联合召回 + 统一打分 + 批量查询（消除 N+1）+ 六型分类加权。
"""
from __future__ import annotations

import logging
import math
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

from ducky.scoring import (
    DEFAULT_WEIGHTS,
    RECENCY_LAMBDA,
    RERANK_WEIGHT,
    calc_bm25_score,
    compute_time_decay,
    extract_timestamp,
    normalize_score,
    score_and_rank_candidates,
)

logger = logging.getLogger("aiduMEM.engine")

# 统一时间衰减率
RECENCY_LAMBDA = float(os.environ.get("AIDUMEM_RECENCY_LAMBDA", "0.05"))
RERANK_WEIGHT = float(os.environ.get("AIDUMEM_RERANK_WEIGHT", "0.4"))


def _parse_time_boundary(val: Optional[str]) -> Optional[str]:
    """解析 before/after 时间边界为标准 ISO 前缀。"""
    if not val or not isinstance(val, str):
        return None
    v = val.strip()
    if not v:
        return None
    # YYYY
    if re.match(r"^\d{4}$", v):
        return v
    # YYYY-MM
    if re.match(r"^\d{4}-\d{2}$", v):
        return v
    # YYYY-MM-DD
    if re.match(r"^\d{4}-\d{2}-\d{2}", v):
        return v[:10]
    return v


def _date_prefix(val: Optional[str]) -> str:
    """提取 ISO 字符串的前缀（YYYY-MM-DD）。"""
    if not val or not isinstance(val, str):
        return ""
    v = val.strip()
    if len(v) >= 10 and re.match(r"^\d{4}-\d{2}-\d{2}", v):
        return v[:10]
    if len(v) >= 7 and re.match(r"^\d{4}-\d{2}", v):
        return v[:7]
    if len(v) >= 4 and re.match(r"^\d{4}", v):
        return v[:4]
    return ""


class RecallEngine:
    """5 维联合召回引擎"""

    def __init__(self, memory_instance=None):
        self._mem = memory_instance

    def _get_mem(self):
        if self._mem is not None:
            return self._mem
        from ducky.mem0_runtime import get_memory
        return get_memory()

    def search(
        self,
        query: str,
        user_id: str = "default",
        *,
        limit: int = 10,
        weights: Optional[Dict[str, float]] = None,
        before: Optional[str] = None,
        after: Optional[str] = None,
        memory_type: Optional[str] = None,
    ) -> List[dict]:
        """检索主逻辑。"""
        t0 = time.time()
        mem = self._get_mem()

        # 1. 向量初步候选召回（多取候选供加权和时效过滤）
        cand_limit = max(limit * 3, 30)
        try:
            raw_res = mem.search(query, filters={"user_id": user_id}, limit=cand_limit)
            if isinstance(raw_res, dict):
                candidates = raw_res.get("results", []) or []
            elif isinstance(raw_res, list):
                candidates = raw_res
            else:
                candidates = []
        except Exception as e:
            logger.warning("向量召回异常降级: %s", e)
            candidates = []

        # 时间窗口粗过滤（before/after）
        b_prefix = _parse_time_boundary(before)
        a_prefix = _parse_time_boundary(after)
        if b_prefix or a_prefix:
            kept = []
            for item in candidates:
                if not isinstance(item, dict):
                    continue
                ts_raw = item.get("created_at") or (item.get("metadata") or {}).get("recorded_at") or ""
                prefix = _date_prefix(str(ts_raw))
                if not prefix:
                    kept.append(item)
                    continue
                if b_prefix and prefix > b_prefix:
                    continue
                if a_prefix and prefix < a_prefix:
                    continue
                kept.append(item)
            candidates = kept

        # 2. 统一打分、六型偏好加权、批量查询 Salience（0 N+1）、Rerank 重排序
        final = score_and_rank_candidates(
            query,
            candidates,
            user_id=user_id,
            limit=limit,
            weights=weights or DEFAULT_WEIGHTS,
            memory_type_filter=memory_type,
        )

        elapsed = round((time.time() - t0) * 1000, 1)
        logger.debug("🔎 [Engine] 召回完成 query='%s' returned=%d elapsed=%sms", query[:30], len(final), elapsed)
        return final


_engine_singleton: Optional[RecallEngine] = None
_engine_lock = threading.Lock()


def get_recall_engine() -> RecallEngine:
    global _engine_singleton
    if _engine_singleton is None:
        with _engine_lock:
            if _engine_singleton is None:
                _engine_singleton = RecallEngine()
    return _engine_singleton
