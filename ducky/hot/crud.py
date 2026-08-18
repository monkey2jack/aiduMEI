"""ducky.hot.crud — recent/stats/delete/update/usage/reload/inject"""
from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException

from ducky.api_models import (
    DeleteAllRequest,
    DeleteRequest,
    GovernanceReviewRequest,
    InjectContextRequest,
    OpinionSetRequest,
    TombstoneRestoreRequest,
    UpdateRequest,
)
from ducky.utils import DEFAULT_USER_ID
from ducky.mem0_runtime import (
    _normalize_user_id,
    get_llm_usage,
    get_memory,
    reset_memory_singleton,
)
from ducky.wal_engine import cascade_delete_memory, cascade_delete_all

logger = logging.getLogger("aiduMEM.hot")


def register_crud_routes(app: FastAPI) -> None:
    @app.get("/recent")
    def recent(user_id: str = DEFAULT_USER_ID, limit: int = 10):
        try:
            mem = get_memory()
            results = mem.get_all(filters={"user_id": user_id}, limit=limit)
            return {"status": "ok", "results": results}
        except Exception as e:
            logger.error(f"recent 失败: {e}")
            raise HTTPException(500, str(e))

    @app.get("/stats")
    def stats(user_id: str = DEFAULT_USER_ID):
        try:
            mem = get_memory()
            all_mem = mem.get_all(filters={"user_id": user_id}, limit=10000)
            results = all_mem.get("results", []) if isinstance(all_mem, dict) else (all_mem or [])

            total = len(results)
            hash_counts: dict = {}
            user_counts: dict = {}
            tag_counts: dict = {}

            for item in results:
                h = item.get("hash", "")
                uid = item.get("user_id", user_id)
                mem_text = item.get("memory", "")
                user_counts[uid] = user_counts.get(uid, 0) + 1
                if h:
                    hash_counts[h] = hash_counts.get(h, 0) + 1
                if mem_text and mem_text.startswith("["):
                    tag = mem_text.split("]")[0] + "]"
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1

            dupes = {h: c for h, c in hash_counts.items() if c > 1}
            total_dupes = sum(c - 1 for c in dupes.values())

            # --- 多模态记忆统计 (v18.3) ---
            vision_count = 0
            obsidian_count = 0
            try:
                from ducky.utils import get_facts_conn
                conn = get_facts_conn()
                vision_count = conn.execute("SELECT COUNT(*) FROM facts WHERE media_url IS NOT NULL").fetchone()[0]
                obsidian_count = conn.execute("SELECT COUNT(*) FROM facts WHERE source = 'obsidian'").fetchone()[0]
                conn.close()
            except Exception as _e:
                logger.warning(f"统计多模态/obsidian数据异常: {_e}")

            return {
                "status": "ok",
                "total": total,
                "total_memories": total,
                "duplicate_count": total_dupes,
                "user_id": user_id,
                "user_distribution": user_counts,
                "unique_hashes": len(hash_counts),
                "duplicate_hashes": len(dupes),
                "after_dedup": total - total_dupes,
                "top_tags": dict(sorted(tag_counts.items(), key=lambda x: -x[1])[:10]),
                "vision_count": vision_count,
                "obsidian_count": obsidian_count,
                "memories": all_mem,
            }
        except Exception as e:
            logger.error(f"stats 失败: {e}")
            raise HTTPException(500, str(e))

    @app.post("/delete")
    def delete(req: DeleteRequest):
        # 🔴P0-1: 传递并严格校验 user_id 归属，杜绝跨租户越权删除
        if not req.memory_id or not req.memory_id.strip():
            raise HTTPException(400, "memory_id 不能为空")
        try:
            user_id = _normalize_user_id(req.user_id) if req.user_id else DEFAULT_USER_ID
            res = cascade_delete_memory(req.memory_id, user_id=user_id)
            return {"status": "ok", "details": res.get("details", {})}
        except Exception as e:
            logger.error(f"delete 失败: {e}")
            raise HTTPException(500, str(e))

    @app.post("/delete_all")
    def delete_all(req: DeleteAllRequest):
        # 🔴P0-3: 强制显式指定 user_id，清空 default 全库必须二次确认 confirm=True
        if not req.user_id or not req.user_id.strip():
            raise HTTPException(400, "user_id 必须显式指定，拒绝空参数清库")
        user_id = _normalize_user_id(req.user_id)
        if user_id == DEFAULT_USER_ID and not getattr(req, "confirm", False):
            raise HTTPException(400, "清空默认用户(default)全部记忆具有破坏性，必须传递 confirm: true 二次确认")

        try:
            res = cascade_delete_all(user_id=user_id, confirm=getattr(req, "confirm", False))
            return {"status": "ok", "details": res.get("details", {})}
        except Exception as e:
            logger.error(f"delete_all 失败: {e}")
            raise HTTPException(500, str(e))

    # 🪦 tombstone 遗忘层（v19.4.0 Mímir 借鉴 B3）：遗忘不是删除，留痕可恢复
    @app.get("/tombstones")
    def tombstones(user_id: str = DEFAULT_USER_ID, limit: int = 50):
        """列某租户的遗忘记录（全文与撤回理由可查）"""
        try:
            from ducky.tombstone import list_tombstones
            uid = _normalize_user_id(user_id) if user_id else DEFAULT_USER_ID
            return {"status": "ok", "results": list_tombstones(uid, limit=limit)}
        except Exception as e:
            logger.error(f"tombstones 失败: {e}")
            raise HTTPException(500, str(e))

    @app.post("/tombstone/restore")
    def tombstone_restore(req: TombstoneRestoreRequest):
        """从 tombstone 快照一键恢复一条记忆"""
        if not req.tombstone_id:
            raise HTTPException(400, "tombstone_id 不能为空")
        try:
            from ducky.tombstone import restore_tombstone
            uid = _normalize_user_id(req.user_id) if req.user_id else DEFAULT_USER_ID
            res = restore_tombstone(req.tombstone_id, user_id=uid)
            return {"status": "ok" if res.get("restored") else "noop", "details": res}
        except Exception as e:
            logger.error(f"tombstone/restore 失败: {e}")
            raise HTTPException(500, str(e))

    # 📒 事件溯源账本（v19.4.0 Mímir 借鉴 B5）：任意记忆的完整变更史可查
    @app.get("/events/history")
    def events_history(target_id: str = "", limit: int = 100):
        """查某条记忆的完整变更史（谁、何时、做了什么、为什么）"""
        if not target_id or not target_id.strip():
            raise HTTPException(400, "target_id 不能为空")
        try:
            from ducky.event_ledger import get_history
            return {"status": "ok", "results": get_history(target_id.strip(), limit=limit)}
        except Exception as e:
            logger.error(f"events/history 失败: {e}")
            raise HTTPException(500, str(e))

    # 🏛️ 治理管线（v19.4.0 Mímir 借鉴 B1）：候选队列 + 人审入口
    @app.get("/governance/candidates")
    def governance_candidates(status: str = "", user_id: str = "", limit: int = 50):
        """候选事实队列（可按状态过滤：pending/evaluated/approved/rejected/committed）"""
        try:
            from ducky.governance import list_candidates
            return {"status": "ok", "results": list_candidates(status, user_id, limit)}
        except Exception as e:
            logger.error(f"governance/candidates 失败: {e}")
            raise HTTPException(500, str(e))

    @app.post("/governance/review")
    def governance_review(req: GovernanceReviewRequest):
        """人审裁决：approve/reject 一条候选，带 reason 留痕"""
        if not req.candidate_id:
            raise HTTPException(400, "candidate_id 不能为空")
        if req.decision not in ("approve", "reject"):
            raise HTTPException(400, "decision 必须是 approve 或 reject")
        try:
            from ducky.governance import review_candidate
            res = review_candidate(req.candidate_id, req.decision,
                                   reason=req.reason, user_id=req.user_id)
            return {"status": "ok", "details": res}
        except Exception as e:
            logger.error(f"governance/review 失败: {e}")
            raise HTTPException(500, str(e))

    # 🧭 信念层 Opinion（v19.4.0 Mímir 借鉴 B6）：三态信念写入 + 聚合判定
    @app.post("/opinions/set")
    def opinion_set(req: OpinionSetRequest):
        """写入一条信念（support/oppose/neutral 三态皆可），账本留痕"""
        if not req.fact_id:
            raise HTTPException(400, "fact_id 不能为空")
        if not req.source or not req.source.strip():
            raise HTTPException(400, "source（证据来源标识）不能为空")
        try:
            from ducky.opinion import set_opinion
            res = set_opinion(req.fact_id, req.stance, confidence=req.confidence,
                              evidence_ids=req.evidence_ids, source=req.source,
                              owner=req.owner)
            return {"status": "ok" if res.get("ok") else "error", "details": res}
        except Exception as e:
            logger.error(f"opinions/set 失败: {e}")
            raise HTTPException(500, str(e))

    @app.get("/opinions")
    def opinions_list(fact_id: int = 0):
        """查某事实的信念清单"""
        if not fact_id:
            raise HTTPException(400, "fact_id 不能为空")
        try:
            from ducky.opinion import list_opinions
            return {"status": "ok", "results": list_opinions(fact_id)}
        except Exception as e:
            logger.error(f"opinions 查询失败: {e}")
            raise HTTPException(500, str(e))

    @app.get("/opinions/aggregate")
    def opinions_aggregate(fact_id: int = 0):
        """聚合判定：≥2 个不同证据来源才聚合（单来源刷好评不聚合）"""
        if not fact_id:
            raise HTTPException(400, "fact_id 不能为空")
        try:
            from ducky.opinion import aggregate_opinion
            return {"status": "ok", "details": aggregate_opinion(fact_id)}
        except Exception as e:
            logger.error(f"opinions/aggregate 失败: {e}")
            raise HTTPException(500, str(e))

    @app.post("/update")
    def update(req: UpdateRequest):
        # 🔴P0-4: 传递并严格校验 user_id 归属，并同步更新 FTS5、facts 与 memory_types
        if not req.memory_id or not req.memory_id.strip():
            raise HTTPException(400, "memory_id 不能为空")
        try:
            mem = get_memory()
            content = req.content
            if not content:
                extra = getattr(req, "model_extra", None) or {}
                content = extra.get("data", "")
            
            user_id = _normalize_user_id(req.user_id) if req.user_id else DEFAULT_USER_ID
            mem.update(req.memory_id, data=content)
            
            # 同步更新 FTS
            try:
                from ducky.text_fts import _index_memory
                _index_memory(req.memory_id, content, user_id=user_id)
            except Exception as fe:
                logger.debug(f"FTS index on update 跳过: {fe}")

            # 同步更新 facts.db 事实内容与更新时间
            try:
                from ducky.utils import get_facts_conn
                fconn = get_facts_conn()
                if user_id == DEFAULT_USER_ID:
                    fconn.execute(
                        "UPDATE facts SET fact_value=?, updated_at=CURRENT_TIMESTAMP WHERE id=? OR fact_key=?",
                        (content, req.memory_id, req.memory_id),
                    )
                else:
                    fconn.execute(
                        "UPDATE facts SET fact_value=?, updated_at=CURRENT_TIMESTAMP WHERE (id=? OR fact_key=?) AND (source=? OR agent_id=?)",
                        (content, req.memory_id, req.memory_id, user_id, user_id),
                    )
                fconn.commit()
                fconn.close()
            except Exception as fte:
                logger.debug(f"facts update on update 跳过: {fte}")

            return {"status": "ok"}
        except Exception as e:
            logger.error(f"update 失败: {e}")
            raise HTTPException(500, str(e))

    @app.get("/usage")
    def usage(start: str = None, end: str = None):
        local_usage = get_llm_usage()
        try:
            from ducky.router_usage import fetch_router_llm_usage
            router_llm = fetch_router_llm_usage()
            if router_llm:
                merged = dict(local_usage) if local_usage else {}
                for dt, info in router_llm.items():
                    if dt not in merged:
                        merged[dt] = {}
                    merged[dt]["llm"] = info
                return {"status": "ok", "source": "router", "usage": merged}
        except Exception as e:
            logger.warning(f"获取上游网关用量失败，回退到本地: {e}")
        return {"status": "ok", "source": "local", "usage": local_usage}

    @app.post("/reload")
    def reload_mem0():
        reset_memory_singleton()
        try:
            _ = get_memory()
            return {"status": "ok", "message": "mem0 重新加载成功"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def _do_inject_context(req: InjectContextRequest) -> dict:
        from ducky.facts_recall import inject_context as inject_facts_context
        # 🔴P0-2（v19.4.1）：注入上下文按租户收窄 —— 注入是记忆流向宿主
        # 模型的出口，此处漏租户等于把别人的事实喂进本租户的对话。
        # InjectContextRequest 早已带 user_id 字段，此前未透传。
        return inject_facts_context(
            req.query,
            k=req.k,
            level=req.level,
            max_tokens=req.max_tokens,
            user_id=req.user_id,
        )

    @app.post("/facts/inject-context")
    def inject_context(req: dict):
        return _do_inject_context(InjectContextRequest(**req))
