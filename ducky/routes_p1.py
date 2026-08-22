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
from ducky.bank_contract import DEFAULT_BANK_ID, make_scope, visible_user_clause

logger = logging.getLogger("aiduMEM.routes_p1")


class BackfillRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    limit: int = 2000
    user_id: str = DEFAULT_USER_ID
    bank_id: str = DEFAULT_BANK_ID


class TypeResetRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    user_id: str = DEFAULT_USER_ID
    bank_id: str = DEFAULT_BANK_ID
    # Destructive all-scope cleanup is deliberately not exposed by default.
    all_scopes: bool = False


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
        ensure_memory_types_schema,
        list_types,
        reset_all_types,
    )

    @app.get("/memory/types")
    def memory_types(
        user_id: str = DEFAULT_USER_ID,
        bank_id: str = DEFAULT_BANK_ID,
    ):
        """六类记忆的类型统计。"""
        try:
            ensure_memory_types_schema()
            scope = make_scope(user_id, bank_id)
            return {
                "status": "ok",
                "user_id": scope.user_id,
                "bank_id": scope.bank_id,
                "types": list_types(scope.user_id, scope.bank_id),
                "valid_types": sorted(VALID_TYPES),
            }
        except Exception as e:
            logger.error(f"/memory/types 失败: {e}")
            return {"status": "error", "detail": str(e)}

    @app.get("/memory/types/query")
    def memory_types_query(
        memory_type: str = "FACTS",
        limit: int = 50,
        user_id: str = DEFAULT_USER_ID,
        bank_id: str = DEFAULT_BANK_ID,
    ):
        """按类型列出已分类的事实（facts 视图；mem0 池在后续 Skill/精炼接入）。"""
        if memory_type not in VALID_TYPES:
            return {"status": "error", "detail": f"memory_type 必须是 {sorted(VALID_TYPES)}"}
        try:
            ensure_memory_types_schema()
            conn = get_facts_conn()
            scope = make_scope(user_id, bank_id)
            # 🔴v20.0：JOIN 的**两侧**都要放宽租户口径，少放一侧等于没放 ——
            # 账本行和事实行都是 ALTER TABLE 一次性写满的字面量 ``default``，
            # 改过名的部署上任一侧精确匹配都会把整个结果集打成空。这是用户直接
            # 看得见的接口：查出来 count=0，像是「类型账本没记过」，其实记过。
            # bank 轴保持精确相等（不可被环境变量改名，放宽就是跨库串味）。
            mt_owner_sql, mt_owner_params = visible_user_clause(scope.user_id, alias="mt")
            f_owner_sql, f_owner_params = visible_user_clause(scope.user_id, alias="f")
            rows = conn.execute(
                f"""
                SELECT f.id, f.category, f.fact_key, f.fact_value, f.valid_from,
                       f.valid_to, f.recorded_at, mt.confidence AS type_confidence
                FROM memory_types mt
                JOIN facts f ON f.id = CAST(substr(mt.memory_ref_raw, 6) AS INTEGER)
                WHERE mt.memory_type = ? AND f.archived = 0
                  AND {mt_owner_sql} AND mt.bank_id = ?
                  AND {f_owner_sql} AND f.bank_id = ?
                ORDER BY f.updated_at DESC LIMIT ?
                """,
                (
                    memory_type,
                    *mt_owner_params,
                    scope.bank_id,
                    *f_owner_params,
                    scope.bank_id,
                    max(1, min(int(limit), 200)),
                ),
            ).fetchall()
            conn.close()
            return {
                "status": "ok",
                "memory_type": memory_type,
                "user_id": scope.user_id,
                "bank_id": scope.bank_id,
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
            scope = make_scope(req.user_id, req.bank_id)
            result = backfill_from_facts(
                limit=req.limit, user_id=scope.user_id, bank_id=scope.bank_id
            )
            return {"status": "ok", "user_id": scope.user_id, "bank_id": scope.bank_id, **result}
        except Exception as e:
            logger.error(f"/memory/types/backfill 失败: {e}")
            return {"status": "error", "detail": str(e)}

    @app.post("/memory/types/reset")
    def memory_types_reset(req: TypeResetRequest | None = None):
        """清空指定 bank 类型账本（用于重建或测试）。"""
        try:
            req = req or TypeResetRequest()
            scope = make_scope(req.user_id, req.bank_id)
            deleted = reset_all_types(
                scope.user_id,
                scope.bank_id,
                all_scopes=bool(req.all_scopes),
            )
            return {
                "status": "ok",
                "user_id": scope.user_id,
                "bank_id": scope.bank_id,
                "deleted": deleted,
            }
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
