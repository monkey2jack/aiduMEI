"""
ducky.routes_p0 — v19.0 P0 认知层路由（Reflect 反思 / 记忆去重自编辑）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
把 P0-3 Reflect 与 P0-2 自编辑账本暴露为 REST 端点，供控制台、
Hermes 插件与运维脚本调用。全部降级友好：LLM 不可用时返回空洞察
而非 500。
"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict

from ducky.utils import DEFAULT_USER_ID

logger = logging.getLogger("aiduMEM.routes_p0")


class ReflectRequest(BaseModel):
    """POST /reflect 请求体。

    FastAPI 的标量参数默认走 query string，这里显式建模后 JSON body
    才会被正确解析（与 /add、/search 的既有风格一致）。
    """

    model_config = ConfigDict(extra="allow")

    user_id: str = DEFAULT_USER_ID
    top_k: int = 20
    source: str = "manual"
    save: bool = True
    # 兼容 MCP mem_reflect 旧调用方：显式 topic 时围绕该主题检索反思。
    topic: str = ""


class RollbackRequest(BaseModel):
    """POST /self-edit/rollback 请求体。"""

    model_config = ConfigDict(extra="allow")

    edit_id: int


def register_p0_routes(app: FastAPI) -> None:
    # ── Reflect 反思（P0-3 核心）──────────────────────────────
    @app.post("/reflect")
    def reflect_run(req: ReflectRequest):
        """触发一次记忆反思：回顾近期记忆 → LLM 提炼洞察 → 落库。

        降级：LLM 未配置/失败时返回空洞察，不报错。
        """
        from ducky.reflect import run_reflect

        try:
            return run_reflect(
                user_id=req.user_id,
                top_k=req.top_k,
                source=req.source,
                save=req.save,
                topic=req.topic,
            )
        except Exception as e:
            logger.error(f"/reflect 失败: {e}")
            return {"status": "error", "detail": str(e), "insights": []}

    @app.get("/reflect/list")
    def reflect_list(user_id: str = DEFAULT_USER_ID, limit: int = 20, insight_type: str = ""):
        """查询已落库的反思洞察。"""
        from ducky.reflect import get_reflections

        try:
            rows = get_reflections(user_id=user_id, limit=limit, insight_type=insight_type)
            return {"status": "ok", "insights": rows, "count": len(rows)}
        except Exception as e:
            logger.error(f"/reflect/list 失败: {e}")
            return {"status": "error", "detail": str(e), "insights": []}

    @app.get("/reflect/context")
    def reflect_context(user_id: str = DEFAULT_USER_ID, limit: int = 5):
        """把最近洞察格式化为可注入上下文（供 Hermes 下一轮对话引用）。"""
        from ducky.reflect import inject_reflections

        try:
            return {"status": "ok", "context": inject_reflections(user_id=user_id, limit=limit)}
        except Exception as e:
            logger.error(f"/reflect/context 失败: {e}")
            return {"status": "error", "detail": str(e), "context": ""}

    # ── 记忆去重自编辑（P0-2）────────────────────────────────
    @app.get("/self-edit/edits")
    def self_edit_list(user_id: str = DEFAULT_USER_ID, limit: int = 20, include_undone: bool = False):
        """列出记忆自编辑账本（合并/冲突快照）。"""
        from ducky.self_edit import list_edits

        try:
            rows = list_edits(user_id=user_id, limit=limit, include_undone=include_undone)
            return {"status": "ok", "edits": rows, "count": len(rows)}
        except Exception as e:
            logger.error(f"/self-edit/edits 失败: {e}")
            return {"status": "error", "detail": str(e), "edits": []}

    @app.post("/self-edit/rollback")
    def self_edit_rollback(req: RollbackRequest):
        """回滚一次自编辑：把记忆恢复到编辑前内容。"""
        from ducky.self_edit import rollback_edit

        try:
            return rollback_edit(req.edit_id)
        except Exception as e:
            logger.error(f"/self-edit/rollback 失败: {e}")
            return {"status": "error", "detail": str(e)}
