"""ducky.engine — 记忆检索主引擎（v19.2.0 统一 Scoring 重构版）

五维联合召回 + 统一打分 + 批量查询（消除 N+1）+ 六型分类加权。
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Dict, List, Optional

from ducky.scoring import (
    DEFAULT_WEIGHTS,
    RECENCY_LAMBDA,  # noqa: F401 —— 兼容 re-export：tests/hot.search 仍从 ducky.engine 取
    RERANK_WEIGHT,  # noqa: F401 —— 兼容 re-export：tests/test_p0_upgrades.py 按旧路径 monkeypatch engine_mod.RERANK_WEIGHT
    extract_timestamp,  # noqa: F401 —— 兼容 re-export：tests/hot.search 按旧路径 from ducky.engine import
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


# f0.5.1 冷启动速断：云腿「未证明健康」阶段的试探预算（秒）。
# 默认 2.0s——本地腿实测 p50 约 0.35s，2s 足够本地腿兜底后仍留在宿主
# 6s 预算内；云腿常态成功时本预算完全不参与（走 proven 快路）。
_CLOUD_PROBE_BUDGET_ENV = "AIDUMEI_CLOUD_PROBE_BUDGET_SEC"
_CLOUD_PROBE_BUDGET_DEFAULT = 2.0

# f0.5.1：进程级共享探针执行器（单线程、daemon 线程）。超时探测后被
# 垂直丢弃不 join——迟到的探针结果经 add_done_callback 记账；若探针
# 真在跑（比如慢网络最终响应了），下一个请求受益。lazy 单例、退出随进程。
_probe_executor = None
_probe_executor_lock = threading.Lock()


def _shared_probe_executor():
    global _probe_executor
    with _probe_executor_lock:
        if _probe_executor is None:
            import concurrent.futures as _cf
            _probe_executor = _cf.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="aidumei-cloud-probe")
        return _probe_executor


def _reset_probe_executor_for_tests(close_wait_sec: float = 3.0) -> None:
    """测试隔离：丢弃共享探针执行器，旧线程限时自然归位（不硬 join）。
    漫长的假探针（慢云腿替身）占着唯一线程会让后续用例的探针排队超
    预算——测试残状态必须清账，不然红的是无辜的邻接用例。"""
    global _probe_executor
    with _probe_executor_lock:
        ex, _probe_executor = _probe_executor, None
    if ex is None:
        return
    ex.shutdown(wait=False)
    deadline = time.time() + close_wait_sec
    while time.time() < deadline:
        alive = any("aidumei-cloud-probe" in (t.name or "")
                    for t in threading.enumerate() if t.is_alive())
        if not alive:
            break
        time.sleep(0.1)


def _cloud_probe_budget_sec() -> float:
    try:
        raw = os.environ.get(_CLOUD_PROBE_BUDGET_ENV, "").strip()
        if not raw:
            return _CLOUD_PROBE_BUDGET_DEFAULT
        val = float(raw)
        import math
        if math.isnan(val) or math.isinf(val) or val <= 0:
            raise ValueError(raw)
        return val
    except (TypeError, ValueError):
        # 复用模块顶部既有 logger（aiduMEM.engine 只许注册一次——
        # 生产采集按前缀过滤，注册处数是机器契约，tests/test_v20_brand_policy.py）
        logger.warning(
            "⚙️ %s 非法值 %r，已回退默认 %ss",
            _CLOUD_PROBE_BUDGET_ENV, raw, _CLOUD_PROBE_BUDGET_DEFAULT)
        return _CLOUD_PROBE_BUDGET_DEFAULT


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
        session_id: str = "",   # v21.2 M2：回声抑制作用域，空 = 不过滤
    ) -> List[dict]:
        """检索主逻辑。"""
        t0 = time.time()
        mem = self._get_mem()

        from ducky.query_aliases import resolve_query_aliases
        original_query = query
        query = resolve_query_aliases(query, user_id, bank_id)

        # 1. 向量初步候选召回（多取候选供加权和时效过滤）
        cand_limit = max(limit * 3, 30)
        _set_recall_telemetry(vector_leg="ok")
        if query != original_query:
            _set_recall_telemetry(alias_resolved=True)
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
                    from ducky.search_candidates import search_candidates

                    def _cloud_probe():
                        return search_candidates(
                            mem, query, filters=vector_scope_filters(user_id, bank_id),
                            limit=cand_limit)

                    # f0.5.1 冷启动速断：本进程云腿尚未成功过一次时，试探只给
                    # 短预算（COLD_BUDGET 秒）——预算内没回来按失败记账并就地落
                    # 本地腿，不再陪 mem0/网络的默认超时（可达十几秒）干等，宿主
                    # 6s 预算早已放弃，用户看到的就是「重启后前两轮失忆」。
                    # 预算只作用于「未证明健康」阶段；云腿成功一次后走原路径
                    # （零包装零开销）。探针跑在**进程级共享**的单线程执行器上：
                    # 超时后 shutdown(wait=False) 立即返回，迟到的探针结果由
                    # add_done_callback 记账（成功→record_cloud_success，下一
                    # 次请求受益；失败→record_failure，与超时账并合无冲突）。
                    from ducky.gear import cloud_unproven
                    if cloud_unproven():
                        import concurrent.futures as _cf
                        _budget = _cloud_probe_budget_sec()
                        _ex = _shared_probe_executor()
                        _fut = _ex.submit(_cloud_probe)
                        try:
                            raw_res = _fut.result(timeout=_budget)
                            record_cloud_success()
                        except _cf.TimeoutError:
                            # f0.6 W-03（openchamber 审计 P1-1）双修：
                            # ①记账去重——此前超时分支双路径记账（回调迟到
                            #   失败一次 + raise 进外层 except 一次），一次
                            #   超时 consec_failures +2，契约「3 连败熔断」
                            #   2 个请求就触发。现在超时只由外层 except 记
                            #   一次账，回调只处理「迟到成功」清零。
                            # ②排队误判——单线程执行器上，前一个探针还挂着
                            #   时后续探针只进队列从未启动（fut.running() 为
                            #   False），它没产生任何关于云腿健康的事实，不
                            #   记失败、只落本地腿。
                            if not _fut.running() and not _fut.done():
                                logger.warning(
                                    "冷启动探针排队未启动（执行器忙），本请求落本地腿不记云失败")
                                _set_recall_telemetry(
                                    vector_leg="local_fallback",
                                    error="probe queued (executor busy)")
                                raw_res = None
                                _skip_local_fallback = False
                            else:
                                _fut.add_done_callback(
                                    lambda f: (
                                        record_cloud_success()
                                        if f.done() and f.exception() is None
                                        else None
                                    ) if f.done() else None)
                                raise TimeoutError(
                                    f"cloud probe exceeded cold-start budget {_budget}s")
                    else:
                        raw_res = _cloud_probe()
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
            # f0.6.1（联调实测缺陷）：auto 挡云腿成功路径**不合并本地腿候选**——
            # /add 蒸馏线的原文只落 mem0_local（f0.1 verbatim 双保险设计），云集合
            # mem0 只有蒸馏后的事实；云挡只查云 → 蒸馏线原文在语义改写查询下
            # not_found（联调实测：「本周五下午三点…复盘」查「复盘会议几点」空手，
            # /add/raw 双写路径同型查询 found）。修法：云腿走通后把本地腿候选**并
            # 入候选池**再统一打分——查询嵌入各自同语言（云查云、本地查本地），
            # 不跨比向量，只在打分层竞争。本地腿挂了/关了就跳过（云挡部署形态
            # _local_off() 为真时零开销）。候选去重按 id，云命中优先保留。
            from ducky.engine_mode import local_leg_enabled
            if local_leg_enabled():
                try:
                    from ducky.dual_index import search_local as _search_local
                    _seen_ids = {str(c.get("id")) for c in candidates if isinstance(c, dict)}
                    _local_extra = _search_local(query, user_id, bank_id=bank_id,
                                                 limit=cand_limit)
                    for _c in _local_extra:
                        if isinstance(_c, dict) and str(_c.get("id")) not in _seen_ids:
                            candidates.append(_c)
                            _seen_ids.add(str(_c.get("id")))
                    if _local_extra:
                        _set_recall_telemetry(local_leg_merged=len(_local_extra))
                except Exception as _local_merge_exc:
                    # 本地合并是增量补充，绝不让它反过来炸掉已到手的云候选
                    logger.debug("本地腿合并跳过: %s", str(_local_merge_exc)[:120])
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
                # f0.6 W-20（hark P1-4）：时间窗改按**事件时间**优先。旧实现
                # created_at（入库时间）优先——导入历史聊天/补录旧事/跑
                # LoCoMo 时窗口全错，与「事件时间与入库时间分开」的 README
                # 承诺相反。现在 recorded_at（事件时间，f0.1 verbatim 修复
                # 引入的仓库约定）优先，缺失回落 created_at，并在条目标
                # time_basis 供调用方/遥测判读。
                _md = item.get("metadata") or {}
                # f0.6.1：event_at（显式事件时刻）最高优先，其次 recorded_at，最后 created_at
                ts_raw = (_md.get("event_at") or item.get("event_at")
                          or _md.get("recorded_at") or item.get("recorded_at")
                          or item.get("created_at") or "")
                prefix = _date_prefix(str(ts_raw))
                if not prefix:
                    kept.append(item)
                    item["time_basis"] = "unparsed"
                    continue
                _has_event = _md.get("event_at") or item.get("event_at")
                _has_rec = _md.get("recorded_at") or item.get("recorded_at")
                item["time_basis"] = ("event_explicit" if _has_event
                                      else "event" if _has_rec else "ingested_fallback")
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
            session_id=session_id,     # v21.2 M2
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
