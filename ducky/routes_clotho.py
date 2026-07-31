"""aiduMEM v11 Hyperion 路由。保持业务模块独立，入口只负责组装。"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

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


class CheckpointPayload(BaseModel):
    session_id: str
    blocks: dict


def register_clotho_routes(app: FastAPI) -> None:
    """注册 CoreMemory、Checkpoint 与 AutoDream API。"""

    @app.get("/api/core-memory")
    def api_core_memory_get():
        return {"status": "ok", "blocks": get_all_blocks()}

    @app.get("/api/core-memory/{block_key}")
    def api_core_memory_get_one(block_key: str):
        block = get_block(block_key)
        if not block:
            raise HTTPException(404, f"block_key 不存在: {block_key}")
        return {"status": "ok", "block": block}

    @app.put("/api/core-memory/{block_key}")
    def api_core_memory_put(block_key: str, content: dict):
        try:
            result = put_block(block_key, content.get("content", ""))
            return {"status": "ok", "result": result}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/core-memory/inject")
    def api_core_memory_inject():
        return {"status": "ok", "context": core_memory_context()}

    @app.get("/api/checkpoint/latest")
    def api_checkpoint_latest():
        checkpoint = get_latest_checkpoint()
        if not checkpoint:
            return {"status": "ok", "checkpoint": None, "message": "暂无快照"}
        return {"status": "ok", "checkpoint": checkpoint}

    @app.get("/api/checkpoint/{session_id}")
    def api_checkpoint_get(session_id: str):
        checkpoint = get_checkpoint(session_id)
        if not checkpoint:
            raise HTTPException(404, f"session_id 不存在: {session_id}")
        return {"status": "ok", "checkpoint": checkpoint}

    @app.post("/api/checkpoint")
    def api_checkpoint_write(payload: CheckpointPayload):
        try:
            result = write_checkpoint(payload.session_id, payload.blocks)
            return {"status": "ok", "result": result}
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.delete("/api/checkpoint/cleanup")
    def api_checkpoint_cleanup():
        return {"status": "ok", "result": cleanup_old_checkpoints()}

    @app.post("/api/checkpoint/inject")
    def api_checkpoint_inject():
        return {"status": "ok", "context": checkpoint_context()}

    @app.get("/api/autodream/status")
    def api_autodream_status():
        return {"status": "ok", "dream": get_dream_status()}

    @app.post("/api/autodream/trigger")
    def api_autodream_trigger():
        return {"status": "ok", "result": trigger_dream()}

    @app.get("/api/autodream/report")
    def api_autodream_report():
        return get_dream_report()
