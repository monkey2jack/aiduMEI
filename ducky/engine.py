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
    calc_token_overlap_score,
    compute_time_decay,
    extract_timestamp,
    normalize_score,
    score_and_rank_candidates,
)

logger = logging.getLogger("aiduMEM.engine")

# ── 召回腿遥测（v20.1 WP-C）──────────────────────────────────────────
# 与 mem0_runtime 的 rerank 遥测同一模式：线程本地，路由层每请求 reset、
# 请求末尾读取。存在的理由只有一个：向量腿的 except 会把「嵌入服务挂了」
# 消化成空候选，没有这份遥测，「搜挂了」和「库里没有」在响应上无法区分。
_recall_telemetry = threading.local()


def reset_recall_telemetry() -> None:
    _recall_telemetry.data = {}


def last_recall_telemetry() -> dict:
    return dict(getattr(_recall_telemetry, "data", {}) or {})


def _set_recall_telemetry(**fields) -> None:
    data = getattr(_recall_telemetry, "data", None)
    if data is None:
        data = {}
        _recall_telemetry.data = data
    data.update(fields)


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
        bank_id: str = "default",
    ) -> List[dict]:
        """检索主逻辑。"""
        t0 = time.time()
        mem = self._get_mem()

        # 1. 向量初步候选召回（多取候选供加权和时效过滤）
        cand_limit = max(limit * 3, 30)
        _set_recall_telemetry(vector_leg="ok")
        # 🪫 v20.2 自动挡（WP-G/WP-F）：切换逻辑是这台双引擎的命门 ——
        # ① open 态（熔断中）向量腿直接走本地索引，云一根手指都不碰；
        # ② closed / half-open 试云腿（半开就是拿真实流量当探针 ——
        #    不试探，成功信号永远不来，系统会卡死在备胎挡）；
        # ③ 云腿当场炸掉时**同一次请求内**落到本地腿兜底 —— 换挡不是
        #    下一个用户才享受的事，这一次查询就无感顺滑。
        # 查询嵌入永远与所查索引同语言（云查云、本地查本地），绝不跨比。
        from ducky.gear import record_cloud_failure, record_cloud_success, should_try_cloud
        _tried_cloud = should_try_cloud()
        try:
            if not _tried_cloud:
                from ducky.dual_index import search_local
                raw_res = search_local(query, user_id, bank_id=bank_id, limit=cand_limit)
                _set_recall_telemetry(vector_leg="local")
            else:
                # 🔴v20：默认域**不能**把 bank_id 下推给 mem0 —— 存量向量 payload 里
                # 没这个字段，Qdrant 的 must 语义会把它们整批滤掉，且不报错只返回空。
                # 下推只用于命名域，默认域靠下面的 Python 复筛保证隔离。
                from ducky.bank_contract import vector_scope_filters
                try:
                    raw_res = mem.search(query, filters=vector_scope_filters(user_id, bank_id), limit=cand_limit)
                    record_cloud_success()
                except Exception as _cloud_exc:
                    record_cloud_failure(str(_cloud_exc))
                    logger.warning("云向量腿失败，本请求就地落本地腿: %s", str(_cloud_exc)[:120])
                    # 备胎自己也可能不在场（依赖未装/索引未建）——备胎再炸
                    # 就是干净的空手，绝不让 fallback 的异常反过来炸掉请求。
                    try:
                        from ducky.dual_index import search_local
                        raw_res = search_local(query, user_id, bank_id=bank_id, limit=cand_limit)
                    except Exception as _local_exc:
                        logger.warning("本地腿也不可用（备胎空手）: %s", str(_local_exc)[:120])
                        raw_res = []
                    # 云失败原因留在遥测里：判语层要用它区分「备胎接住了」
                    # 与「云断且备胎空手」（后者必须仍判 degraded）。
                    _set_recall_telemetry(vector_leg="local_fallback",
                                          error=str(_cloud_exc)[:120])
            if isinstance(raw_res, dict):
                candidates = raw_res.get("results", []) or []
            elif isinstance(raw_res, list):
                candidates = raw_res
            else:
                candidates = []
            from ducky.bank_contract import vector_item_in_bank as _viib
            candidates = [c for c in candidates if _viib(c, bank_id)]
        except Exception as e:
            logger.warning("向量召回异常降级: %s", e)
            # v20.2.1（外审 Y2）：云调用已被内层 try 精确包住并各自上报；
            # 走到这里的是复筛/装配等**非云腿**异常 —— 再记云失败会误触
            # 熔断降挡（三次复筛 bug 就把好端端的云挡切了）。不记。
            candidates = []
            # v20.1 WP-C：向量腿失效必须让调用方看得见。此前这个 except 把
            # 「嵌入服务挂了」消化成 candidates=[]，一路走完返回空列表 ——
            # 与「库里确实没有」在响应上逐字节相同。/search 拿这份遥测判
            # 三态：空结果 + 腿断 = degraded，绝不冒充 not_found。
            # 与 rerank 遥测同一模式：线程本地、每请求由路由层 reset。
            _set_recall_telemetry(vector_leg="failed", error=str(e)[:120])

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
            bank_id=bank_id,          # v20.2.4 F-15：此前断在这里
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
