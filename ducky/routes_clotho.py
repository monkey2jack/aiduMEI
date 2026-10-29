"""aiduMEM v11 Hyperion 路由。保持业务模块独立，入口只负责组装。"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException

from ducky.core_memory import (
    get_all_blocks,
)
from ducky.utils import DEFAULT_USER_ID
from ducky.bank_contract import DEFAULT_BANK_ID, make_scope


def _authorize_scope(user_id: str, bank_id: str, caller_user_id: str,
                     *, action: str = "read") -> None:
    """Apply the same declared-caller policy used by /search.

    Pantheon grants cover read/export only.  For writes, a distinct caller has
    no grant path; self access and the existing owner-direct path still work.
    The shared API token does not bind a caller to a credential.
    """
    from ducky.pantheon import HallError, authorize_cross_hall
    try:
        authorize_cross_hall(user_id, caller_user_id, bank_id=bank_id,
                             action=action)
    except HallError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


def register_clotho_routes(app: FastAPI) -> None:
    """注册 CoreMemory 只读 API（f0.6+++ WP2 起仅保留 CC hook 用的 GET）。"""

    @app.get("/api/core-memory")
    def api_core_memory_get(
        user_id: str = DEFAULT_USER_ID,
        bank_id: str = DEFAULT_BANK_ID,
        caller_user_id: str = "",
    ):
        scope = make_scope(user_id, bank_id)
        _authorize_scope(scope.user_id, scope.bank_id, caller_user_id)
        return {
            "status": "ok",
            "user_id": scope.user_id,
            "bank_id": scope.bank_id,
            "blocks": get_all_blocks(scope.user_id, scope.bank_id),
        }
