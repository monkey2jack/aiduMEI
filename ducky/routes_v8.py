"""
ducky.routes_v8 — v8 五脉 + graduate（C 档从 api_server 抽出）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Ignition · Workspace · Broadcast · J-lens · Session · Instinct graduate

语义与抽出前一致，仅搬家。
"""

from __future__ import annotations

import logging
import os
import threading

from fastapi import FastAPI, HTTPException

from ducky.api_models import SearchRequest
from ducky.bank_contract import (
    DEFAULT_BANK_ID,
    BankScopeError,
    vector_item_in_bank,
    vector_scope_filters,
)
from ducky.utils import DEFAULT_USER_ID
from ducky.mem0_runtime import get_memory
from ducky.api_errors import api_error_detail

logger = logging.getLogger("aiduMEM.routes")

# 会话结束反思默认开启；AIDUMEM_REFLECT_ON_SESSION_END=false 可关闭（手动 /reflect 不受影响）
_REFLECT_ON_SESSION_END = os.environ.get("AIDUMEM_REFLECT_ON_SESSION_END", "true").strip().lower() not in {
    "0", "false", "no", "off",
}


def _trigger_session_end_reflect(user_id: str, bank_id: str = "") -> None:
    """P0-3 接线：会话结束时在后台线程触发一次 Reflect 反思。

    - 不阻塞 /session/end 响应
    - 异常吞掉只记日志，绝不因反思失败影响会话结束
    - 由 AIDUMEM_REFLECT_ON_SESSION_END 控制开关
    - v20 P0-2：反思继承会话的 bank_id，产物落回本域
    """
    if not _REFLECT_ON_SESSION_END:
        return

    def _run():
        try:
            from ducky.reflect import run_reflect
            run_reflect(user_id=user_id, source="session_end", bank_id=bank_id)
        except Exception as e:
            logger.warning(f"会话结束反思触发失败（忽略）: {e}")

    threading.Thread(target=_run, daemon=True, name="aidumem-session-end-reflect").start()


def register_v8_routes(app: FastAPI) -> None:
    # ── Ignition ──────────────────────────────────────
    @app.post("/ignition_test")
    def ignition_test(req: SearchRequest):
        """测试 Ignition 点火效果：返回 ignited/remaining 分列"""
        try:
            mem = get_memory()
            from ducky.memory_ignition import ignition_filter
            # v20 P0-2：与核心 /search 同款——命名域下推、默认域复筛
            raw = mem.search(req.query,
                             filters=vector_scope_filters(req.user_id, req.bank_id),
                             limit=30)
            candidates = raw.get("results", raw) if isinstance(raw, dict) else raw
            if not isinstance(candidates, list):
                candidates = []
            candidates = [c for c in candidates if vector_item_in_bank(c, req.bank_id)]
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
    def workspace_status(user_id: str = DEFAULT_USER_ID, bank_id: str = DEFAULT_BANK_ID):
        """查看活跃记忆工作区状态"""
        try:
            from ducky.memory_workspace import ws_status
            return {"status": "ok", **ws_status(user_id, bank_id)}
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    @app.post("/workspace/clear")
    def workspace_clear(user_id: str = DEFAULT_USER_ID, bank_id: str = DEFAULT_BANK_ID):
        """清空工作区"""
        try:
            from ducky.memory_workspace import ws_clear
            ws_clear(user_id, bank_id)
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
            result = broadcast_chain(mem, req.query, req.user_id,
                                     max_depth=max_depth, bank_id=req.bank_id)
            return {"status": "ok", **result}
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    @app.post("/broadcast_expand")
    def broadcast_expand(memory_id: str, user_id: str = DEFAULT_USER_ID, limit: int = 5,
                         bank_id: str = DEFAULT_BANK_ID):
        """单次广播展开（从 memory_id 出发）"""
        try:
            mem = get_memory()
            from ducky.memory_broadcast import broadcast_expand as _broadcast_expand
            results = _broadcast_expand(mem, memory_id, user_id, limit=limit,
                                        bank_id=bank_id)
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

            raw = mem.search(req.query,
                             filters=vector_scope_filters(req.user_id, req.bank_id),
                             limit=30)
            candidates = raw.get("results", raw) if isinstance(raw, dict) else raw
            if not isinstance(candidates, list):
                candidates = []
            candidates = [c for c in candidates if vector_item_in_bank(c, req.bank_id)]

            ign_result = ignition_filter(req.query, candidates)
            final = ignition_boost_sort(ign_result["ignited"], ign_result["remaining"], req.limit)

            return collect_jlens_report(
                query=req.query,
                ignited=ign_result["ignited"],
                remaining=ign_result["remaining"],
                final=final,
                workspace_status=ws_status(req.user_id, req.bank_id),
                total_ms=ign_result["stats"]["ms"],
            )
        except Exception as e:
            return {"status": "error", "detail": str(e)}

    # ── Session (Persistence) ─────────────────────────
    @app.post("/session/start")
    def session_start(user_id: str = DEFAULT_USER_ID, bank_id: str = DEFAULT_BANK_ID):
        try:
            from ducky.memory_persistence import session_start as _session_start
            return {"status": "ok", **_session_start(user_id, bank_id=bank_id)}
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
            result = _session_end(session_id)
            if result.get("status") == "ok":
                # P0-3 接线：会话结束触发一次 Reflect 反思（后台线程，不阻塞响应）
                # v20 P0-2：反思继承会话作用域，产物落回本域
                _trigger_session_end_reflect(
                    result.get("user_id") or DEFAULT_USER_ID,
                    bank_id=result.get("bank_id") or "",
                )
            return result
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
    def graduate_instincts(user_id: str = DEFAULT_USER_ID, dry_run: bool = False,
                           bank_id: str = ""):
        """Instinct→Skill 自动毕业（注：v9.0.1 已彻底关闭自动生成 auto-*.md）

        v20 P0-2：毕业链整体锁在 (user_id, bank_id) 域内；不传 bank_id = default 域。
        """
        try:
            from ducky.instinct_graduation import auto_graduate, scan_instincts
            mem = get_memory()
            if dry_run:
                groups = scan_instincts(mem, user_id, bank_id=bank_id or DEFAULT_BANK_ID)
                return {"status": "ok", "dry_run": True, "groups": groups, "total_groups": len(groups)}
            result = auto_graduate(mem, user_id, bank_id=bank_id or DEFAULT_BANK_ID)
            return {"status": "ok", **result}
        except BankScopeError as e:
            return {"status": "error", "detail": str(e)}
        except ImportError:
            raise HTTPException(503, "Instinct Graduation 模块未就绪")
        # 🔴v20.3.2（第 10 轮审计 P0-2）：**先透传 HTTPException**。
        # `get_memory()` 在后端未就绪时抛的是 HTTPException(503)，而它是
        # Exception 的子类 —— 没有这一句就会被下面的 except 接住、重包成 500。
        # 结果错误正文里明明白白写着「原始错误：503」，状态码却是 500：
        # 调用方无法区分「服务还没配好（可重试）」与「服务端坏了（要人查）」。
        # 这个惯用法仓里已有 6 处（crud.py:182、routes_octopus.py:99 等），
        # crud.py 那处的注释记的就是同一课（v19.4.1 P1-4 注入拦截 400 被吞成 500）。
        # **惯用法存在不等于被一致套用** —— 漏的那一处恰在新用户必经之路上。
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"graduate 失败: {e}")
            raise HTTPException(500, api_error_detail(e))
