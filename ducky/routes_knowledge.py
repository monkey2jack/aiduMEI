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
from ducky.utils import get_facts_conn

logger = logging.getLogger("aiduMEM.RoutesKnowledge")


def _require_scope(user_id: str, bank_id: str):
    uid = (user_id or "").strip()
    bid = (bank_id or "").strip()
    if not uid or not bid:
        raise HTTPException(
            status_code=400,
            detail="user_id 与 bank_id 必须显式提供——缺省/跨域请求一律拒绝（v21 域纪律）")
    return make_scope(uid, bid)


def _memory_visible_in_scope(memory_id: str, conn, user_id: str, bank_id: str) -> bool:
    """该记忆 id 在调用方域内可见吗？（v21.0 收口，生产用户审计 🟡-2 改判据）

    - ``fact:NNN`` 数字 id 可枚举 → **保留域校验**（跨域 404，不暴露存在性）。
    - UUID 不可枚举，且本端点载荷只有 id/关系/溯源（零记忆正文）→
      **链上存在即可见**（此前的 memory_types 预检覆盖率仅 31%，69% 真实
      查询被误杀 404——那是守卫误伤，不是隔离）。
    """
    from ducky.scope_sql import scope_clause
    if memory_id.startswith("fact:"):
        frag, params = scope_clause(make_scope(user_id, bank_id), flavor="canonical")
        try:
            fid = int(memory_id[5:])
        except ValueError:
            return False
        return conn.execute(
            f"SELECT 1 FROM facts WHERE id=? {frag} LIMIT 1", [fid, *params]
        ).fetchone() is not None
    # UUID 形态：演化链上存在即可见
    return conn.execute(
        "SELECT 1 FROM knowledge_evolution WHERE source_id=? OR target_id=? LIMIT 1",
        (memory_id, memory_id)).fetchone() is not None


def register_knowledge_routes(app: FastAPI) -> None:
    @app.get("/knowledge/{memory_id}/evolution")
    def knowledge_evolution(memory_id: str, user_id: str = "", bank_id: str = ""):
        """某条知识的演化链（Replaces/Enriches/Confirms/Challenges）
        + 每个节点的 provenance 三件套（origin_agent/session/turn）。"""
        scope = _require_scope(user_id, bank_id)
        conn = get_facts_conn()
        try:
            visible = _memory_visible_in_scope(memory_id, conn, scope.user_id, scope.bank_id)
            if not visible:
                raise HTTPException(status_code=404, detail="memory not found")
            cur = conn.execute(
                "SELECT * FROM knowledge_evolution WHERE source_id=? OR target_id=? "
                "ORDER BY id ASC", (memory_id, memory_id))
            cols = [d[0] for d in cur.description]
            chain = [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            conn.close()
        # v21.1（众神殿 WP-6）：fact:NNN 经域校验 = 拥有本殿事实 → 完整链。
        # UUID 不可域校验（knowledge_evolution 无殿列，v21.0 裁决保留可见以免 69%
        # 误杀），但链上的 reason 与 origin 三件套可能含内容片段/会话身份——跨殿
        # 人格独立，这些字段一律脱敏，只留关系结构（source/target/relation/confidence）。
        owns = memory_id.startswith("fact:")
        if not owns:
            _SENSITIVE = ("reason", "origin_agent", "origin_session_id", "origin_turn")
            for row in chain:
                for k in _SENSITIVE:
                    if k in row:
                        row[k] = ""
        return {
            "status": "ok",
            "memory_id": memory_id,
            "count": len(chain),
            "chain": chain,
            "redacted": not owns,
        }

    @app.get("/dossier/scope-hint")
    def dossier_scope_hint():
        """控制台导出按钮的域提示：返回本部署的默认域（env 可配的
        DEFAULT_USER_ID / DEFAULT_BANK_ID——与 /facts 等默认调用同一口径，
        不暴露任何额外信息）。/dossier 本体的显式域纪律不变。"""
        from ducky.bank_contract import DEFAULT_BANK_ID
        from ducky.utils import DEFAULT_USER_ID
        return {"status": "ok", "user_id": DEFAULT_USER_ID, "bank_id": DEFAULT_BANK_ID}

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
