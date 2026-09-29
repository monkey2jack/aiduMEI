"""
ducky.routes_persona — v19.0 人格记忆基座路由（Persona Memory Layer）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
把人格记忆基座（persona_memory）暴露为 REST 端点，供任何下游 agent
构建 / 检索 / 回滚自己的「按情境检索的自传体记忆库」。

    POST   /persona/build        构建基座（synthesis 合成 | grounded 真实）
    GET    /persona/banks        列出基座（可回滚到任意历史版本）
    GET    /persona/detail       查看某基座全部 L/G/E 记忆
    POST   /persona/retrieve     按当前情境检索相关人格记忆（dynamic conditioning）
    POST   /persona/rollback     回滚到指定版本（数据不删，只切状态）
    GET    /persona/context      直接拿注入用上下文文本

配置（f0.3 起**默认关闭**）：基座是全实例共享的派生数据，bank_id 是可枚举的
自增整数，不在 (user_id, bank_id) 租户轴上。部署方显式设
AIDUMEM_PERSONA_ENABLED=true 才放行；未开启时端点仍在路由表里，回
404 feature_disabled。OpenAPI 里标 `system-only (not tenant-isolated)`。
"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field

from ducky.api_models import (
    ID_FIELD_MAX_CHARS, SHORT_TEXT_MAX_CHARS, TEXT_FIELD_MAX_CHARS,
)
from ducky.system_endpoints import feature_enabled, system_route

logger = logging.getLogger("aiduMEM.routes_persona")


class PersonaBuildRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    persona_card: str = Field(default="", max_length=SHORT_TEXT_MAX_CHARS)  # 合成模式：简短人设
    persona_name: str = Field(default="", max_length=ID_FIELD_MAX_CHARS)
    persona_key: str = Field(default="", max_length=ID_FIELD_MAX_CHARS)     # 省略则从 persona_name/card 推导
    mode: str = Field(default="synthesis", max_length=64)                   # synthesis | grounded
    source_material: str = Field(default="", max_length=TEXT_FIELD_MAX_CHARS)  # 真实模式：素材原文（多行）
    use_llm: bool = True


class PersonaRetrieveRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    situation: str = Field(..., max_length=SHORT_TEXT_MAX_CHARS)  # 当前情境描述
    persona_key: str = Field(default="", max_length=ID_FIELD_MAX_CHARS)
    bank_id: int = 0
    k: int = 5
    level: str = Field(default="", max_length=16)                 # 可选只取某层 L/G/E


class PersonaRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    persona_key: str = Field(..., max_length=ID_FIELD_MAX_CHARS)
    to_version: int


def register_persona_routes(app: FastAPI) -> None:
    # f0.3：路由**始终注册**（路由表 / OpenAPI / MCP 契约保持稳定），是否放行
    # 由 system_route("persona") 的依赖在**每次请求**时读 AIDUMEM_PERSONA_ENABLED
    # 判定 —— 默认关闭（基座是全实例共享的、bank_id 是可枚举自增整数，不在租户轴上）。
    if not feature_enabled("AIDUMEM_PERSONA_ENABLED"):
        logger.info("👤 人格记忆基座端点默认关闭（系统级、未按域隔离）；"
                    "设 AIDUMEM_PERSONA_ENABLED=true 开启")

    @app.post("/persona/build", **system_route("persona"))
    def persona_build(req: PersonaBuildRequest):
        """构建人格记忆基座。

        - synthesis：从一句话人设生成虚构自传体记忆库（面向虚构角色）
        - grounded：从真实素材抽取组织记忆，零虚构，每条可回溯 source_ref
        """
        from ducky.persona_memory import build_persona

        try:
            return build_persona(
                req.persona_card,
                mode=req.mode,
                persona_key=req.persona_key,
                persona_name=req.persona_name,
                source_material=req.source_material,
                use_llm=req.use_llm,
            )
        except Exception as e:
            logger.error(f"/persona/build 失败: {e}")
            return {"status": "error", "detail": str(e)}

    @app.get("/persona/banks", **system_route("persona"))
    def persona_banks(persona_key: str = "", status: str = ""):
        """列出基座（含版本、L/G/E 计数、构建耗时）。"""
        from ducky.persona_memory import list_banks

        try:
            return {"status": "ok", "banks": list_banks(persona_key=persona_key, status=status)}
        except Exception as e:
            logger.error(f"/persona/banks 失败: {e}")
            return {"status": "error", "detail": str(e), "banks": []}

    @app.get("/persona/detail", **system_route("persona"))
    def persona_detail(bank_id: int):
        """查看某基座全部 L/G/E 记忆。"""
        from ducky.persona_memory import get_bank_detail

        try:
            return get_bank_detail(bank_id)
        except Exception as e:
            logger.error(f"/persona/detail 失败: {e}")
            return {"status": "error", "detail": str(e)}

    @app.post("/persona/retrieve", **system_route("persona"))
    def persona_retrieve(req: PersonaRetrieveRequest):
        """按当前情境检索相关人格记忆（替代整卡注入）。"""
        from ducky.persona_memory import retrieve_persona

        try:
            return retrieve_persona(
                req.situation,
                persona_key=req.persona_key,
                bank_id=req.bank_id,
                k=req.k,
                level=req.level,
            )
        except Exception as e:
            logger.error(f"/persona/retrieve 失败: {e}")
            return {"status": "error", "detail": str(e), "results": []}

    @app.post("/persona/rollback", **system_route("persona"))
    def persona_rollback(req: PersonaRollbackRequest):
        """回滚到指定版本（数据不删，只切 ready/superseded 状态）。"""
        from ducky.persona_memory import rollback_persona

        try:
            return rollback_persona(req.persona_key, req.to_version)
        except Exception as e:
            logger.error(f"/persona/rollback 失败: {e}")
            return {"status": "error", "detail": str(e)}

    @app.get("/persona/context", **system_route("persona"))
    def persona_context(persona_key: str, situation: str = "", k: int = 5):
        """直接拿注入用上下文文本（供 Hermes 等下游在对话前调用）。"""
        from ducky.persona_memory import get_persona_context

        try:
            ctx = get_persona_context(persona_key, situation=situation, k=k)
            return {"status": "ok", "context": ctx}
        except Exception as e:
            logger.error(f"/persona/context 失败: {e}")
            return {"status": "error", "detail": str(e), "context": ""}
