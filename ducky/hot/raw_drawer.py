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

from ducky.bank_contract import DEFAULT_BANK_ID
from ducky.utils import DEFAULT_USER_ID
from ducky.security.injection_guard import validate_and_sanitize_memory_content

logger = logging.getLogger("aiduMEM.raw_drawer")


class RawDrawerRequest(BaseModel):
    content: str
    user_id: str = DEFAULT_USER_ID
    # 🔴v20：原味抽屉此前完全不知道「域」的存在 —— /add/raw 写入的原文
    # 恒落默认域，任何命名域都无法用它存原文。补齐后与 /add 同一套契约。
    bank_id: str = DEFAULT_BANK_ID
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
                # 🔴v20：这条去重查询原本**不带任何租户/域条件** —— 只要内容哈希
                # 撞上，就把别人的那条当成「已存在」，直接把**其他租户的
                # memory_id** 回给调用方，同时静默丢弃本次写入。既是跨租户信息
                # 泄露，也是一次无声的数据丢失（用户以为存进去了）。
                # 去重必须发生在域内。
                existing = conn.execute(
                    "SELECT id FROM memories WHERE id LIKE ? AND user_id=? AND bank_id=? LIMIT 1",
                    (f"raw-{content_hash}%", req.user_id, req.bank_id)
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
                category=category,
                bank_id=req.bank_id,
            )
            fts_ok = True
        except Exception as e:
            logger.warning(f"Raw FTS5 索引失败: {e}")

        # ── 2. Qdrant 向量入库 ──
        vector_ok = False
        try:
            from ducky.mem0_runtime import get_memory
            mem = get_memory()
            # 🔴v20：原文向量也要盖域戳，否则命名域存的原文在向量侧与默认域
            # 混在一起，只有 FTS 那一半是隔离的。
            from ducky.bank_contract import stamp_bank_metadata
            md = stamp_bank_metadata(req.metadata, req.bank_id)
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
            # 🔴v20 甲6：补 user_id / bank_id 两列作用域戳。
            #
            # facts 的唯一约束是 (agent_id, user_id, bank_id, category, fact_key)
            # ——见 federation/schema.py 的 FACTS_UNIQUE_COLUMNS。原来这两列
            # 一个字没给，落到迁移时的 DEFAULT 'default'，于是同一租户在库 A
            # 和库 B 写同一段内容，凑出的唯一元组一模一样，``INSERT OR IGNORE``
            # 把后写的那条**静默丢掉**（不抛异常、只有一句 debug 都不会有），
            # 而下面的返回值照旧报 ``facts_registered: true`` —— 响应在替一次
            # 没有发生的登记作证。同一类缺陷 v19.4.0 P0-2b 在 /facts/add 上
            # 出过一次（见 version.py），那次的修法是按租户落 agent_id；
            # bank 这一维是 v20 新加的，于是同一个坑又露了半边。
            #
            # fact_key 仍保持全局形状 ``raw:{hash}``，**没有**改成
            # ``raw:{user}:{bank}:{hash}``（方案原文写的是改键形，实施时否掉了）：
            #   · 作用域已经由上面两列进了唯一约束，键里再塞一遍是冗余；
            #   · 存量库里的键是老形状，一改形状，同一段老内容在升级后会被
            #     判成新键再插一行，OR IGNORE 的幂等去重就此失效。
            # 第二条由 test_jia6_legacy_global_key_row_still_deduplicates 钉住。
            conn.execute(
                """INSERT OR IGNORE INTO facts
                   (category, fact_key, fact_value, source, memory_tier,
                    agent_id, user_id, bank_id)
                   VALUES (?, ?, ?, ?, 'verbatim', ?, ?, ?)""",
                (
                    category,
                    f"raw:{content_hash}",
                    content[:500],
                    req.source,
                    req.user_id,
                    req.user_id,
                    req.bank_id,
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
