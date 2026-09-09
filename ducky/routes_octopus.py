"""
ducky.routes_octopus — v16.0 Opus Octopod (opus八爪鱼) 专属端点
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
暴露：
1. POST /conflict/resolve — 手动/隐式触发冲突消解
2. GET  /tree/nodes       — 查询树状结构子树
3. POST /tree/node        — 添加树状节点
4. GET  /crystals          — 查询沉淀的技能结晶候选项
5. POST /crystals/detect   — 立即触发一次技能结晶感知
"""
from __future__ import annotations

import logging
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from ducky.api_models import (
    ID_FIELD_MAX_CHARS, QUERY_FIELD_MAX_CHARS,
    SHORT_TEXT_MAX_CHARS, TEXT_FIELD_MAX_CHARS,
)

from ducky.conflict_resolver import resolve_fact_conflict, scan_and_resolve_text_conflicts
from ducky.utils import DEFAULT_USER_ID
from ducky.bank_contract import DEFAULT_BANK_ID, make_scope
from ducky.tree_memory import add_tree_node, get_subtree
from ducky.skill_crystallizer import (
    detect_and_crystallize_patterns,
    list_crystals,
    record_skill_use,
    prune_low_utility_skills,
    approve_crystal,
)
from ducky.api_errors import api_error_detail

logger = logging.getLogger("aiduMEM.OctopusRoutes")


class ConflictCheckRequest(BaseModel):
    category: str = Field(default="general", max_length=ID_FIELD_MAX_CHARS)
    fact_key: str = Field(default="", max_length=ID_FIELD_MAX_CHARS)
    fact_value: str = Field(default="", max_length=SHORT_TEXT_MAX_CHARS)
    text: str = Field(default="", max_length=TEXT_FIELD_MAX_CHARS)
    user_id: str = Field(default=DEFAULT_USER_ID, max_length=ID_FIELD_MAX_CHARS)
    bank_id: str = Field(default=DEFAULT_BANK_ID, max_length=ID_FIELD_MAX_CHARS)


class TreeNodeRequest(BaseModel):
    name: str = Field(..., max_length=ID_FIELD_MAX_CHARS)
    parent_path: str = Field(default="/aidu", max_length=1024)
    description: str = Field(default="", max_length=SHORT_TEXT_MAX_CHARS)
    # v20.4.0（P1-6）：节点落在调用方自己的域里
    user_id: str = Field(default="default", max_length=ID_FIELD_MAX_CHARS)
    bank_id: str = Field(default="default", max_length=ID_FIELD_MAX_CHARS)


def register_octopus_routes(app: FastAPI) -> None:
    @app.post("/conflict/resolve")
    def conflict_resolve_endpoint(req: ConflictCheckRequest):
        """显式触发冲突检测与消解"""
        try:
            scope = make_scope(req.user_id, req.bank_id)
            res_fact = None
            if req.fact_key and req.fact_value:
                res_fact = resolve_fact_conflict(
                    req.category,
                    req.fact_key,
                    req.fact_value,
                    user_id=scope.user_id,
                    bank_id=scope.bank_id,
                )
            res_text = []
            if req.text:
                res_text = scan_and_resolve_text_conflicts(
                    req.text,
                    user_id=scope.user_id,
                    bank_id=scope.bank_id,
                )
            return {
                "status": "ok",
                "user_id": scope.user_id,
                "bank_id": scope.bank_id,
                "fact_override": res_fact,
                "text_conflicts_invalidated": res_text,
            }
        except Exception as e:
            logger.error("🐙 /conflict/resolve 错误: %s", e)
            raise HTTPException(500, api_error_detail(e))

    @app.get("/tree/nodes")
    def tree_nodes_endpoint(root_path: str = Query("/aidu", description="根节点路径"),
                            user_id: str = Query("default"), bank_id: str = Query("default")):
        """查询树状结构子树（v20.4.0 P1-6：仅本域节点与计数）"""
        try:
            nodes = get_subtree(root_path, user_id=user_id, bank_id=bank_id)
            return {"status": "ok", "root_path": root_path, "nodes": nodes, "count": len(nodes)}
        except Exception as e:
            logger.error("🐙 /tree/nodes 错误: %s", e)
            raise HTTPException(500, api_error_detail(e))

    @app.post("/tree/node")
    def tree_node_add_endpoint(req: TreeNodeRequest):
        """新增/更新树状节点"""
        try:
            res = add_tree_node(req.name, req.parent_path, req.description,
                                user_id=req.user_id, bank_id=req.bank_id)
            if "error" in res:
                raise HTTPException(400, res["error"])
            return {"status": "ok", "node": res}
        except HTTPException:
            raise
        except Exception as e:
            logger.error("🐙 /tree/node 错误: %s", e)
            raise HTTPException(500, api_error_detail(e))

    @app.get("/crystals")
    def crystals_list_endpoint(status: str = "candidate"):
        """获取技能结晶候选项列表"""
        try:
            crystals = list_crystals(status)
            return {"status": "ok", "crystals": crystals, "count": len(crystals)}
        except Exception as e:
            logger.error("🐙 /crystals 错误: %s", e)
            raise HTTPException(500, api_error_detail(e))

    @app.post("/crystals/detect")
    def crystals_detect_endpoint():
        """手动触发模式感知与技能结晶"""
        try:
            detected = detect_and_crystallize_patterns()
            return {"status": "ok", "detected": detected, "count": len(detected)}
        except Exception as e:
            logger.error("🐙 /crystals/detect 错误: %s", e)
            raise HTTPException(500, api_error_detail(e))

    # ── v19.0 P1-2 技能精炼：复用追踪 + 低效用淘汰 ─────────
    @app.post("/crystals/use")
    def crystals_use_endpoint(skill_name: str, success: bool = True):
        """记录一次技能复用成功/失败（P1-2 技能精炼）"""
        try:
            return record_skill_use(skill_name, success)
        except Exception as e:
            logger.error("🐙 /crystals/use 错误: %s", e)
            raise HTTPException(500, api_error_detail(e))

    @app.post("/crystals/prune")
    def crystals_prune_endpoint():
        """低效用技能自动标记为 archived（待淘汰，可人工复核）"""
        try:
            archived = prune_low_utility_skills()
            return {"status": "ok", "archived": archived, "count": len(archived)}
        except Exception as e:
            logger.error("🐙 /crystals/prune 错误: %s", e)
            raise HTTPException(500, api_error_detail(e))

    # 🔴8：人工审批端点——此前 approve_crystal 零调用方，draft 永远转不了正。
    @app.post("/crystals/approve")
    def crystals_approve_endpoint(crystal_id: int):
        """人工审核通过某个技能结晶候选项（candidate/draft -> approved）。

        遵循 Mímir 铁律：只有人工审核才能 approve，不可自动批准。
        """
        try:
            result = approve_crystal(crystal_id)
            if result.get("status") == "error":
                raise HTTPException(400, result.get("message", "approve 失败"))
            return result
        except HTTPException:
            raise
        except Exception as e:
            logger.error("🐙 /crystals/approve 错误: %s", e)
            raise HTTPException(500, api_error_detail(e))
