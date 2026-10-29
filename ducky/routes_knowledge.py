"""
ducky.routes_knowledge — v21 知识治理只读端点
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
F2 `GET /knowledge/{id}/evolution`：知识演化链 + 溯源三件套审计。
F3 `GET /dossier`：记忆档案 Markdown 导出（`?download=1` 落盘附件）。

两条端点共享同一域纪律：(user_id, bank_id) 必须显式——
缺省/跨域直接拒，绝不静默落回 default 域。
"""
from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse

from ducky.bank_contract import make_scope

logger = logging.getLogger("aiduMEM.RoutesKnowledge")


def _require_scope(user_id: str, bank_id: str):
    uid = (user_id or "").strip()
    bid = (bank_id or "").strip()
    if not uid or not bid:
        raise HTTPException(
            status_code=400,
            detail="user_id 与 bank_id 必须显式提供——缺省/跨域请求一律拒绝（v21 域纪律）")
    return make_scope(uid, bid)


def register_knowledge_routes(app: FastAPI) -> None:


    @app.get("/dossier", response_class=PlainTextResponse)
    def memory_dossier(user_id: str = "", bank_id: str = "", download: int = 0,
                       caller_user_id: str = ""):
        """导出该域的完整记忆档案（Markdown）。"""
        from ducky.dossier import build_dossier_data, render_markdown
        scope = _require_scope(user_id, bank_id)
        # v21.1 众神殿：跨殿导出须持 export 借阅（caller 空/==user_id 放行=导出自己殿）
        from ducky.pantheon import authorize_cross_hall
        try:
            authorize_cross_hall(scope.user_id, caller_user_id,
                                 bank_id=scope.bank_id, action="export")
        except Exception as e:
            raise HTTPException(status_code=403, detail=str(e))
        data = build_dossier_data(scope.user_id, scope.bank_id)
        text = render_markdown(data)
        headers = {}
        if download:
            fname = f"dossier_{scope.user_id}_{scope.bank_id}.md"
            headers["Content-Disposition"] = f'attachment; filename="{fname}"'
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8", headers=headers)
