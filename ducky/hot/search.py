"""ducky.hot.search — /search /search_trace"""
from __future__ import annotations

import logging
import os

from fastapi import FastAPI, HTTPException

from ducky.api_models import SearchRequest, SearchResponse
from ducky.mem0_runtime import (
    _normalize_user_id,
    boost_salience_for_results,
    get_memory,
    lazy_import_funnel,
    lazy_import_hybrid,
)
from ducky.bank_contract import (
    ensure_bank_registered,
    make_scope,
    vector_item_in_bank,
    vector_scope_filters,
)

logger = logging.getLogger("aiduMEM.hot")


def _annotate_memory_types(results: list) -> None:
    """把六型分类结果回填到检索结果（P2-3 / v19.2.0：单次 SQL 批量加载，消除 N+1 读查询）。

    - 只读账本，单次 SQL 批量加载，不触发任何 LLM 调用，检索性能不受影响；
    - 每条结果写入 memory_type 字段；账本无记录时默认 FACTS。
    - ref 命中优先级：mem0 UUID（主链写时）→ fact:{fact_id}（backfill 写时）。
    - 失败静默降级（检索优先，分类失败不阻断召回）。
    """
    if not results:
        return
    try:
        from ducky.memory_types import get_batch_memory_types

        ref_list = []
        item_ref_map = []
        for item in results:
            if not isinstance(item, dict):
                continue
            meta = item.get("metadata") or {}
            ref = ""
            if isinstance(meta, dict) and meta.get("fact_id") is not None:
                ref = f"fact:{meta['fact_id']}"
            if not ref:
                ref = item.get("id") or item.get("memory_id") or ""
            if ref:
                ref_str = str(ref)
                ref_list.append(ref_str)
                item_ref_map.append((item, ref_str))
            else:
                item["memory_type"] = "FACTS"

        if ref_list:
            type_map = get_batch_memory_types(ref_list)
            for item, ref_str in item_ref_map:
                item["memory_type"] = type_map.get(ref_str, "FACTS")
    except Exception:
        return


def _apply_time_window_to_trace(result: dict, before: str, after: str) -> dict:
    """对 funnel trace 的 results 做 P0-4 时间窗口客户端过滤。

    funnel 返回结构为 {status, trace, results, ...}；这里复用 engine
    的时间归一化逻辑，失败则原样返回（降级不阻断检索）。
    """
    try:
        results = result.get("results") or []
        if not isinstance(results, list):
            return result
        _filter_results_by_time(results, before, after)
        result["results"] = results
        if "trace" in result and isinstance(result["trace"], dict):
            result["trace"]["final_count"] = len(results)
        return result
    except Exception:
        return result


def _filter_results_by_time(results: list, before: str, after: str) -> None:
    """原地过滤 results，剔除不在 before/after 窗口内的候选（P0-4）。

    与 engine.RecallEngine.search 的窗口过滤同一套四级时间戳回退语义。
    """
    try:
        from ducky.engine import extract_timestamp

        if not before and not after:
            return
        b_prefix = None
        a_prefix = None
        try:
            from ducky.engine import _parse_time_boundary
            b_prefix = _parse_time_boundary(before)
            a_prefix = _parse_time_boundary(after)
        except Exception:
            from ducky.engine import _date_prefix
            b_prefix = _date_prefix(before)
            a_prefix = _date_prefix(after)

        kept = []
        for item in results:
            if not isinstance(item, dict):
                continue
            ts = extract_timestamp(item)
            prefix = ""
            if ts > 0:
                from datetime import datetime, timezone
                prefix = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            if not prefix:
                kept.append(item)  # 无时间戳保守保留
                continue
            if b_prefix and prefix > b_prefix:
                continue
            if a_prefix and prefix < a_prefix:
                continue
            kept.append(item)
        results[:] = kept
    except Exception:
        return


# ── 召回强度标注（v20 P0-6 同案）────────────────────────────────────────────
#
# 生产实测的分数分布（生产实例，8 个探针）：
#     真问题  top = 0.558 / 0.560 / 0.662 / 0.651
#     纯噪声  top = 0.457 / 0.423 / 0.466
# 两组不重叠，也就是说**嵌入是有区分力的**。（第一次测时我拿「zzz9x9x9x 不存在的
# 话题」当噪声，它拿到 0.589 —— 但那句里带着「不存在的话题」四个真词，压根不是噪声。
# 一个选坏了的探针差点让我得出「分数毫无区分力」的相反结论。）
#
# 真实缺陷是：**纯噪声照样返回满额结果，且没有任何「这批很弱」的信号**。
# 用户拿到 5 条分数 0.40–0.47 的东西，和拿到 5 条 0.66 的东西，在响应里长得一模一样。
#
# 为什么只标注、不默认过滤：8 个数据点定不出一个生产阈值 —— 那正是「拍脑袋常数」。
# 默认过滤一旦把阈值定高，丢掉的是真记忆，比多返回几条噪声严重得多。所以：
#   · 默认（floor=0）：只标注，不丢任何结果，行为与整改前逐字节一致；
#   · 部署方显式设 `AIDUMEM_RECALL_SCORE_FLOOR` 才启用过滤，且过滤掉几条要报出来。
_SCORE_FLOOR_ENV = "AIDUMEM_RECALL_SCORE_FLOOR"


def _score_floor() -> float:
    """读取召回下限；未设置或值非法一律返回 0.0（= 只标注不过滤）。

    值非法时打 warning 而不是静默当 0 —— 「设了一个打错的阈值」和「没设」
    在行为上一样，但在意图上完全不同（铁律 13）。
    """
    raw = (os.environ.get(_SCORE_FLOOR_ENV) or "").strip()
    if not raw:
        return 0.0
    try:
        v = float(raw)
    except ValueError:
        logger.warning("%s=%r 不是数字，本次按不过滤处理（只标注）", _SCORE_FLOOR_ENV, raw[:20])
        return 0.0
    if not (0.0 <= v <= 1.0):
        logger.warning("%s=%s 超出 [0,1]，本次按不过滤处理（只标注）", _SCORE_FLOOR_ENV, v)
        return 0.0
    return v


def annotate_recall_strength(results: list, floor: float | None = None) -> dict:
    """给召回结果打强度标注，返回随响应下发的元信息。

    返回 `{"top_score", "floor", "weak", "dropped"}`：
      · `top_score` —— 本次最高分（无结果时 None，**不是 0.0**：0.0 会被读成
        「有结果但都是 0 分」，那是另一件事）
      · `weak`      —— 最高分低于 floor（floor=0 时恒 False）
      · `dropped`   —— 被 floor 过滤掉的条数（floor=0 时恒 0）
    """
    f = _score_floor() if floor is None else floor
    scores = [r.get("score") for r in results if isinstance(r, dict)]
    nums = [float(x) for x in scores if isinstance(x, (int, float))]
    top = max(nums) if nums else None
    dropped = 0
    if f > 0.0 and results:
        keep = [r for r in results
                if not isinstance(r, dict)
                or not isinstance(r.get("score"), (int, float))
                or float(r["score"]) >= f]
        dropped = len(results) - len(keep)
        results[:] = keep
    return {
        "top_score": round(top, 4) if top is not None else None,
        "floor": f,
        "weak": bool(top is not None and f > 0.0 and top < f),
        "dropped": dropped,
    }


def register_search_routes(app: FastAPI) -> None:
    @app.post("/search", response_model=SearchResponse)
    def search(req: SearchRequest):
        """搜索记忆 — Workspace 优先 → 混合召回（Hybrid）→ Salience boost"""
        try:
            # 注意：/search 是显式搜索 API，不走 relevance gate（gate 用于对话上下文注入）
            # v20 P0-4：每请求重置 rerank 遥测——线程复用时上一请求的残留
            # 会被误读成本次的重排序结局。
            from ducky.mem0_runtime import last_rerank_telemetry, reset_rerank_telemetry
            reset_rerank_telemetry()
            recall_path = "hybrid"
            mem = get_memory()
            scope = make_scope(req.user_id, req.bank_id)
            uid = _normalize_user_id(scope.user_id)
            bank_id = scope.bank_id
            ensure_bank_registered(make_scope(uid, bank_id))

            try:
                from ducky.memory_workspace import ws_lookup, ws_feed_from_results
                ws_hits = ws_lookup(uid, req.query, bank_id=bank_id)
                if ws_hits:
                    boost_salience_for_results(ws_hits)
                    return {
                        "status": "ok", "results": ws_hits,
                        "_workspace_hit": True,
                        "_recall_path": "workspace",
                        "_rerank": {"status": "not_invoked"},
                    }
            except ImportError:
                pass

            results = []
            effective_limit = req.top_k if req.top_k and req.top_k > 0 else req.limit
            try:
                results = lazy_import_hybrid()(
                    mem, req.query, uid, effective_limit,
                    before=req.before, after=req.after,
                    bank_id=bank_id,
                )
                logger.info(f"🔍 hybrid 召回: query='{req.query}' user_id='{_normalize_user_id(req.user_id)}' → {len(results)} 条")
            except Exception as e:
                recall_path = "mem0_degraded"
                logger.debug(f"混合召回不可用，降级 mem0 搜索: {e}")
                raw = mem.search(req.query, filters=vector_scope_filters(uid, bank_id), top_k=max(effective_limit * 3, 20))
                results = raw.get("results", raw) if isinstance(raw, dict) else raw
                # 🔴v20：默认域不下推 bank_id（下推=清空存量），命名域的点在这里剔除。
                results = [it for it in (results or []) if vector_item_in_bank(it, bank_id)]
                if req.before or req.after:
                    # 降级路径也必须兑现 P0-4 时间窗口，否则混合召回一挂
                    # before/after 就被静默丢弃，时间推理返回错误结果。
                    _filter_results_by_time(results, req.before, req.after)
                logger.info(f"🔍 mem0 裸搜: query='{req.query}' user_id='{_normalize_user_id(req.user_id)}' → {len(results)} 条")

            boost_salience_for_results(results)
            _annotate_memory_types(results)

            # 📼 v19.4.0 明镜工程 Phase 1: Verbatim Vault 原文证据融合
            # 在既有召回结果之上，并行检索原文层并融合返回（主干优先、保留配额、
            # 失败干净降级）。让召回的不只是蒸馏后的事实，还有说过的原话。
            try:
                from ducky.verbatim_vault import verbatim_search, fuse_verbatim
                v_hits = verbatim_search(req.query, uid, limit=effective_limit, bank_id=bank_id)
                if v_hits:
                    results = fuse_verbatim(results, v_hits, limit=effective_limit, query=req.query)
            except Exception as _ve:
                logger.debug(f"📼 [VerbatimVault] 原文融合跳过: {_ve}")

            try:
                from ducky.memory_workspace import ws_feed_from_results
                ws_feed_from_results(uid, results, bank_id=bank_id)
            except ImportError:
                pass

            # v20 P0-4：召回路径与 rerank 三态随响应返回——「降级裸搜」和
            # 「重排序其实没生效」此前只活在服务端日志里，调用方无从察觉。
            rerank_telem = last_rerank_telemetry() or {"status": "not_invoked"}
            # v20：召回强度随响应下发。整改前「5 条 0.66」和「5 条 0.42」
            # 在响应里长得一模一样，调用方无从判断这批东西值不值得信。
            strength = annotate_recall_strength(results)
            if strength["dropped"]:
                logger.info("召回下限过滤掉 %d 条（floor=%s, top=%s）",
                            strength["dropped"], strength["floor"], strength["top_score"])
            return {
                "status": "ok", "results": results,
                "_recall_path": recall_path,
                "_rerank": rerank_telem,
                "_recall_strength": strength,
            }
        except Exception as e:
            logger.error(f"search 失败: {e}")
            return {"status": "error", "results": [], "detail": str(e)}

    @app.post("/search_trace")
    def search_trace(req: SearchRequest):
        """搜索记忆 + Recall Funnel trace（带分阶段耗时）"""
        try:
            mem = get_memory()
            effective_limit = req.top_k if req.top_k and req.top_k > 0 else req.limit
            scope = make_scope(req.user_id, req.bank_id)
            result = lazy_import_funnel()(mem, req.query, _normalize_user_id(scope.user_id), effective_limit, bank_id=scope.bank_id)
            # P0-4：与 /search 保持一致的时间窗口过滤。funnel 若返回
            # results 列表，这里做一次客户端过滤，不改变 trace 结构。
            if req.before or req.after:
                result = _apply_time_window_to_trace(result, req.before, req.after)
            return result
        except ImportError:
            raise HTTPException(503, "Recall Funnel 模块未就绪")
        # P1-4（v19.4.1）：先放行 HTTPException —— 否则注入拦截的 400
        # 会被下面的 except Exception 吞掉再包成 500，调用方无法区分
        # 「内容被拒」与「服务端故障」（实机冒烟：注入拦截返回 500）。
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"search_trace 失败: {e}")
            return {
                "status": "error",
                "trace": {"stages": [], "total_ms": 0, "final_count": 0},
                "results": [],
                "detail": str(e),
            }

    # 🔴1：Tahoe-Gate 相关性闸门端点。此前 relevance_check 全库零生产调用，
    # 「省 Token」卖点是画饼。现暴露为 /gate，供宿主 Agent 在对话上下文注入前
    # 先问一句「这轮要不要检索记忆」——闲聊直接跳过检索，真正省掉 Token 与算力。
    @app.get("/gate")
    def gate(query: str = "", text: str = "", q: str = ""):
        """相关性闸门：判断这条 query 是否需要检索记忆上下文。

        兼容 query / text / q 三种入参键名。
        返回 {needs_memory, reason, scope}。宿主在注入记忆前调用它，
        needs_memory=false 时可整轮跳过 /search，省掉无谓的向量检索。
        """
        actual_query = (query or text or q or "").strip()
        if not actual_query:
            return {"status": "ok", "needs_memory": False, "reason": "empty_query", "scope": None}
        try:
            from ducky.pipeline.memory_gate import relevance_check
            return {"status": "ok", **relevance_check(actual_query)}
        except Exception as e:
            logger.error(f"gate 失败: {e}")
            return {"status": "error", "needs_memory": True, "reason": f"gate_error: {e}", "scope": None}
