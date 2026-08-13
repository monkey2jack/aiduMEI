"""
ducky.routes_p1 — v19.0 P1 记忆类型分离路由（四网络查询视图）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
P1-1 将混在单池里的记忆按 FACTS / PREFERENCES / EXPERIENCES /
OBSERVATIONS / REFLECTIONS / DECISIONS 六类显式分离。这里提供：
    GET  /memory/types         类型统计与标签
    GET  /memory/types/query   按类型列出事实（join facts 视图）
    POST /memory/types/backfill 存量数据规则重建账本
    POST /memory/types/reset   清空账本
"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict

from ducky.utils import DEFAULT_USER_ID, get_facts_conn

logger = logging.getLogger("aiduMEM.routes_p1")


class BackfillRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    limit: int = 2000


class SkillGrowRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    trajectory: list[str]
    task_name: str = ""
    use_llm: bool = True
    source: str = "manual"


class RefineGroupRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    category: str
    user_id: str = DEFAULT_USER_ID
    limit: int = 20
    use_llm: bool = True


class RefineActionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    refine_id: int


def register_p1_routes(app: FastAPI) -> None:
    from ducky.memory_types import (
        VALID_TYPES,
        backfill_from_facts,
        list_types,
        reset_all_types,
    )

    @app.get("/memory/types")
    def memory_types():
        """六类记忆的类型统计。"""
        try:
            return {"status": "ok", "types": list_types(), "valid_types": sorted(VALID_TYPES)}
        except Exception as e:
            logger.error(f"/memory/types 失败: {e}")
            return {"status": "error", "detail": str(e)}

    @app.get("/memory/types/query")
    def memory_types_query(
        memory_type: str = "FACTS",
        limit: int = 50,
        user_id: str = DEFAULT_USER_ID,
    ):
        """按类型列出已分类的事实（facts 视图；mem0 池在后续 Skill/精炼接入）。"""
        if memory_type not in VALID_TYPES:
            return {"status": "error", "detail": f"memory_type 必须是 {sorted(VALID_TYPES)}"}
        try:
            conn = get_facts_conn()
            rows = conn.execute(
                """
                SELECT f.id, f.category, f.fact_key, f.fact_value, f.valid_from,
                       f.valid_to, f.recorded_at, mt.confidence AS type_confidence
                FROM memory_types mt
                JOIN facts f ON f.id = CAST(substr(mt.memory_ref, 6) AS INTEGER)
                WHERE mt.memory_type = ? AND f.archived = 0
                ORDER BY f.updated_at DESC LIMIT ?
                """,
                (memory_type, max(1, min(int(limit), 200))),
            ).fetchall()
            conn.close()
            return {
                "status": "ok",
                "memory_type": memory_type,
                "count": len(rows),
                "facts": [dict(r) for r in rows],
            }
        except Exception as e:
            logger.error(f"/memory/types/query 失败: {e}")
            return {"status": "error", "detail": str(e)}

    @app.post("/memory/types/backfill")
    def memory_types_backfill(req: BackfillRequest):
        """对存量 facts 做规则判型重建账本（不调用 LLM）。"""
        try:
            result = backfill_from_facts(limit=req.limit)
            return {"status": "ok", **result}
        except Exception as e:
            logger.error(f"/memory/types/backfill 失败: {e}")
            return {"status": "error", "detail": str(e)}

    @app.post("/memory/types/reset")
    def memory_types_reset():
        """清空类型账本（用于重建或测试）。"""
        try:
            deleted = reset_all_types()
            return {"status": "ok", "deleted": deleted}
        except Exception as e:
            logger.error(f"/memory/types/reset 失败: {e}")
            return {"status": "error", "detail": str(e)}

    # ── P1-2 自动 Skill 生长 ────────────────────────────────────
    @app.post("/skill/grow")
    def skill_grow(req: SkillGrowRequest):
        """从任务轨迹生成技能草稿（status=draft，需人工 approve）。"""
        from ducky.skill_growth import grow_skill_from_trajectory

        try:
            return grow_skill_from_trajectory(
                req.trajectory,
                task_name=req.task_name,
                use_llm=req.use_llm,
                source=req.source,
            )
        except Exception as e:
            logger.error(f"/skill/grow 失败: {e}")
            return {"status": "error", "detail": str(e)}

    @app.get("/skill/drafts")
    def skill_drafts(status: str = "draft"):
        """列出技能草稿。"""
        from ducky.skill_growth import list_skill_drafts

        try:
            return {"status": "ok", "skills": list_skill_drafts(status=status)}
        except Exception as e:
            logger.error(f"/skill/drafts 失败: {e}")
            return {"status": "error", "detail": str(e)}

    # ── P1-3 记忆递归精炼 ───────────────────────────────────────
    @app.post("/memory/refine")
    def memory_refine(req: RefineGroupRequest):
        """对指定 category 做一次递归精炼（proposed，不自动应用）。"""
        from ducky.refine_memory import refine_group

        try:
            return refine_group(
                req.user_id,
                req.category,
                limit=req.limit,
                use_llm=req.use_llm,
            )
        except Exception as e:
            logger.error(f"/memory/refine 失败: {e}")
            return {"status": "error", "detail": str(e)}

    @app.get("/memory/refinements")
    def memory_refinements(user_id: str = DEFAULT_USER_ID, state: str = "proposed", limit: int = 20):
        """列出递归精炼账本。"""
        from ducky.refine_memory import list_refinements

        try:
            return {"status": "ok", "refinements": list_refinements(user_id=user_id, state=state, limit=limit)}
        except Exception as e:
            logger.error(f"/memory/refinements 失败: {e}")
            return {"status": "error", "detail": str(e)}

    @app.post("/memory/refine/apply")
    def memory_refine_apply(req: RefineActionRequest):
        """应用一次精炼（把源记忆 soft-superseded 归档）。"""
        from ducky.refine_memory import apply_refinement

        try:
            return apply_refinement(req.refine_id)
        except Exception as e:
            logger.error(f"/memory/refine/apply 失败: {e}")
            return {"status": "error", "detail": str(e)}

    @app.post("/memory/refine/rollback")
    def memory_refine_rollback(req: RefineActionRequest):
        """回滚一次精炼（恢复被归档的源记忆）。"""
        from ducky.refine_memory import rollback_refinement

        try:
            return rollback_refinement(req.refine_id)
        except Exception as e:
            logger.error(f"/memory/refine/rollback 失败: {e}")
            return {"status": "error", "detail": str(e)}
