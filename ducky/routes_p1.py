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
import sqlite3
from typing import Annotated, Literal

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ducky.api_models import (
    ID_FIELD_MAX_CHARS,
)

from ducky.utils import DEFAULT_USER_ID, get_facts_conn
from ducky.bank_contract import DEFAULT_BANK_ID, make_scope, visible_user_clause

logger = logging.getLogger("aiduMEM.routes_p1")


class BackfillRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    limit: int = Field(default=2000, ge=1, le=5000)
    user_id: str = Field(default=DEFAULT_USER_ID, max_length=ID_FIELD_MAX_CHARS)
    bank_id: str = Field(default=DEFAULT_BANK_ID, max_length=ID_FIELD_MAX_CHARS)
    source: Literal["facts", "mem0"] = "facts"
    after_id: int = Field(default=0, ge=0)
    # Qdrant point offsets are UUID strings or unsigned integer ids.  Bound
    # both variants before handing a public request to the scroll backend.
    cursor: (Annotated[str, StringConstraints(strict=True, max_length=128)] |
             Annotated[int, Field(ge=0, le=18446744073709551615, strict=True)] | None) = None
    apply: bool = False


class TypeResetRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    user_id: str = DEFAULT_USER_ID
    bank_id: str = DEFAULT_BANK_ID
    # v20.2.4（外审 F-06）：`all_scopes` **已从 HTTP 契约里移除**。
    #
    # 它本来的定位是「离线维护逃生口」，但字段留在 model 上、路由又
    # 原样透传，于是一个普通 POST 就能清空**全部域**的类型账本
    # （实测：两 bank 各一行 → 请求 all_scopes=true → deleted=2，账本清空）。
    # 注释写着「deliberately not exposed by default」，而它其实是暴露的 ——
    # **注释不是能力边界**。
    #
    # 底层 reset_all_types(all_scopes=...) 的能力保留给不注册到 HTTP 的
    # 离线 CLI；这里连字段都不给，request body 里写了也没有任何东西去读它。


class SkillGrowRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    trajectory: list[str] = Field(..., max_length=10_000)
    task_name: str = Field(default="", max_length=1024)
    use_llm: bool = True
    source: str = Field(default="manual", max_length=ID_FIELD_MAX_CHARS)


class RefineGroupRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    category: str = Field(..., max_length=ID_FIELD_MAX_CHARS)
    user_id: str = Field(default=DEFAULT_USER_ID, max_length=ID_FIELD_MAX_CHARS)
    # v20.2.4（外审 F-10）：此前**没有这个字段**，于是跨 bank 合并
    bank_id: str = Field(default=DEFAULT_BANK_ID, max_length=ID_FIELD_MAX_CHARS)
    limit: int = 20
    use_llm: bool = True


class RefineActionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    refine_id: int
    # v20.2.4（外审 F-10）：apply / rollback **不许只凭自增 ID 执行**。
    # 声明了 scope 就严格匹配；缺省保持既有管理员语义（与治理 F-08 同口径）。
    user_id: str = Field(default="", max_length=ID_FIELD_MAX_CHARS)
    bank_id: str = Field(default="", max_length=ID_FIELD_MAX_CHARS)


def _types_query_memories(conn, memory_type: str, scope, mt_owner_sql: str,
                          mt_owner_params, limit: int) -> list[dict]:
    """f0.3 (C5): mem0-keyed ledger rows of one type, resolved by their own key.

    A mem0 memory has no facts row; its text lives in text_fts.memories under
    the scoped storage key (bare id in the default bank).  Never derive a
    facts id from a UUID.
    """
    refs = conn.execute(
        f"SELECT mt.memory_ref_raw, mt.confidence FROM memory_types mt "
        f"WHERE mt.memory_type = ? AND {mt_owner_sql} AND mt.bank_id = ? "
        f"AND mt.memory_ref_raw IS NOT NULL AND mt.memory_ref_raw != '' "
        f"AND mt.memory_ref_raw NOT GLOB 'fact:*' "
        f"ORDER BY mt.updated_at DESC LIMIT ?",
        (memory_type, *mt_owner_params, scope.bank_id, limit),
    ).fetchall()
    if not refs:
        return []
    from ducky.bank_contract import scoped_storage_key
    from ducky.utils import get_text_conn
    by_key = {scoped_storage_key(r[0], scope): (str(r[0]), r[1]) for r in refs}
    found: dict = {}
    try:
        f_owner_sql, f_owner_params = visible_user_clause(scope.user_id)
        tconn = get_text_conn()
        for key in by_key:                # <= 200 primary-key lookups, local
            row = tconn.execute(
                f"SELECT id, content, category FROM memories WHERE id = ? "
                f"AND {f_owner_sql} AND bank_id = ?",
                (key, *f_owner_params, scope.bank_id)).fetchone()
            if row is not None:
                found[str(row[0])] = (row[1], row[2])
    except (sqlite3.Error, ImportError, ValueError) as exc:  # index unavailable: refs stay unresolved
        logger.warning(f"/memory/types/query 记忆正文解析跳过: {exc}")
    out = []
    for key, (raw, confidence) in by_key.items():
        content, category = found.get(key, (None, None))
        out.append({"memory_id": raw, "content": content, "category": category,
                    "type_confidence": confidence, "resolved": key in found})
    return out


def register_p1_routes(app: FastAPI) -> None:
    from ducky.memory_types import (
        VALID_TYPES,
        backfill_from_facts,
        backfill_from_mem0,
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
            capped = max(1, min(int(limit), 200))
            # f0.3 (C5 / S-5): the ledger holds two disjoint key spaces --
            # `fact:<int>` (facts rowid, from backfill) and mem0 UUIDs (the
            # write path).  The old join CAST(substr(ref, 6)) turned a UUID
            # like '550e8400-…' into 400 and returned unrelated fact #400
            # (~60% of UUIDs start with digits after position 5).  Only a
            # strict `fact:<digits>` ref may join facts; UUID refs resolve
            # through the text index of mem0 memories (_types_query_memories).
            rows = conn.execute(
                f"""
                SELECT f.id, f.category, f.fact_key, f.fact_value, f.valid_from,
                       f.valid_to, f.recorded_at, mt.confidence AS type_confidence
                FROM memory_types mt
                JOIN facts f ON mt.memory_ref_raw GLOB 'fact:[0-9]*'
                            AND substr(mt.memory_ref_raw, 6) NOT GLOB '*[^0-9]*'
                            AND f.id = CAST(substr(mt.memory_ref_raw, 6) AS INTEGER)
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
                    capped,
                ),
            ).fetchall()
            memories = _types_query_memories(conn, memory_type, scope,
                                             mt_owner_sql, mt_owner_params, capped)
            conn.close()
            return {
                "status": "ok",
                "memory_type": memory_type,
                "user_id": scope.user_id,
                "bank_id": scope.bank_id,
                "count": len(rows),
                "facts": [dict(r) for r in rows],
                "memory_count": len(memories),
                "memories": memories,
            }
        except Exception as e:
            logger.error(f"/memory/types/query 失败: {e}")
            return {"status": "error", "detail": str(e)}

    @app.post("/memory/types/backfill")
    def memory_types_backfill(req: BackfillRequest):
        """分页预览存量六型分类；显式 apply 才落盘（不调用 LLM）。"""
        try:
            scope = make_scope(req.user_id, req.bank_id)
            if req.source == "mem0":
                result = backfill_from_mem0(
                    limit=req.limit, user_id=scope.user_id, bank_id=scope.bank_id,
                    cursor=req.cursor, apply=req.apply,
                )
            else:
                result = backfill_from_facts(
                    limit=req.limit, user_id=scope.user_id, bank_id=scope.bank_id,
                    after_id=req.after_id, apply=req.apply,
                )
            partial = result.get("failed", 0) or result.get("conflicts", 0)
            return {"status": "partial" if partial else "ok", "source": req.source,
                    "user_id": scope.user_id, "bank_id": scope.bank_id, **result}
        except Exception as e:
            logger.error(f"/memory/types/backfill 失败: {e}")
            return {"status": "error", "detail": str(e)}

    @app.post("/memory/types/reset")
    def memory_types_reset(req: TypeResetRequest | None = None):
        """清空指定 bank 类型账本（用于重建或测试）。"""
        try:
            req = req or TypeResetRequest()
            scope = make_scope(req.user_id, req.bank_id)
            # 恒 False：HTTP 面永不打开全域清空（外审 F-06）。
            # model_config 是 extra="allow"，所以 body 里的 all_scopes 会被
            # 当额外字段收下 —— 但这里**不读它**，写了也没用。
            deleted = reset_all_types(
                scope.user_id,
                scope.bank_id,
                all_scopes=False,
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
                bank_id=req.bank_id,      # v20.2.4 F-10：此前断在这里
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
            return apply_refinement(req.refine_id,
                                    user_id=req.user_id, bank_id=req.bank_id)
        except Exception as e:
            logger.error(f"/memory/refine/apply 失败: {e}")
            return {"status": "error", "detail": str(e)}

    @app.post("/memory/refine/rollback")
    def memory_refine_rollback(req: RefineActionRequest):
        """回滚一次精炼（恢复被归档的源记忆）。"""
        from ducky.refine_memory import rollback_refinement

        try:
            return rollback_refinement(req.refine_id,
                                       user_id=req.user_id, bank_id=req.bank_id)
        except Exception as e:
            logger.error(f"/memory/refine/rollback 失败: {e}")
            return {"status": "error", "detail": str(e)}
