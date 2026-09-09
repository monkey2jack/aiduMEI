"""aiduMEM v11 Hyperion 路由。保持业务模块独立，入口只负责组装。"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from ducky.api_models import (
    ID_FIELD_MAX_CHARS, QUERY_FIELD_MAX_CHARS,
    SHORT_TEXT_MAX_CHARS, TEXT_FIELD_MAX_CHARS,
)

from ducky.autodream import get_dream_report, get_dream_status, trigger_dream
from ducky.checkpoint import (
    cleanup_old_checkpoints,
    get_checkpoint,
    get_latest_checkpoint,
    inject_context as checkpoint_context,
    write_checkpoint,
)
from ducky.core_memory import (
    get_all_blocks,
    get_block,
    inject_context as core_memory_context,
    put_block,
)
from ducky.utils import DEFAULT_USER_ID
from ducky.bank_contract import DEFAULT_BANK_ID, make_scope


class CheckpointPayload(BaseModel):
    session_id: str = Field(..., max_length=ID_FIELD_MAX_CHARS)
    blocks: dict
    # v20.4.0（P1-1）：快照落在调用方自己的域里；缺省 = 默认租户/默认库
    user_id: str = Field(default=DEFAULT_USER_ID, max_length=ID_FIELD_MAX_CHARS)
    bank_id: str = Field(default=DEFAULT_BANK_ID, max_length=ID_FIELD_MAX_CHARS)


def register_clotho_routes(app: FastAPI) -> None:
    """注册 CoreMemory、Checkpoint 与 AutoDream API。"""

    @app.get("/api/core-memory")
    def api_core_memory_get(
        user_id: str = DEFAULT_USER_ID,
        bank_id: str = DEFAULT_BANK_ID,
    ):
        scope = make_scope(user_id, bank_id)
        return {
            "status": "ok",
            "user_id": scope.user_id,
            "bank_id": scope.bank_id,
            "blocks": get_all_blocks(scope.user_id, scope.bank_id),
        }

    @app.get("/api/core-memory/{block_key}")
    def api_core_memory_get_one(
        block_key: str,
        user_id: str = DEFAULT_USER_ID,
        bank_id: str = DEFAULT_BANK_ID,
    ):
        scope = make_scope(user_id, bank_id)
        block = get_block(block_key, scope.user_id, scope.bank_id)
        if not block:
            raise HTTPException(404, f"block_key 不存在: {block_key}")
        return {"status": "ok", "block": block}

    @app.put("/api/core-memory/{block_key}")
    def api_core_memory_put(
        block_key: str,
        content: dict,
        user_id: str = DEFAULT_USER_ID,
        bank_id: str = DEFAULT_BANK_ID,
    ):
        try:
            scope = make_scope(user_id, bank_id)
            result = put_block(
                block_key,
                content.get("content", ""),
                scope.user_id,
                scope.bank_id,
            )
            return {"status": "ok", "result": result}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/core-memory/inject")
    def api_core_memory_inject(
        user_id: str = DEFAULT_USER_ID,
        bank_id: str = DEFAULT_BANK_ID,
    ):
        scope = make_scope(user_id, bank_id)
        return {
            "status": "ok",
            "user_id": scope.user_id,
            "bank_id": scope.bank_id,
            "context": core_memory_context(scope.user_id, scope.bank_id),
        }

    # v20.4.0（三方审计 P1-1 · 动态审计 🟡-1）：五个 checkpoint 端点全部带租户轴。
    # 此前 /latest 与 /inject 全库取最新 —— 租户 B 调 /inject 会把租户 A 的
    # 会话快照注入自己的上下文（动态审计实测取回 TENANT_A_SECRET）。
    @app.get("/api/checkpoint/latest")
    def api_checkpoint_latest(user_id: str = DEFAULT_USER_ID,
                              bank_id: str = DEFAULT_BANK_ID):
        scope = make_scope(user_id, bank_id)
        checkpoint = get_latest_checkpoint(scope.user_id, scope.bank_id)
        if not checkpoint:
            return {"status": "ok", "checkpoint": None, "message": "暂无快照"}
        return {"status": "ok", "checkpoint": checkpoint}

    @app.get("/api/checkpoint/{session_id}")
    def api_checkpoint_get(session_id: str,
                           user_id: str = DEFAULT_USER_ID,
                           bank_id: str = DEFAULT_BANK_ID):
        scope = make_scope(user_id, bank_id)
        checkpoint = get_checkpoint(session_id, user_id=scope.user_id,
                                    bank_id=scope.bank_id)
        if not checkpoint:
            raise HTTPException(404, f"session_id 不存在: {session_id}")
        return {"status": "ok", "checkpoint": checkpoint}

    @app.post("/api/checkpoint")
    def api_checkpoint_write(payload: CheckpointPayload):
        try:
            scope = make_scope(payload.user_id, payload.bank_id)
            result = write_checkpoint(payload.session_id, payload.blocks,
                                      user_id=scope.user_id, bank_id=scope.bank_id)
            return {"status": "ok", "result": result}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete("/api/checkpoint/cleanup")
    def api_checkpoint_cleanup(user_id: str = DEFAULT_USER_ID,
                               bank_id: str = DEFAULT_BANK_ID):
        scope = make_scope(user_id, bank_id)
        return {"status": "ok",
                "result": cleanup_old_checkpoints(scope.user_id, scope.bank_id)}

    @app.post("/api/checkpoint/inject")
    def api_checkpoint_inject(user_id: str = DEFAULT_USER_ID,
                              bank_id: str = DEFAULT_BANK_ID):
        scope = make_scope(user_id, bank_id)
        return {"status": "ok",
                "context": checkpoint_context(scope.user_id, scope.bank_id)}

    @app.get("/api/autodream/status")
    def api_autodream_status():
        return {"status": "ok", "dream": get_dream_status()}

    @app.post("/api/autodream/trigger")
    def api_autodream_trigger():
        return {"status": "ok", "result": trigger_dream()}

    @app.get("/api/autodream/report")
    def api_autodream_report():
        return get_dream_report()
