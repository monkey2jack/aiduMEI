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
import threading

from ducky.env_config import float_env
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from ducky.salience.core import get_batch_salience_records
from ducky.failure_ledger import feature_failed

logger = logging.getLogger("aiduMEM.scoring")

# 召回闸门遥测（线程本地）。**拦了多少必须让调用方看得见** —— 这个仓的家训是
# 腿断/降级都要如实下发（v20.1 WP-C）；一道悄悄过滤的闸门与静默失败同型。
_gate_telemetry = threading.local()


def reset_gate_telemetry() -> None:
    _gate_telemetry.data = {}


def last_gate_telemetry() -> dict:
    return dict(getattr(_gate_telemetry, "data", {}) or {})


def _set_gate_telemetry(**fields) -> None:
    data = getattr(_gate_telemetry, "data", None)
    if data is None:
        data = {}
        _gate_telemetry.data = data
    data.update(fields)


def _evidence_gate_on() -> bool:
    """证据闸门开关；只有显式写 0/false/off 才关。

    非法值按**开**处理并出声 —— 与铁律 13 同款：「设了个打错的值」不该
    悄悄变成「关掉了一道安全闸门」。
    """
    raw = (os.environ.get(_EVIDENCE_GATE_ENV) or "").strip().lower()
    if not raw:
        return True
    if raw in ("0", "false", "off", "no"):
        return False
    if raw in ("1", "true", "on", "yes"):
        return True
    logger.warning("%s=%r 无法识别，本次按**开启**处理（安全侧）", _EVIDENCE_GATE_ENV, raw[:20])
    return True


def _score_bucket(s: float) -> str:
    """把分数落进 0.1 宽的桶 —— `score_histogram` 是给下一版定阈值用的原料。"""
    b = max(0, min(9, int(float(s) * 10)))
    return f"{b/10:.1f}-{(b+1)/10:.1f}"

# 统一衰减率与映射参数（单一真相源，支持环境变量微调）
# v20.2.3（外审 M-2 同族）：非法值曾让本模块 import 即崩 —— 打分参数
# 写错一个字符，整条召回链跟着消失。回退默认 + 出声，见 ducky/env_config.py。
RECENCY_LAMBDA = float_env("AIDUMEM_RECENCY_LAMBDA", 0.05, minimum=0.0)
RERANK_WEIGHT = float_env("AIDUMEM_RERANK_WEIGHT", 0.4, minimum=0.0, maximum=1.0)
# v20.2.4（外审 F-20）：温度必须**严格大于零** —— 取 0 时 normalize_score 的
# math.exp(-s / T) 直接 ZeroDivisionError。exclusive_minimum 这个能力 v20.2.3
# 的 A-2 就加进 env_config 了，当时**没用在这一行**。
SIGMOIDAL_TEMPERATURE = float_env("AIDUMEM_SIGMOIDAL_TEMP", 10.0, exclusive_minimum=0.0)

# ── 召回闸门（Issue #5：弱命中条目可凑分填满结果集）────────────────────
#
# **这个仓里一共有三道分数相关的闸门，轴与层位各不相同，别把它们当重复实现。**
#
#   ① `AIDUMEM_RECALL_SCORE_FLOOR`（`hot/search.py:_score_floor`）
#      —— 注意前缀是 `AIDUMEM_`：那是为兼容既有部署**冻结**的老前缀。
#      本次新加的两个变量按现行规范用 `AIDUMEI_`（品牌守卫会盯着）。
#      —— 轴 = **向量分** `r["score"]`；层 = `/search` **响应层**；默认 0.0。
#      只覆盖 `/search` 这一条路。
#   ② `RECALL_MIN_HYBRID`（本文件，下面这一行）
#      —— 轴 = **复合总分** `_hybrid_score`（rerank 融合后的终态分）；
#      层 = **打分层**，两条调用链（engine / recall_funnel）与一切调用本函数的
#      路径都受益（含 MCP）。默认 0.0。
#   ③ `CHAIN_MIN_SCORE`（`pipeline/memory_broadcast.py`）与
#      `MIN_SCORE_TO_PROMOTE`（`evolve_mem.py`）—— 不在召回链上，别混。
#
# **默认取 0.0 不是偷懒，是这个仓 v20.1 就下过的裁决**（原话在
# `hot/search.py:_verdict_threshold` 的注释里）：本机没有足够的查询分布去定
# 一个生产阈值，拍脑袋常数会把真记忆判成「没有」。校准值属于部署配置决策，
# 要用生产侧的真实查询分布算分位数再设 —— 所以本次把**观测**做出来
# （见下面回写的 `score_histogram`），值等数据够了再开。
#
# 实测支撑（2026-08-29，跑本函数现算，非推导）：
#   · 零证据条目地板 0.2015；事实类查询 ×1.35 后 0.2720；
#     高信任高热度可到 0.4000，叠事实类 0.5400，再叠 funnel 的
#     `IGNITION_BOOST ×1.5` 到 **0.8100** —— **已越过「真相关」参照的 0.6065**。
#   · 所以**单靠总分门槛治不了这个病**：拦得住 0.81 的阈值必然连真结果一起杀。
#     承重的是下面那道**证据闸门**，总分门槛只是补充。
#   · 而 issue 建议的 0.3 会误杀合法弱召回：三 token 查询命中一个
#     （bm25=0.333）总分约 0.285，落在门槛下方。
RECALL_MIN_HYBRID = float_env("AIDUMEI_RECALL_MIN_HYBRID", 0.0,
                              minimum=0.0, maximum=1.0)

# 证据闸门：向量分与 BM25 分**双零**的候选一律出局。
# 关掉它需要显式设 `AIDUMEI_RECALL_EVIDENCE_GATE=0` —— 与总分门槛相反，
# 这一道**默认开**：零证据条目与查询之间不存在任何可解释的关联，
# 放它进结果集没有「可能是对的」这种情形，因此不存在误杀风险。
_EVIDENCE_GATE_ENV = "AIDUMEI_RECALL_EVIDENCE_GATE"

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


def finite_or(value: Any, default: float = 0.0) -> float:
    """外部数值的有限性闸门（v20.2.4 · 外审 F-20）。

    v20.2.3 的 A-1 在 env_config 拦住了**配置面**的 nan/inf，却漏了**数据面**：
    向量库、reranker、metadata 送进来的分数同样可能是 nan，而
    `not (v < 0 or v > 1)` 对 nan 恒真 —— 于是 nan 一路进 _hybrid_score，
    排序彻底失效而系统报健康。**同一个后果，两条入口，上一版只堵了一条。**
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def normalize_score(score: Any) -> float:
    """归一化分数到 [0.0, 1.0] 区间。非有限值（nan/inf）按 0 处理，不污染融合分。"""
    if score is None:
        return 0.0
    s = finite_or(score, 0.0)
    if s < 0:
        return 0.0
    if s > 1.0:
        # 对欧氏距离或未归一化大分值，采用 Sigmoidal 平滑压缩到 (0.5, 1.0]
        # 温度参数 SIGMOIDAL_TEMPERATURE=10.0 保证 s 在 [0, 50] 内具有良好梯度区分度
        return round(1.0 / (1.0 + math.exp(-s / SIGMOIDAL_TEMPERATURE)), 4)
    return round(s, 4)


def calc_token_overlap_score(query: str, text: str) -> float:
    """查询词元在文本中的**覆盖率**（0–1）。**这不是 BM25。**

    🔴v20.3.2-beta（外审）：本函数原名 `calc_bm25_score`，而它算的是
    「查询里有几成词元出现在文本里」—— **无 IDF、无 TF 饱和、无文档长度归一**，
    经典 Okapi BM25 的三个灵魂一个都没有。它在融合分里占 0.25 权重，
    对外文档还大张旗鼓叫「BM25 词频」，属于概念包装。

    诚实的名字就是 token overlap。要真 BM25 的话，底层 SQLite FTS5 有原生
    `bm25()` 可用（登记为 v20.4 候选，涉及打分口径变更，需重跑基准）。
    旧名保留为**兼容别名**（存量调用方），但不再声称自己是 BM25。
    """
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
    # v20.2.4（外审 F-15）：**此前连这个参数都没有**，于是类型账本查询用默认
    # scope，命名 bank 下一条都查不到 —— 六型加权与差异化衰减在那些部署上
    # 整体失效（实测偏好类记忆的时效分 1.0000 → 0.0111，被当 FACTS 打折 90 倍），
    # 而检索照常返回、没有任何告警。
    bank_id: str = "default",
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
        from ducky.memory_types import get_batch_memory_types, memory_type_ref
        # 类型账本的键与 salience 的键**不同源**：账本认 fact:{fact_id} 或 UUID，
        # salience 只认 UUID。共用一个列表就会让带 fact_id 的记忆查不到类型。
        type_refs = [r for r in (memory_type_ref(it) for it in candidates) if r]
        type_map = get_batch_memory_types(type_refs, user_id=user_id, bank_id=bank_id)
    except Exception as e:
        logger.debug(f"批量查询 memory_types 跳过: {e}")

    scored: List[dict] = []
    _gate_on = _evidence_gate_on()
    _evidence_filtered = 0
    for item in candidates:
        if not isinstance(item, dict):
            continue

        mid = str(item.get("id") or item.get("memory_id") or "")
        from ducky.memory_types import memory_type_ref as _mt_ref
        mtype = (item.get("memory_type")
                 or (item.get("metadata") or {}).get("memory_type")
                 or type_map.get(_mt_ref(item))
                 or "FACTS")

        # 六型过滤
        if memory_type_filter and memory_type_filter.upper() != "ALL":
            if mtype.upper() != memory_type_filter.upper():
                continue

        # 向量分
        vec_s = normalize_score(item.get("score", 0) or 0)

        # BM25 分
        content_text = str(item.get("memory") or item.get("content") or item.get("fact_value") or "")
        bm25_s = (item.get("metadata") or {}).get("bm25_score", 0) or calc_bm25_score(query, content_text)
        # v20.2.4（F-20）：外部数值一律过有限性闸门 —— min(nan, 1.0) 返回 nan
        bm25_s = min(finite_or(bm25_s, 0.0), 1.0)

        # 统一时效分
        created_ts = extract_timestamp(item)
        time_s = compute_time_decay(created_ts, now_ts, RECENCY_LAMBDA,
                                    memory_type=mtype if _type_decay_on else None)

        # 可靠性分
        reliability = (item.get("metadata") or {}).get("reliability", 0.5) or 0.5
        reliability_s = min(finite_or(reliability, 0.5), 1.0)

        # 访问热度分（批量缓存读取）
        sal_rec = salience_map.get(mid, {})
        access_count = (item.get("metadata") or {}).get("access_count") or sal_rec.get("access_count", 1)
        heat_s = min(finite_or(access_count, 1.0) / 100.0, 1.0)

        # ── 证据闸门（Issue #5 · 承重）────────────────────────────────
        # 向量分与 BM25 分**双零** = 这条候选与查询之间没有任何可解释的关联。
        # 它此前照样能靠时效 + 可靠性 + 热度凑分进结果集：实测零证据条目
        # 地板 0.2015，高信任高热度可到 0.4000，事实类查询再 ×1.35 到 0.5400，
        # funnel 的 ignition 再 ×1.5 到 0.8100 —— **越过了「真相关」参照的 0.6065**。
        #
        # 放在这里（打分循环内、rerank 之前）有两个好处：垃圾候选不进重排，
        # 省 token；两条调用链（engine / recall_funnel）同时受益，因为它们
        # 共用本函数这一个出口。
        #
        # **ignited 条目不会被误杀**：`recall_funnel.py:176` 把 `_ignition_score`
        # 并进了 `item["score"]`，走的就是向量分这个入口 —— 它有证据。
        # **向量腿断掉时也不误判**：那时所有候选 vec_s=0，还剩 BM25；两者都 0
        # 就是真的没有证据。返回空不会冒充腿断 —— v20.2.4 的 `vector_leg`
        # 三态遥测（ok/degraded/not_found）区分得开。
        if _gate_on and vec_s <= 0 and bm25_s <= 0:
            _evidence_filtered += 1
            continue

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
        # 全被闸门滤光也要如实回报 —— 否则「候选里一条有证据的都没有」
        # 与「压根没有候选」在响应里长得一模一样，正是本仓反复修过的
        # 「一个空列表说两件事」。
        _set_gate_telemetry(evidence_filtered=_evidence_filtered, score_filtered=0,
                            threshold=RECALL_MIN_HYBRID,
                            evidence_gate=_gate_on, score_histogram={})
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
                # v20.2.4（F-20）：reranker 送来非有限分时**丢弃这个信号**，
                # 保留融合分原值 —— 不是当 0 处理（那会把一条好候选压到底，
                # 等于让外部服务的一次抽风改变排序）。
                raw_rr = r.get("relevance_score", 0)
                rr_score = finite_or(raw_rr, float("nan"))
                if not math.isfinite(rr_score):
                    logger.debug("rerank 返回非有限分，跳过该条回写: idx=%s raw=%r", idx, raw_rr)
                    continue
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

    # 4. 排序、总分门槛、截断
    scored.sort(key=lambda x: x.get("_hybrid_score", 0), reverse=True)

    # 分数直方图：**这是下一版给 RECALL_MIN_HYBRID 定值的原料**。
    # 没有它，阈值只能继续拍脑袋 —— 而本仓已经为「拍脑袋常数」付过两次学费
    # （WAL 告警阈值 1MB、核心记忆 30 天）。先量，再卡。
    _hist: Dict[str, int] = {}
    for it in scored:
        _hist[_score_bucket(it.get("_hybrid_score", 0) or 0)] = \
            _hist.get(_score_bucket(it.get("_hybrid_score", 0) or 0), 0) + 1

    _score_filtered = 0
    if RECALL_MIN_HYBRID > 0:
        # 门槛必须卡在 **rerank 融合之后** —— `old*(1-W) + rr*W` 才是终态分，
        # 卡在融合前等于对一个中间量设限。
        #
        # **ignited 条目豁免**：`recall_funnel.py:194` 在本函数返回**之后**才乘
        # `IGNITION_BOOST = 1.5`，门槛在这里看到的是 boost 前的分。一条 boost 后
        # 能到 0.33 的 ignited 条目，会在 0.22 时被这道门槛杀掉 —— 那是把
        # 「显式的相关性信号」当成弱命中处理，方向正好反了。
        kept = [it for it in scored
                if it.get("_ignited")
                or (it.get("_hybrid_score", 0) or 0) >= RECALL_MIN_HYBRID]
        _score_filtered = len(scored) - len(kept)
        scored = kept

    # 拦了多少必须让调用方看得见（与 rerank 遥测同款纪律：回写自身失败
    # 只记 debug，绝不把主查询带崩）。
    try:
        _set_gate_telemetry(
            evidence_filtered=_evidence_filtered,
            score_filtered=_score_filtered,
            threshold=RECALL_MIN_HYBRID,
            evidence_gate=_gate_on,
            score_histogram=_hist,
        )
    except Exception as exc:
        logger.debug("召回闸门遥测回写失败，响应里看不到过滤条数: %s", exc)

    final = scored[:limit]

    return final


# 兼容别名（v20.3.2-beta）：旧名指向同一实现，避免打断存量调用方。
# **它不是 BM25** —— 见 calc_token_overlap_score 的 docstring。
calc_bm25_score = calc_token_overlap_score
