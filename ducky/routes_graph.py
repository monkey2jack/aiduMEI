"""f0.6++ WP5 · 记忆图谱 API(只读;写边走 WP5 派生函数,不暴露写端点)。

两个端点:
  GET /graph/neighbors?user_id=&node_type=&node_id=(&depth=&bank_id=&limit=)
      → 按 node 展开邻域(depth 1-3;默认 1)
  GET /graph/stats?user_id=(&bank_id=)
      → 边按类型计数

鉴权:scope read(与 /facts 族同款;走 ScopeRegistrar 统一收口)。
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException

from ducky.api_models import DEFAULT_USER_ID, DEFAULT_BANK_ID
from ducky.graph_memory import graph_stats, neighbors

logger = logging.getLogger("aiduMEM.graph_memory")


def register_graph_routes(app: FastAPI) -> None:
    @app.get("/graph/neighbors")
    def api_graph_neighbors(
        user_id: str = DEFAULT_USER_ID,
        node_type: str = "fact",
        node_id: str = "",
        bank_id: str = DEFAULT_BANK_ID,
        depth: int = 1,
        limit: int = 50,
        caller_user_id: str = "",
    ):
        if not node_id:
            raise HTTPException(400, "node_id 必填")
        try:
            return {
                "status": "ok",
                "node_id": node_id,
                **neighbors(user_id, node_type, node_id,
                            bank_id=bank_id, depth=depth, limit=limit),
                "caller_user_id": caller_user_id,
            }
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @app.get("/graph/stats")
    def api_graph_stats(
        user_id: str = DEFAULT_USER_ID,
        bank_id: str = DEFAULT_BANK_ID,
        caller_user_id: str = "",
    ):
        return {"status": "ok", "user_id": user_id, "bank_id": bank_id,
                "caller_user_id": caller_user_id, **graph_stats(user_id, bank_id)}
