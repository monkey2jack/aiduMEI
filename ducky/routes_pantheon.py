"""ducky.routes_pantheon — 众神殿殿管理 + 跨殿借阅端点 (v21.1)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
殿 = user_id。端点全部把 pantheon 层的 HallError（及其它异常）包成
{"status":"error","detail":...}，不抛 500（与 v8 路由约定一致）。
定位①：单主人多分身——这些端点是主人管理自己麾下诸殿与它们之间的借阅。

v22.0（雷霆审计 A1）：管理面接调用者鉴权，对齐联邦层（T-06/T-08 同类）。
- grant/revoke/deactivate：须「本人或 admin」，空 caller 返回 status:error
- list_grants：非 admin 只能看自己相关（caller 自限）
- create_hall/get_hall/list_halls：按 v21.1 兼容红线保留旧语义
"""
from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI

from ducky import pantheon

logger = logging.getLogger("aiduMEM.RoutesPantheon")


def _err(detail: str, code: str = "Error", retryable: bool = False) -> dict:
    return {"status": "error", "detail": detail, "error_code": code, "retryable": retryable}


def _admins() -> frozenset[str]:
    raw = os.environ.get("AIDUMEI_FEDERATION_ADMINS", "")
    return frozenset(a.strip() for a in raw.split(",") if a.strip())


def _is_admin(caller: str) -> bool:
    return bool(caller) and caller in _admins()


def _try_admin_or_owner(owner: str, caller: str) -> dict | None:
    """返回 None=通过；返回 dict=应作为响应。"""
    if not caller:
        return _err("caller 必填（v22.0 起管理面零匿名）", code="Unauthorized")
    if caller != owner and not _is_admin(caller):
        return _err(f"caller({caller}) 非 owner({owner}) 且非 admin", code="Forbidden")
    return None


def register_pantheon_routes(app: FastAPI) -> None:

    # ── 殿注册表 ──────────────────────────────────────────────
    @app.post("/pantheon/hall")
    def create_hall(user_id: str, display_name: str = "", description: str = "", caller: str = ""):
        try:
            if caller:
                description = f"{description} [by:{caller}]" if description else f"[by:{caller}]"
            return {"status": "ok", "hall": pantheon.create_hall(
                user_id, display_name=display_name, description=description)}
        except Exception as e:
            return _err(str(e))

    @app.get("/pantheon/halls")
    def list_halls(include_inactive: bool = False):
        try:
            return {"status": "ok", "halls": pantheon.list_halls(include_inactive)}
        except Exception as e:
            return _err(str(e))

    @app.get("/pantheon/hall/{user_id}")
    def get_hall(user_id: str):
        try:
            hall = pantheon.get_hall(user_id)
            if hall is None:
                return _err("殿不存在", code="NotFound")
            return {"status": "ok", "hall": hall}
        except Exception as e:
            return _err(str(e))

    @app.post("/pantheon/hall/{user_id}/deactivate")
    def deactivate_hall(user_id: str, caller: str = ""):
        try:
            deny = _try_admin_or_owner(user_id, caller)
            if deny:
                return deny
            return {"status": "ok", **pantheon.deactivate_hall(user_id)}
        except Exception as e:
            return _err(str(e))

    # ── 跨殿借阅 ──────────────────────────────────────────────
    @app.post("/pantheon/grant")
    def grant(grantor_user_id: str, grantee_user_id: str, actions: str = "read",
              bank_id: str = "*", expires_at: str = "", created_by: str = "", caller: str = ""):
        try:
            deny = _try_admin_or_owner(grantor_user_id, caller)
            if deny:
                return deny
            return {"status": "ok", "grant": pantheon.grant_hall_access(
                grantor_user_id, grantee_user_id, actions=actions, bank_id=bank_id,
                expires_at=(expires_at or None), created_by=(caller or created_by))}
        except Exception as e:
            return _err(str(e))

    @app.post("/pantheon/grant/{grant_id}/revoke")
    def revoke(grant_id: str, caller: str = ""):
        try:
            if not _is_admin(caller):
                return _err(f"revoke 须 admin（caller={caller or '空'}）", code="Forbidden")
            return {"status": "ok", **pantheon.revoke_hall_grant(grant_id)}
        except Exception as e:
            return _err(str(e))

    @app.get("/pantheon/grants")
    def list_grants(user_id: str, direction: str = "granted", caller: str = ""):
        try:
            if not _is_admin(caller) and caller != user_id:
                return _err(f"caller({caller}) 只能看自己相关授权", code="Forbidden")
            return {"status": "ok", "grants": pantheon.list_hall_grants(user_id, direction)}
        except Exception as e:
            return _err(str(e))
