"""ducky.hot.search — /search /search_trace"""
from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException

from ducky.api_models import SearchRequest, SearchResponse
from ducky.mem0_runtime import (
    boost_salience_for_results,
    get_memory,
    lazy_import_funnel,
    lazy_import_hybrid,
)

logger = logging.getLogger("aiduMEM.hot")


def register_search_routes(app: FastAPI) -> None:
    @app.post("/search", response_model=SearchResponse)
    def search(req: SearchRequest):
        """搜索记忆 — Gate → Workspace 优先 → Ignition → 混合召回 → Salience boost"""
        try:
            try:
                from ducky.memory_gate import relevance_check
                gate = relevance_check(req.query)
                if not gate["needs_memory"]:
                    logger.debug(f"闸门跳过: {gate['reason']}")
                    return {"status": "ok", "results": [], "_gate_blocked": gate["reason"]}
            except ImportError:
                pass

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
            try:
                results = lazy_import_hybrid()(mem, req.query, req.user_id, req.limit)
            except (ImportError, Exception) as e:
                logger.debug(f"混合召回不可用，降级 mem0 搜索: {e}")
                raw = mem.search(req.query, filters={"user_id": req.user_id}, limit=req.limit)
                results = raw.get("results", raw) if isinstance(raw, dict) else raw

            boost_salience_for_results(results)

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
            result = lazy_import_funnel()(mem, req.query, req.user_id, req.limit)
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

