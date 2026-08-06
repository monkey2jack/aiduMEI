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
from pydantic import BaseModel

from ducky.conflict_resolver import resolve_fact_conflict, scan_and_resolve_text_conflicts
from ducky.tree_memory import add_tree_node, get_subtree
from ducky.skill_crystallizer import detect_and_crystallize_patterns, list_crystals

logger = logging.getLogger("aiduMEM.OctopusRoutes")


class ConflictCheckRequest(BaseModel):
    category: str = "general"
    fact_key: str = ""
    fact_value: str = ""
    text: str = ""


class TreeNodeRequest(BaseModel):
    name: str
    parent_path: str = "/aidu"
    description: str = ""


def register_octopus_routes(app: FastAPI) -> None:
    @app.post("/conflict/resolve")
    def conflict_resolve_endpoint(req: ConflictCheckRequest):
        """显式触发冲突检测与消解"""
        try:
            res_fact = None
            if req.fact_key and req.fact_value:
                res_fact = resolve_fact_conflict(req.category, req.fact_key, req.fact_value)
            res_text = []
            if req.text:
                res_text = scan_and_resolve_text_conflicts(req.text)
            return {
                "status": "ok",
                "fact_override": res_fact,
                "text_conflicts_invalidated": res_text,
            }
        except Exception as e:
            logger.error("🐙 /conflict/resolve 错误: %s", e)
            raise HTTPException(500, str(e))

    @app.get("/tree/nodes")
    def tree_nodes_endpoint(root_path: str = Query("/aidu", description="根节点路径")):
        """查询树状结构子树"""
        try:
            nodes = get_subtree(root_path)
            return {"status": "ok", "root_path": root_path, "nodes": nodes, "count": len(nodes)}
        except Exception as e:
            logger.error("🐙 /tree/nodes 错误: %s", e)
            raise HTTPException(500, str(e))

    @app.post("/tree/node")
    def tree_node_add_endpoint(req: TreeNodeRequest):
        """新增/更新树状节点"""
        try:
            res = add_tree_node(req.name, req.parent_path, req.description)
            if "error" in res:
                raise HTTPException(400, res["error"])
            return {"status": "ok", "node": res}
        except HTTPException:
            raise
        except Exception as e:
            logger.error("🐙 /tree/node 错误: %s", e)
            raise HTTPException(500, str(e))

    @app.get("/crystals")
    def crystals_list_endpoint(status: str = "candidate"):
        """获取技能结晶候选项列表"""
        try:
            crystals = list_crystals(status)
            return {"status": "ok", "crystals": crystals, "count": len(crystals)}
        except Exception as e:
            logger.error("🐙 /crystals 错误: %s", e)
            raise HTTPException(500, str(e))

    @app.post("/crystals/detect")
    def crystals_detect_endpoint():
        """手动触发模式感知与技能结晶"""
        try:
            detected = detect_and_crystallize_patterns()
            return {"status": "ok", "detected": detected, "count": len(detected)}
        except Exception as e:
            logger.error("🐙 /crystals/detect 错误: %s", e)
            raise HTTPException(500, str(e))
