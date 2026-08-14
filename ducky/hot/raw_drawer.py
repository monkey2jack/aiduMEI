"""
ducky.hot.raw_drawer — POST /add/raw 原味抽屉（带注入防护版）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Zeus-Alpha v18.0 / v19.2.0 加固：
长代码 / 日志 / 原文直入 FTS5 + Qdrant 向量，绕过 LLM 提取。
标记 memory_tier='verbatim'，与现有 LLM 抽取轨道完全并行。
"""
from __future__ import annotations

import hashlib
import logging
import time
import uuid

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from ducky.utils import DEFAULT_USER_ID
from ducky.security.injection_guard import validate_and_sanitize_memory_content

logger = logging.getLogger("aiduMEM.raw_drawer")


class RawDrawerRequest(BaseModel):
    content: str
    user_id: str = DEFAULT_USER_ID
    metadata: dict = Field(default_factory=dict)
    source: str = "raw_drawer"
    dedup: bool = True


def register_raw_drawer_routes(app: FastAPI) -> None:
    @app.post("/add/raw")
    def add_raw(req: RawDrawerRequest):
        t0 = time.time()

        if not req.content or not req.content.strip():
            raise HTTPException(400, "content 不能为空")

        is_safe, sanitized_content, rejection = validate_and_sanitize_memory_content(req.content.strip())
        if not is_safe:
            logger.warning("🛡️ /add/raw rejected injection: %s", rejection)
            raise HTTPException(400, f"Memory content rejected: {rejection}")

        content = sanitized_content
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

        # ── 去重检查 ──
        if req.dedup:
            try:
                from ducky.text_fts import get_text_conn
                conn = get_text_conn()
                existing = conn.execute(
                    "SELECT id FROM memories WHERE id LIKE ? LIMIT 1",
                    (f"raw-{content_hash}%",)
                ).fetchone()
                conn.close()
                if existing:
                    return {
                        "status": "ok",
                        "action": "dedup_skipped",
                        "memory_id": existing[0],
                        "message": "内容已存在（去重跳过）",
                        "timing_ms": round((time.time() - t0) * 1000, 1),
                    }
            except Exception as e:
                logger.debug(f"去重检查跳过: {e}")

        memory_id = f"raw-{content_hash}-{uuid.uuid4().hex[:8]}"
        category = req.metadata.get("category", "verbatim")

        # ── 1. FTS5 索引 ──
        fts_ok = False
        try:
            from ducky.text_fts import _index_memory
            _index_memory(
                memory_id, content,
                user_id=req.user_id,
                category=category
            )
            fts_ok = True
        except Exception as e:
            logger.warning(f"Raw FTS5 索引失败: {e}")

        # ── 2. Qdrant 向量入库 ──
        vector_ok = False
        try:
            from ducky.mem0_runtime import get_memory
            mem = get_memory()
            md = dict(req.metadata or {})
            md["memory_tier"] = "verbatim"
            md["source"] = req.source
            md["content_hash"] = content_hash
            md["raw_length"] = len(content)

            result = mem.add(
                content,
                user_id=req.user_id,
                metadata=md,
                infer=False,
            )
            vector_ok = True
        except Exception as e:
            logger.warning(f"Raw 向量入库失败: {e}")

        # ── 3. facts.db 登记 ──
        facts_ok = False
        try:
            from ducky.utils import get_facts_conn
            conn = get_facts_conn()
            conn.execute(
                """INSERT OR IGNORE INTO facts
                   (category, fact_key, fact_value, source, memory_tier, agent_id)
                   VALUES (?, ?, ?, ?, 'verbatim', ?)""",
                (
                    category,
                    f"raw:{content_hash}",
                    content[:500],
                    req.source,
                    req.user_id,
                )
            )
            conn.commit()
            conn.close()
            facts_ok = True
        except Exception as e:
            logger.debug(f"Raw facts 登记跳过: {e}")

        elapsed_ms = round((time.time() - t0) * 1000, 1)

        return {
            "status": "ok",
            "action": "raw_stored",
            "memory_id": memory_id,
            "memory_tier": "verbatim",
            "content_hash": content_hash,
            "raw_length": len(content),
            "fts_indexed": fts_ok,
            "vector_stored": vector_ok,
            "facts_registered": facts_ok,
            "timing_ms": elapsed_ms,
            "message": f"原味抽屉已存入 ({elapsed_ms}ms)",
        }

    @app.get("/raw/stats")
    def raw_stats():
        try:
            from ducky.text_fts import get_text_conn
            conn = get_text_conn()
            total = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE id LIKE 'raw-%'"
            ).fetchone()[0]
            conn.close()
        except Exception as e:
            logger.debug(f"raw_drawer total count skip: {e}")
            total = -1

        try:
            from ducky.utils import get_facts_conn
            conn = get_facts_conn()
            facts_count = conn.execute(
                "SELECT COUNT(*) FROM facts WHERE memory_tier='verbatim'"
            ).fetchone()[0]
            conn.close()
        except Exception as e:
            logger.debug(f"raw_drawer facts count skip: {e}")
            facts_count = -1

        return {
            "status": "ok",
            "raw_memories_fts": total,
            "verbatim_facts": facts_count,
        }
