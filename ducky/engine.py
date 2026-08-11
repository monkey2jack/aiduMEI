"""
ducky.engine — aiduMEM 统一召回与混合引擎 (v11.0.0 Hyperion)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
将向量匹配、BM25 词频、Salience 热度、时效衰减与 Reranker 抽象为统一的高级召回引擎。
"""

import time
import math
import logging
from typing import Optional, List, Dict, Any

from ducky.utils import normalize_score, parse_iso_timestamp
from ducky.salience.core import get_salience_record
from ducky.text_fts import calc_bm25_score
from ducky.mem0_runtime import _normalize_user_id

logger = logging.getLogger("aiduMEM.engine")

DEFAULT_WEIGHTS = {
    "vector": 0.45,      # 语义向量相似度
    "bm25": 0.15,        # BM25 关键词匹配
    "time": 0.15,        # 时效性（越新越高）
    "reliability": 0.15, # 可靠性（来源可信度）
    "heat": 0.10,        # 热度（访问次数）
}
RECENCY_LAMBDA = 0.01   # 时间衰减率
RERANK_WEIGHT = 0.25    # Rerank 在综合分中的权重


class RecallEngine:
    def __init__(self, memory_instance=None, default_weights: Optional[Dict[str, float]] = None):
        self.memory = memory_instance
        self.weights = {**DEFAULT_WEIGHTS, **(default_weights or {})}

    def search(self, query: str, user_id: str, limit: int = 10, weights: Optional[Dict[str, float]] = None) -> List[Dict[str, Any]]:
        """执行全流程式多信号混合召回 + 重排序"""
        w = {**self.weights, **(weights or {})}
        now_ts = time.time()

        # 规范化 user_id，兼容历史数据（统一映射到 default）
        user_id = _normalize_user_id(user_id)
        logger.info(f"🔍 引擎召回: query='{query}' user_id='{user_id}' limit={limit}")

        # 1. 向量基础匹配
        candidates = []
        if self.memory:
            try:
                raw = self.memory.search(query, filters={"user_id": user_id}, limit=limit * 3)
                candidates = raw.get("results", raw) if isinstance(raw, dict) else raw
                if not isinstance(candidates, list):
                    candidates = []
            except Exception as e:
                logger.warning(f"向量搜索降级: {e}")
                candidates = []

        if not candidates:
            return []

        # 2. 算分加权
        scored = []
        for item in candidates:
            if not isinstance(item, dict):
                continue
            score = 0.0

            # 向量分
            vec_s = normalize_score(item.get("score", 0) or 0)
            score += w["vector"] * vec_s

            # BM25 分
            content_text = item.get("memory", "") or item.get("content", "")
            bm25_s = (item.get("metadata") or {}).get("bm25_score", 0) or calc_bm25_score(query, content_text)
            score += w["bm25"] * min(bm25_s, 1.0)

            # 时效分
            created = item.get("created_at", "")
            age_days = 0
            if created:
                try:
                    created_ts = parse_iso_timestamp(created)
                    age_days = max(0, (now_ts - created_ts) / 86400)
                except Exception:
                    pass
            time_s = math.exp(-RECENCY_LAMBDA * age_days)
            score += w["time"] * time_s

            # 可靠性分
            reliability = (item.get("metadata") or {}).get("reliability", 0.5) or 0.5
            score += w["reliability"] * min(reliability, 1.0)

            # 热度分
            mem_id = item.get("id", "")
            access_count = (item.get("metadata") or {}).get("access_count", 0)
            if not access_count and mem_id:
                try:
                    rec = get_salience_record(mem_id)
                    access_count = rec.get("access_count", 1) if isinstance(rec, dict) else 1
                except Exception:
                    access_count = 1
            heat_s = min((access_count or 1) / 100, 1.0)
            score += w["heat"] * heat_s

            item["_hybrid_score"] = round(score, 4)
            scored.append(item)

        # 3. Rerank 重排序
        if scored:
            try:
                from ducky.mem0_runtime import rerank as do_rerank
                docs = [it.get("memory", "") for it in scored]
                rr = do_rerank(query, docs, top_n=min(len(docs), limit * 2))
                if rr:
                    for r in rr:
                        idx = r.get("index", -1)
                        rr_score = r.get("relevance_score", 0) or 0
                        if 0 <= idx < len(scored):
                            old = scored[idx].get("_hybrid_score", 0) or 0
                            scored[idx]["_hybrid_score"] = round(old * (1 - RERANK_WEIGHT) + rr_score * RERANK_WEIGHT, 4)
                            scored[idx]["_rerank_score"] = round(rr_score, 4)
            except Exception as e:
                logger.debug(f"Rerank 降级: {e}")

        # 4. 排序与截断
        scored.sort(key=lambda x: x["_hybrid_score"], reverse=True)
        final = scored[:limit]

        for item in final:
            item.pop("_hybrid_score", None)

        return final
