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

配置：AIDUMEM_PERSONA_ENABLED=false 可整体关闭（register_persona_routes 变 no-op）。
"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger("aiduMEM.routes_persona")


class PersonaBuildRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    persona_card: str = ""            # 合成模式：简短人设
    persona_name: str = ""
    persona_key: str = ""             # 省略则从 persona_name/card 推导
    mode: str = "synthesis"           # synthesis | grounded
    source_material: str = ""         # 真实模式：素材原文（多行）
    use_llm: bool = True


class PersonaRetrieveRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    situation: str                    # 当前情境描述
    persona_key: str = ""
    bank_id: int = 0
    k: int = 5
    level: str = ""                   # 可选只取某层 L/G/E


class PersonaRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    persona_key: str
    to_version: int


def register_persona_routes(app: FastAPI) -> None:
    from ducky.persona_memory import PERSONA_ENABLED, VALID_LEVELS

    if not PERSONA_ENABLED:
        logger.info("👤 人格记忆基座路由已禁用（AIDUMEM_PERSONA_ENABLED=false）")
        return

    @app.post("/persona/build")
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

    @app.get("/persona/banks")
    def persona_banks(persona_key: str = "", status: str = ""):
        """列出基座（含版本、L/G/E 计数、构建耗时）。"""
        from ducky.persona_memory import list_banks

        try:
            return {"status": "ok", "banks": list_banks(persona_key=persona_key, status=status)}
        except Exception as e:
            logger.error(f"/persona/banks 失败: {e}")
            return {"status": "error", "detail": str(e), "banks": []}

    @app.get("/persona/detail")
    def persona_detail(bank_id: int):
        """查看某基座全部 L/G/E 记忆。"""
        from ducky.persona_memory import get_bank_detail

        try:
            return get_bank_detail(bank_id)
        except Exception as e:
            logger.error(f"/persona/detail 失败: {e}")
            return {"status": "error", "detail": str(e)}

    @app.post("/persona/retrieve")
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

    @app.post("/persona/rollback")
    def persona_rollback(req: PersonaRollbackRequest):
        """回滚到指定版本（数据不删，只切 ready/superseded 状态）。"""
        from ducky.persona_memory import rollback_persona

        try:
            return rollback_persona(req.persona_key, req.to_version)
        except Exception as e:
            logger.error(f"/persona/rollback 失败: {e}")
            return {"status": "error", "detail": str(e)}

    @app.get("/persona/context")
    def persona_context(persona_key: str, situation: str = "", k: int = 5):
        """直接拿注入用上下文文本（供 Hermes 等下游在对话前调用）。"""
        from ducky.persona_memory import get_persona_context

        try:
            ctx = get_persona_context(persona_key, situation=situation, k=k)
            return {"status": "ok", "context": ctx}
        except Exception as e:
            logger.error(f"/persona/context 失败: {e}")
            return {"status": "error", "detail": str(e), "context": ""}
