"""
ducky.routes_v8 — v8 五脉 + graduate（C 档从 api_server 抽出）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Ignition · Workspace · Broadcast · J-lens · Session · Instinct graduate

语义与抽出前一致，仅搬家。
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException

from ducky.api_models import SearchRequest
from ducky.utils import DEFAULT_USER_ID
from ducky.mem0_runtime import get_memory

logger = logging.getLogger("aiduMEM.routes")


def register_v8_routes(app: FastAPI) -> None:
    # ── Ignition ──────────────────────────────────────
    @app.post("/ignition_test")
    def ignition_test(req: SearchRequest):
        """测试 Ignition 点火效果：返回 ignited/remaining 分列"""
        try:
            mem = get_memory()
            from ducky.memory_ignition import ignition_filter
            raw = mem.search(req.query, filters={"user_id": req.user_id}, limit=30)
            candidates = raw.get("results", raw) if isinstance(raw, dict) else raw
            if not isinstance(candidates, list):
                candidates = []
            result = ignition_filter(req.query, candidates)
            return {
                "status": "ok",
                "stats": result["stats"],
                "ignited_preview": [
                    {"text": r.get("memory", "")[:80], "score": r.get("_ignition_score", 0)}
                    for r in result["ignited"][:5]
                ],
            }
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    # ── Workspace ─────────────────────────────────────
    @app.get("/workspace")
    def workspace_status(user_id: str = DEFAULT_USER_ID):
        """查看活跃记忆工作区状态"""
        try:
            from ducky.memory_workspace import ws_status
            return {"status": "ok", **ws_status(user_id)}
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    @app.post("/workspace/clear")
    def workspace_clear(user_id: str = DEFAULT_USER_ID):
        """清空工作区"""
        try:
            from ducky.memory_workspace import ws_clear
            ws_clear(user_id)
            return {"status": "ok"}
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    # ── Broadcast ─────────────────────────────────────
    @app.post("/recall_chain")
    def recall_chain(req: SearchRequest, max_depth: int = 3):
        """记忆广播链：从一条查询出发，发现关联记忆（3 层传播）"""
        try:
            mem = get_memory()
            from ducky.memory_broadcast import broadcast_chain
            result = broadcast_chain(mem, req.query, req.user_id, max_depth=max_depth)
            return {"status": "ok", **result}
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    @app.post("/broadcast_expand")
    def broadcast_expand(memory_id: str, user_id: str = DEFAULT_USER_ID, limit: int = 5):
        """单次广播展开（从 memory_id 出发）"""
        try:
            mem = get_memory()
            from ducky.memory_broadcast import broadcast_expand as _broadcast_expand
            results = _broadcast_expand(mem, memory_id, user_id, limit=limit)
            return {"status": "ok", "results": results, "count": len(results)}
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    # ── J-lens ────────────────────────────────────────
    @app.post("/jlens")
    def jlens_report(req: SearchRequest):
        """收集完整的 J-lens 审计报告（Ignition + 管道 + 距离矩阵 + Workspace 快照）"""
        try:
            mem = get_memory()
            from ducky.memory_ignition import ignition_filter, ignition_boost_sort
            from ducky.memory_jlens import collect_jlens_report
            from ducky.memory_workspace import ws_status

            raw = mem.search(req.query, filters={"user_id": req.user_id}, limit=30)
            candidates = raw.get("results", raw) if isinstance(raw, dict) else raw
            if not isinstance(candidates, list):
                candidates = []

            ign_result = ignition_filter(req.query, candidates)
            final = ignition_boost_sort(ign_result["ignited"], ign_result["remaining"], req.limit)

            return collect_jlens_report(
                query=req.query,
                ignited=ign_result["ignited"],
                remaining=ign_result["remaining"],
                final=final,
                workspace_status=ws_status(req.user_id),
                total_ms=ign_result["stats"]["ms"],
            )
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    # ── Session (Persistence) ─────────────────────────
    @app.post("/session/start")
    def session_start(user_id: str = DEFAULT_USER_ID):
        try:
            from ducky.memory_persistence import session_start as _session_start
            return {"status": "ok", **_session_start(user_id)}
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    @app.post("/session/search")
    def session_search(req: SearchRequest, session_id: str = "", use_context: bool = True):
        if not session_id:
            return {"status": "error", "detail": "需要 session_id"}
        try:
            mem = get_memory()
            from ducky.memory_persistence import session_search as _session_search
            return _session_search(mem, session_id, req.query, req.limit, use_context)
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    @app.post("/session/pin")
    def session_pin(session_id: str, memory_id: str):
        try:
            from ducky.memory_persistence import session_pin as _session_pin
            return _session_pin(session_id, memory_id)
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    @app.post("/session/unpin")
    def session_unpin(session_id: str, memory_id: str):
        try:
            from ducky.memory_persistence import session_unpin as _session_unpin
            return _session_unpin(session_id, memory_id)
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    @app.get("/session/report")
    def session_report(session_id: str):
        try:
            from ducky.memory_persistence import session_report as _session_report
            return _session_report(session_id)
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    @app.post("/session/end")
    def session_end(session_id: str):
        try:
            from ducky.memory_persistence import session_end as _session_end
            return _session_end(session_id)
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    @app.get("/session/list")
    def session_list(user_id: str = None):
        try:
            from ducky.memory_persistence import session_list as _session_list
            return {"status": "ok", "sessions": _session_list(user_id)}
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    # ── Instinct→Skill 毕业 ───────────────────────────
    @app.post("/graduate")
    def graduate_instincts(user_id: str = DEFAULT_USER_ID, dry_run: bool = False):
        """Instinct→Skill 自动毕业（注：v9.0.1 已彻底关闭自动生成 auto-*.md）"""
        try:
            from ducky.instinct_graduation import auto_graduate, scan_instincts
            mem = get_memory()
            if dry_run:
                groups = scan_instincts(mem, user_id)
                return {"status": "ok", "dry_run": True, "groups": groups, "total_groups": len(groups)}
            result = auto_graduate(mem, user_id)
            return {"status": "ok", **result}
        except ImportError:
            raise HTTPException(503, "Instinct Graduation 模块未就绪")
        except Exception as e:
            logger.error(f"graduate 失败: {e}")
            raise HTTPException(500, str(e))
