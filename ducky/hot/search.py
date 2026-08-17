"""ducky.hot.search — /search /search_trace"""
from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException

from ducky.api_models import SearchRequest, SearchResponse
from ducky.mem0_runtime import (
    _normalize_user_id,
    boost_salience_for_results,
    get_memory,
    lazy_import_funnel,
    lazy_import_hybrid,
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


def register_search_routes(app: FastAPI) -> None:
    @app.post("/search", response_model=SearchResponse)
    def search(req: SearchRequest):
        """搜索记忆 — Workspace 优先 → 混合召回（Hybrid）→ Salience boost"""
        try:
            # 注意：/search 是显式搜索 API，不走 relevance gate（gate 用于对话上下文注入）
            mem = get_memory()

            try:
                from ducky.memory_workspace import ws_lookup, ws_feed_from_results
                ws_hits = ws_lookup(req.user_id, req.query)
                if ws_hits:
                    boost_salience_for_results(ws_hits)
                    return {"status": "ok", "results": ws_hits, "_workspace_hit": True}
            except ImportError:
                pass

            results = []
            effective_limit = req.top_k if req.top_k and req.top_k > 0 else req.limit
            try:
                results = lazy_import_hybrid()(
                    mem, req.query, _normalize_user_id(req.user_id), effective_limit,
                    before=req.before, after=req.after,
                )
                logger.info(f"🔍 hybrid 召回: query='{req.query}' user_id='{_normalize_user_id(req.user_id)}' → {len(results)} 条")
            except Exception as e:
                logger.debug(f"混合召回不可用，降级 mem0 搜索: {e}")
                raw = mem.search(req.query, filters={"user_id": _normalize_user_id(req.user_id)}, top_k=max(effective_limit * 3, 20))
                results = raw.get("results", raw) if isinstance(raw, dict) else raw
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
                v_hits = verbatim_search(req.query, _normalize_user_id(req.user_id), limit=effective_limit)
                if v_hits:
                    results = fuse_verbatim(results, v_hits, limit=effective_limit, query=req.query)
            except Exception as _ve:
                logger.debug(f"📼 [VerbatimVault] 原文融合跳过: {_ve}")

            try:
                from ducky.memory_workspace import ws_feed_from_results
                ws_feed_from_results(req.user_id, results)
            except ImportError:
                pass

            return {"status": "ok", "results": results}
        except Exception as e:
            logger.error(f"search 失败: {e}")
            return {"status": "error", "results": [], "detail": str(e)}

    @app.post("/search_trace")
    def search_trace(req: SearchRequest):
        """搜索记忆 + Recall Funnel trace（带分阶段耗时）"""
        try:
            mem = get_memory()
            effective_limit = req.top_k if req.top_k and req.top_k > 0 else req.limit
            result = lazy_import_funnel()(mem, req.query, req.user_id, effective_limit)
            # P0-4：与 /search 保持一致的时间窗口过滤。funnel 若返回
            # results 列表，这里做一次客户端过滤，不改变 trace 结构。
            if req.before or req.after:
                result = _apply_time_window_to_trace(result, req.before, req.after)
            return result
        except ImportError:
            raise HTTPException(503, "Recall Funnel 模块未就绪")
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

