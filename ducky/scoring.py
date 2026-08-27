"""ducky.scoring — 统一记忆打分与重排序引擎（v19.2.0 重构）

收敛原本发散在 10 个文件中的打分逻辑，根治「双套 λ 漂移」与「检索 N+1 往返」：
1. 统一 5 维打分（向量相似度 + BM25 词频 + 统一时间衰减 + 可靠性 + 访问热度）
2. 六型分类深度加权（事实类查询智能优先加权 FACTS / PREFERENCES）
3. 批量查询 Salience（消除 N+1 数据库往返）
4. Reranker 统一调度与耗时/成功率透明探针
"""
from __future__ import annotations

import logging
import math
import os

from ducky.env_config import float_env
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from ducky.salience.core import get_batch_salience_records
from ducky.failure_ledger import feature_failed

logger = logging.getLogger("aiduMEM.scoring")

# 统一衰减率与映射参数（单一真相源，支持环境变量微调）
# v20.2.3（外审 M-2 同族）：非法值曾让本模块 import 即崩 —— 打分参数
# 写错一个字符，整条召回链跟着消失。回退默认 + 出声，见 ducky/env_config.py。
RECENCY_LAMBDA = float_env("AIDUMEM_RECENCY_LAMBDA", 0.05, minimum=0.0)
RERANK_WEIGHT = float_env("AIDUMEM_RERANK_WEIGHT", 0.4, minimum=0.0, maximum=1.0)
SIGMOIDAL_TEMPERATURE = float_env("AIDUMEM_SIGMOIDAL_TEMP", 10.0, minimum=0.0)

DEFAULT_WEIGHTS = {
    "vector": 0.35,
    "bm25": 0.25,
    "time": 0.15,
    "reliability": 0.10,
    "heat": 0.15,
}

_FACT_SEEKING_KEYWORDS = re.compile(
    r"生日|是谁|哪天|什么时候|喜欢|偏好|最爱|爱好|习惯|底线|规则|铁律|是什么|配置|账号|密码|何处|哪里|邮箱|电话|微信|身份|关系",
    re.IGNORECASE,
)


def normalize_score(score: Any) -> float:
    """归一化分数到 [0.0, 1.0] 区间。"""
    if score is None:
        return 0.0
    try:
        s = float(score)
    except (ValueError, TypeError):
        return 0.0
    if s < 0:
        return 0.0
    if s > 1.0:
        # 对欧氏距离或未归一化大分值，采用 Sigmoidal 平滑压缩到 (0.5, 1.0]
        # 温度参数 SIGMOIDAL_TEMPERATURE=10.0 保证 s 在 [0, 50] 内具有良好梯度区分度
        return round(1.0 / (1.0 + math.exp(-s / SIGMOIDAL_TEMPERATURE)), 4)
    return round(s, 4)


def calc_bm25_score(query: str, text: str) -> float:
    """轻量级快速词频与覆盖度打分。"""
    if not query or not text:
        return 0.0
    q_tokens = set(re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z0-9]+", query.lower()))
    if not q_tokens:
        return 0.0
    t_lower = text.lower()
    hits = sum(1 for tok in q_tokens if tok in t_lower)
    return round(hits / len(q_tokens), 4)


def extract_timestamp(item: dict) -> float:
    """三级时间戳提取（事实级 created_at -> metadata -> 兜底 0）。"""
    if not isinstance(item, dict):
        return 0.0
    for key in ("timestamp", "created_at", "recorded_at", "updated_at", "valid_from", "valid_to", "expires_at"):
        val = item.get(key)
        if isinstance(val, (int, float)) and val > 0:
            return float(val)
        if isinstance(val, str) and val.strip():
            try:
                from datetime import datetime
                # 处理 ISO 字符串
                dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                return dt.timestamp()
            except Exception as e:
                logger.debug(f"extract_timestamp: suppressed exception: {e}")
    md = item.get("metadata") or {}
    if isinstance(md, dict):
        for key in ("timestamp", "created_at", "recorded_at", "updated_at", "valid_from", "valid_to", "expires_at"):
            val = md.get(key)
            if isinstance(val, (int, float)) and val > 0:
                return float(val)
            if isinstance(val, str) and val.strip():
                try:
                    from datetime import datetime
                    dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                    return dt.timestamp()
                except Exception as e:
                    logger.debug(f"extract_timestamp: suppressed exception: {e}")
    return 0.0


def is_fact_seeking_query(query: str) -> bool:
    """判断查询是否偏向事实、偏好或特定规则。"""
    if not query:
        return False
    return bool(_FACT_SEEKING_KEYWORDS.search(query))


# ── 差异化时效衰减（v20.2.4 · WP-A，借鉴一个同源分支的分车道衰减思路）──
#
# 问题：此前全局一个 λ（RECENCY_LAMBDA），于是「用户叫什么名字」与「上周部署
# 报错时的心情」在时效上被一视同仁地打折 —— 只能二选一地错：要么身份被过度
# 打折，要么情绪被过度保鲜。
#
# 取舍：**借参数，不借表结构**。承载类型的账本 v19.0 就有（ducky/memory_types，
# 六类、带作用域列、带确定性降级），且 score_and_rank_candidates 已在循环外
# 批量查好（get_batch_memory_types，注释写着「彻底消除 N+1」）——所以本特性
# **零新表、零新查询、零额外往返**，只是把已经在手的类型用起来。
#
# ⚠️ 六个 λ 是**工程惯例值，不是实测值**，与召回阈值 0.46、熔断 N/M/T 同款
# 待生产分布校准。**绝不冒充「算出来的」。**
#
# FACTS 取 0.05 是刻意的：它等于今天的全局默认，而 mtype 缺失时上游回退
# "FACTS" —— 于是**未分类的存量记忆行为逐字不变**（门槛 5 由此天然成立）。
TYPE_DECAY: Dict[str, float] = {
    "PREFERENCES":  0.00,   # 偏好长期稳定，不该因久未提及被打折
    "DECISIONS":    0.02,   # 关键决策与约定，衰减极慢
    "FACTS":        0.05,   # ＝ 今天的全局默认，存量行为不变
    "REFLECTIONS":  0.08,   # 反思洞察，中速
    "EXPERIENCES":  0.16,   # 经历，较快
    "OBSERVATIONS": 0.28,   # 中性观察，最快
}
_TYPE_DECAY_ENV = "AIDUMEI_TYPE_DECAY"


def type_decay_enabled() -> bool:
    """按类型分档是否启用。**默认关** —— 新能力先诚实默认，拿到真实分布
    再定（老规矩）。关闭时打分路径逐字节回到本特性之前。"""
    return os.environ.get(_TYPE_DECAY_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def type_decay_lambda(memory_type: Optional[str]) -> float:
    """按记忆类型取衰减率；未知/缺失类型回退全局 RECENCY_LAMBDA。

    回退而不是报错是刻意的：类型账本对存量记忆覆盖不全（2026-08-27 生产
    实测覆盖率 29%），查不到必须安全降级，绝不跳过、绝不给 0 分。
    """
    if not memory_type:
        return RECENCY_LAMBDA
    return TYPE_DECAY.get(str(memory_type).upper(), RECENCY_LAMBDA)


def compute_time_decay(created_ts: float, now_ts: Optional[float] = None, recency_lambda: Optional[float] = None,
                       memory_type: Optional[str] = None) -> float:
    """统一计算时间衰减分数。"""
    if created_ts <= 0:
        return 0.5  # 未知时间给中性分
    now = now_ts or time.time()
    # v20.2.4：memory_type 给定时按类型取 λ；**不给时逐字回到旧行为**
    # （`or` 链保持原样 —— 传 0.0 的 λ 走 TYPE_DECAY 分支，不会被 or 吃掉）。
    if memory_type is not None:
        lam = type_decay_lambda(memory_type)
    else:
        lam = recency_lambda or RECENCY_LAMBDA
    age_days = max(0.0, (now - created_ts) / 86400.0)
    return round(math.exp(-lam * age_days), 4)


def score_and_rank_candidates(
    query: str,
    candidates: List[dict],
    *,
    user_id: str = "default",
    limit: int = 10,
    weights: Optional[Dict[str, float]] = None,
    memory_type_filter: Optional[str] = None,
) -> List[dict]:
    """统一候选记忆打分与排序入口。

    1. 批量查询 Salience，消除 N+1 数据库往返；
    2. 计算多维加权总分并应用六型优先加权；
    3. 调用 Reranker 重排序并输出透明探针日志。
    """
    if not candidates:
        return []

    w = weights or DEFAULT_WEIGHTS
    now_ts = time.time()
    is_fact_query = is_fact_seeking_query(query)

    # v20.2.4：开关在循环外读一次（每条候选读 env 是白烧）
    _type_decay_on = type_decay_enabled()

    # 1. 批量查询 Salience 记录（0 N+1）
    mem_ids = [str(it.get("id") or it.get("memory_id") or "") for it in candidates if it.get("id") or it.get("memory_id")]
    salience_map = get_batch_salience_records(mem_ids)

    # 2. 批量查询 Memory Types（单次 SQL 批量加载，彻底消除 N+1 数据库往返）
    type_map: Dict[str, str] = {}
    try:
        from ducky.memory_types import get_batch_memory_types
        type_map = get_batch_memory_types(mem_ids)
    except Exception as e:
        logger.debug(f"批量查询 memory_types 跳过: {e}")

    scored: List[dict] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue

        mid = str(item.get("id") or item.get("memory_id") or "")
        mtype = item.get("memory_type") or (item.get("metadata") or {}).get("memory_type") or type_map.get(mid) or "FACTS"

        # 六型过滤
        if memory_type_filter and memory_type_filter.upper() != "ALL":
            if mtype.upper() != memory_type_filter.upper():
                continue

        # 向量分
        vec_s = normalize_score(item.get("score", 0) or 0)

        # BM25 分
        content_text = str(item.get("memory") or item.get("content") or item.get("fact_value") or "")
        bm25_s = (item.get("metadata") or {}).get("bm25_score", 0) or calc_bm25_score(query, content_text)
        bm25_s = min(float(bm25_s), 1.0)

        # 统一时效分
        created_ts = extract_timestamp(item)
        time_s = compute_time_decay(created_ts, now_ts, RECENCY_LAMBDA,
                                    memory_type=mtype if _type_decay_on else None)

        # 可靠性分
        reliability = (item.get("metadata") or {}).get("reliability", 0.5) or 0.5
        reliability_s = min(float(reliability), 1.0)

        # 访问热度分（批量缓存读取）
        sal_rec = salience_map.get(mid, {})
        access_count = (item.get("metadata") or {}).get("access_count") or sal_rec.get("access_count", 1)
        heat_s = min(float(access_count or 1) / 100.0, 1.0)

        # 基础综合得分
        base_score = (
            w["vector"] * vec_s
            + w["bm25"] * bm25_s
            + w["time"] * time_s
            + w["reliability"] * reliability_s
            + w["heat"] * heat_s
        )

        # 六型加权增益：针对事实类查询，对 FACTS/PREFERENCES 给予 1.35x 增益
        if is_fact_query and mtype in ("FACTS", "PREFERENCES", "DECISIONS"):
            base_score *= 1.35

        item["_hybrid_score"] = round(base_score, 4)
        item["_time_decay"] = round(time_s, 4)
        item["memory_type"] = mtype
        scored.append(item)

    if not scored:
        return []

    # 3. Rerank 重排序
    t_rr_start = time.time()
    rerank_applied = False
    try:
        from ducky.mem0_runtime import rerank as do_rerank
        docs = [str(it.get("memory") or it.get("content") or it.get("fact_value") or "") for it in scored]
        rr = do_rerank(query, docs, top_n=min(len(docs), limit * 2))
        if rr:
            for r in rr:
                idx = r.get("index", -1)
                rr_score = r.get("relevance_score", 0) or 0
                if 0 <= idx < len(scored):
                    old = scored[idx].get("_hybrid_score", 0) or 0
                    scored[idx]["_hybrid_score"] = round(old * (1 - RERANK_WEIGHT) + rr_score * RERANK_WEIGHT, 4)
                    scored[idx]["_rerank_score"] = round(rr_score, 4)
            rerank_applied = True
            rr_elapsed = round((time.time() - t_rr_start) * 1000, 1)
            logger.debug("🎯 [Scoring] rerank ok: %d docs -> top %d in %sms", len(docs), len(rr), rr_elapsed)
    except Exception as e:
        feature_failed("rerank", e)
        logger.debug("Rerank 降级: %s", e)

    # v20 P0-4：rerank_applied 此前是丢在地上的局部变量，响应里永远看不到
    # 重排序到底生效没有。回写进线程本地遥测，由 /search 带回响应。
    try:
        from ducky.mem0_runtime import last_rerank_telemetry
        _telem = last_rerank_telemetry()
        if isinstance(_telem, dict):
            _telem["applied"] = rerank_applied
    except Exception as exc:
        # 本版加这段是为了修「rerank_applied 看不见」。要是回写自己也静默失败，
        # 修复等于没做，而响应里长期显示不出重排是否生效 —— 症状与修复前一致。
        # 遥测不该把主查询带崩，所以照旧不抛，但必须留一笔。
        logger.debug("rerank 遥测回写失败，响应里看不到 applied: %s", exc)

    # 4. 排序与截断
    scored.sort(key=lambda x: x.get("_hybrid_score", 0), reverse=True)
    final = scored[:limit]

    return final
