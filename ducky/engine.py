"""
ducky.engine — aiduMEM 统一召回与混合引擎 (v11.0.0 Hyperion · v19.0 P0-4 升级)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
将向量匹配、BM25 词频、Salience 热度、时效衰减与 Reranker 抽象为统一的高级召回引擎。

v19.0 P0-4 升级（检索升级 · Hindsight TEMPR 借鉴）：
    1. 时效衰减率可配置（AIDUMEM_RECENCY_LAMBDA，默认 0.01）
    2. 时间戳解析强化：created_at → metadata.recorded_at → metadata.valid_from 三级回退
    3. before/after 时间窗口过滤（Zep 双时态：回答「三个月前偏好什么」类问题）
    4. 时间近的结果在同等相关性下排名更靠前（指数衰减，永不归零）
"""

import os
import time
import math
import logging
from datetime import datetime, timezone
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
# 时间衰减率：环境变量可调（P0-4）。λ 越大，旧记忆衰减越快。
# 默认 0.01：30 天记忆 ≈ 0.74，180 天记忆 ≈ 0.16，永不归零。
def _env_lambda() -> float:
    try:
        return float(os.environ.get("AIDUMEM_RECENCY_LAMBDA", "0.01"))
    except (TypeError, ValueError):
        return 0.01

RECENCY_LAMBDA = _env_lambda()
RERANK_WEIGHT = 0.25    # Rerank 在综合分中的权重

# ── 时间戳解析（P0-4 强化：三级回退）────────────────────────────────────
def extract_timestamp(item: Dict[str, Any]) -> float:
    """从候选记忆中提取 Unix 时间戳。优先级：
    created_at → metadata.recorded_at → metadata.valid_from → metadata.created_at。
    全部失败返回 0.0（表示未知时间，不参与衰减）。
    """
    candidates = [
        item.get("created_at", ""),
        (item.get("metadata") or {}).get("recorded_at", ""),
        (item.get("metadata") or {}).get("valid_from", ""),
        (item.get("metadata") or {}).get("created_at", ""),
    ]
    for raw in candidates:
        if not raw:
            continue
        ts = parse_iso_timestamp(str(raw))
        if ts > 0:
            return ts
    return 0.0


def _date_prefix(raw: str) -> str:
    """取 ISO / SQLite 时间戳的日期前缀 YYYY-MM-DD（P0-4 时间窗口比较）。"""
    if not raw:
        return ""
    return str(raw).strip().replace("T", " ")[:10]


def _norm_bound(raw: str, is_before: bool) -> str:
    """把 before/after 入参归一化成可比较的日期前缀。

    与 facts_recall._parse_time_bound 语义保持一致：
      YYYY / YYYY-MM 会被补全为期首日（after）或期末日（before），
      保证与候选的 YYYY-MM-DD 前缀做字符串比较时结果正确。
    导入失败则退回 _date_prefix 原样前缀（降级，不阻断检索）。
    """
    if not raw:
        return ""
    try:
        from ducky.facts_recall import _parse_time_bound
        return _parse_time_bound(raw, is_before=is_before)
    except Exception:
        return _date_prefix(raw)


class RecallEngine:
    def __init__(self, memory_instance=None, default_weights: Optional[Dict[str, float]] = None):
        self.memory = memory_instance
        self.weights = {**DEFAULT_WEIGHTS, **(default_weights or {})}

    def search(
        self,
        query: str,
        user_id: str,
        limit: int = 10,
        weights: Optional[Dict[str, float]] = None,
        before: str = "",
        after: str = "",
    ) -> List[Dict[str, Any]]:
        """执行全流程式多信号混合召回 + 重排序。

        P0-4 新增：
            before: YYYY[-MM[-DD]] 过滤掉该时间点之后才产生/生效的记忆
            after:  YYYY[-MM[-DD]] 过滤掉该时间点之前就已过期/失效的记忆
        时间窗口在召回后做客户端过滤（mem0/Qdrant 不提供 SQL 级时间过滤），
        过滤失败静默降级，不阻断检索。
        """
        w = {**self.weights, **(weights or {})}
        now_ts = time.time()

        # 规范化 user_id，兼容历史数据（统一映射到 default）
        user_id = _normalize_user_id(user_id)
        logger.info(f"🔍 引擎召回: query='{query}' user_id='{user_id}' limit={limit} before={before!r} after={after!r}")

        # 1. 向量基础匹配
        # 注意：mem0 2.0.x 的 search 是 keyword-only top_k（不是 limit），
        # 传 limit 会被 **kwargs 静默吞掉，导致候选池恒为默认 20 条，
        # P0-4 时间窗口过滤就永远捞不到更早的记忆。这里显式用 top_k，
        # 对旧版只认 limit 的签名用 TypeError 回退兼容。
        candidates = []
        if self.memory:
            try:
                try:
                    raw = self.memory.search(query, filters={"user_id": user_id}, top_k=limit * 3)
                except TypeError:
                    raw = self.memory.search(query, filters={"user_id": user_id}, limit=limit * 3)
                candidates = raw.get("results", raw) if isinstance(raw, dict) else raw
                if not isinstance(candidates, list):
                    candidates = []
            except Exception as e:
                logger.warning(f"向量搜索降级: {e}")
                candidates = []

        if not candidates:
            return []

        # 1.5 时间窗口过滤（P0-4）：before/after 比较候选时间戳的日期前缀
        if before or after:
            b_prefix = _norm_bound(before, is_before=True)
            a_prefix = _norm_bound(after, is_before=False)
            kept = []
            for item in candidates:
                if not isinstance(item, dict):
                    continue
                # 与 extract_timestamp 同一套四级回退（含 metadata.created_at），
                # 保证过滤侧与打分侧用同一时间源，不会一边按时间衰减一边又漏过滤。
                ts = extract_timestamp(item)
                if ts > 0:
                    ts_raw = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                else:
                    ts_raw = ""
                prefix = _date_prefix(ts_raw)
                # 无时间戳的候选在时间过滤下保守保留（不误杀老数据）
                if not prefix:
                    kept.append(item)
                    continue
                if b_prefix and prefix > b_prefix:
                    continue  # 该记忆在 before 之后才产生
                if a_prefix and prefix < a_prefix:
                    continue  # 该记忆在 after 之前就已存在（不满足「此后仍有效」的粗过滤）
                kept.append(item)
            candidates = kept

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

            # 时效分（P0-4 强化：三级时间戳回退 + 可配置衰减率）
            # 未知时间戳（created_ts=0）→ 中性分 0.5，不再给满分，避免
            # 老数据/无时间戳记忆压过已知新鲜的记忆。
            created_ts = extract_timestamp(item)
            if created_ts > 0:
                age_days = max(0, (now_ts - created_ts) / 86400)
                time_s = math.exp(-RECENCY_LAMBDA * age_days)
            else:
                time_s = 0.5
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
            item["_time_decay"] = round(time_s, 4)
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
            item.pop("_time_decay", None)

        return final
