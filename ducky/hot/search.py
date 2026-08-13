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
    """把六型分类结果回填到检索结果（P2-3：分类从「写了不读」变为参与召回）。

    - 只读账本，不触发任何 LLM 调用，检索性能不受影响；
    - 每条结果写入 memory_type 字段；账本无记录时默认 FACTS。
    - 失败静默降级（检索优先，分类失败不阻断召回）。
    """
    if not results:
        return
    try:
        from ducky.memory_types import get_memory_type

        for item in results:
            if not isinstance(item, dict):
                continue
            ref = ""
            meta = item.get("metadata") or {}
            if isinstance(meta, dict):
                ref = meta.get("fact_key") or meta.get("fact_id") or ""
            if not ref:
                ref = item.get("id") or item.get("memory_id") or ""
            if not ref:
                continue
            try:
                item["memory_type"] = get_memory_type(str(ref))
            except Exception:
                continue
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
            from ducky.engine import _norm_bound
            b_prefix = _norm_bound(before, is_before=True)
            a_prefix = _norm_bound(after, is_before=False)
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
    def gate(query: str):
        """相关性闸门：判断这条 query 是否需要检索记忆上下文。

        返回 {needs_memory, reason, scope}。宿主在注入记忆前调用它，
        needs_memory=false 时可整轮跳过 /search，省掉无谓的向量检索。
        """
        try:
            from ducky.pipeline.memory_gate import relevance_check
            return {"status": "ok", **relevance_check(query)}
        except Exception as e:
            logger.error(f"gate 失败: {e}")
            # 失败时保守放行（需要记忆），不因闸门故障而漏召回
            return {"status": "error", "needs_memory": True, "reason": f"gate_error: {e}", "scope": None}

