"""ducky.hot.crud — recent/stats/delete/update/usage/reload/inject"""
from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException

from ducky.api_models import DeleteRequest, InjectContextRequest, UpdateRequest
from ducky.utils import DEFAULT_USER_ID
from ducky.mem0_runtime import (
    get_llm_usage,
    get_memory,
    reset_memory_singleton,
)

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
        try:
            mem = get_memory()
            mem.delete(req.memory_id)
            try:
                from ducky.text_fts import _unindex_memory
                _unindex_memory(req.memory_id)
            except Exception as e:
                logger.debug(f"FTS unindex on delete 跳过: {e}")
            return {"status": "ok"}
        except Exception as e:
            logger.error(f"delete 失败: {e}")
            raise HTTPException(500, str(e))

    @app.post("/delete_all")
    def delete_all(user_id: str = DEFAULT_USER_ID):
        try:
            mem = get_memory()
            mem.delete_all(user_id=user_id)
            return {"status": "ok"}
        except Exception as e:
            raise HTTPException(500, str(e))

    @app.post("/update")
    def update(req: UpdateRequest):
        try:
            mem = get_memory()
            mem.update(req.memory_id, data=req.content)
            return {"status": "ok"}
        except Exception as e:
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
        return inject_facts_context(
            req.query,
            k=req.k,
            level=req.level,
            max_tokens=req.max_tokens,
        )

    @app.post("/facts/inject-context")
    def inject_context(req: dict):
        return _do_inject_context(InjectContextRequest(**req))

