"""
ducky.routes_octopus — v16.0 Opus Octopod (opus八爪鱼) 专属端点
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
暴露：
1. POST /conflict/resolve — 手动/隐式触发冲突消解
2. GET  /tree/nodes       — 查询树状结构子树
3. POST /tree/node        — 添加树状节点
4. GET  /crystals          — 查询沉淀的技能结晶候选项
5. POST /crystals/detect   — 立即触发一次技能结晶感知

f0.3：/crystals 全族（含 use / prune / approve）是**系统级**端点——结晶从全实例
事实里挖出、按全局自增 id 审批，不在 (user_id, bank_id) 租户轴上。默认关闭，
设 AIDUMEI_CRYSTALS_ENABLED=true 才放行（见 ducky.system_endpoints）。
冲突消解与树状记忆按域隔离，不受影响。
"""
from __future__ import annotations

import logging
from fastapi import FastAPI, HTTPException


from ducky.skill_crystallizer import (
    list_crystals,
)
from ducky.api_errors import api_error_detail
from ducky.system_endpoints import system_route

logger = logging.getLogger("aiduMEM.OctopusRoutes")


def register_octopus_routes(app: FastAPI) -> None:


    @app.get("/crystals", **system_route("crystals"))
    def crystals_list_endpoint(status: str = "candidate"):
        """获取技能结晶候选项列表"""
        try:
            crystals = list_crystals(status)
            return {"status": "ok", "crystals": crystals, "count": len(crystals)}
        except HTTPException:
            raise
        except Exception as e:
            logger.error("🐙 /crystals 错误: %s", e)
            raise HTTPException(500, api_error_detail(e))


