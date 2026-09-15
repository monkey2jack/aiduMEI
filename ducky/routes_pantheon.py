"""ducky.routes_pantheon — 众神殿殿管理 + 跨殿借阅端点 (v21.1)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
殿 = user_id。端点全部把 pantheon 层的 HallError（及其它异常）包成
{"status":"error","detail":...}，不抛 500（与 v8 路由约定一致）。
定位①：单主人多分身——这些端点是主人管理自己麾下诸殿与它们之间的借阅。
"""
from __future__ import annotations

import logging

from fastapi import FastAPI

from ducky import pantheon

logger = logging.getLogger("aiduMEM.RoutesPantheon")


def register_pantheon_routes(app: FastAPI) -> None:

    # ── 殿注册表 ──────────────────────────────────────────────
    @app.post("/pantheon/hall")
    def create_hall(user_id: str, display_name: str = "", description: str = ""):
        try:
            return {"status": "ok", "hall": pantheon.create_hall(
                user_id, display_name=display_name, description=description)}
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    @app.get("/pantheon/halls")
    def list_halls(include_inactive: bool = False):
        try:
            return {"status": "ok", "halls": pantheon.list_halls(include_inactive)}
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    @app.get("/pantheon/hall/{user_id}")
    def get_hall(user_id: str):
        try:
            hall = pantheon.get_hall(user_id)
            if hall is None:
                return {"status": "error", "detail": "殿不存在"}
            return {"status": "ok", "hall": hall}
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    @app.post("/pantheon/hall/{user_id}/deactivate")
    def deactivate_hall(user_id: str):
        try:
            return {"status": "ok", **pantheon.deactivate_hall(user_id)}
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    # ── 跨殿借阅 ──────────────────────────────────────────────
    @app.post("/pantheon/grant")
    def grant(grantor_user_id: str, grantee_user_id: str, actions: str = "read",
              bank_id: str = "*", expires_at: str = "", created_by: str = ""):
        try:
            return {"status": "ok", "grant": pantheon.grant_hall_access(
                grantor_user_id, grantee_user_id, actions=actions, bank_id=bank_id,
                expires_at=(expires_at or None), created_by=created_by)}
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    @app.post("/pantheon/grant/{grant_id}/revoke")
    def revoke(grant_id: str):
        try:
            return {"status": "ok", **pantheon.revoke_hall_grant(grant_id)}
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    @app.get("/pantheon/grants")
    def list_grants(user_id: str, direction: str = "granted"):
        try:
            return {"status": "ok", "grants": pantheon.list_hall_grants(user_id, direction)}
        except Exception as e:
            return {"status": "error", "detail": str(e)}
