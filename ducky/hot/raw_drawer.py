"""
ducky.hot.raw_drawer — POST /add/raw 原味抽屉
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Zeus-Alpha v18.0: 吸收 MemPalace Verbatim Storage 理念。
长代码 / 日志 / 原文直入 FTS5 + Qdrant 向量，绕过 LLM 提取。
标记 memory_tier='verbatim'，与现有 LLM 抽取轨道完全并行。

安全铁律：
  - 不碰 /add 热路径任何代码
  - 只走 embedding，不走 LLM
  - FTS5 索引复用 text_fts._index_memory
"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from ducky.utils import DEFAULT_USER_ID

logger = logging.getLogger("aiduMEM.raw_drawer")


class RawDrawerRequest(BaseModel):
    """原味抽屉写入请求"""
    content: str                          # 原始文本（代码 / 日志 / 长文）
    user_id: str = DEFAULT_USER_ID
    metadata: dict = Field(default_factory=dict)
    # 可选：显式指定来源类型
    source: str = "raw_drawer"
    # 可选：去重 hash（相同内容不重复存）
    dedup: bool = True


def register_raw_drawer_routes(app: FastAPI) -> None:
    """注册 /add/raw 端点和查询端点"""

    @app.post("/add/raw")
    def add_raw(req: RawDrawerRequest):
        """原味抽屉：文本直入 FTS5 + Qdrant 向量，零 LLM 开销。

        适用场景：
          - 长代码片段（不想被 LLM 总结损失细节）
          - 日志原文（需要精确关键词检索）
          - 配置文件 / 架构文档
        """
        t0 = time.time()

        if not req.content or not req.content.strip():
            raise HTTPException(400, "content 不能为空")

        content = req.content.strip()
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

        # ── 1. FTS5 索引（本地全文检索）──
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

        # ── 2. Qdrant 向量入库（语义检索）──
        vector_ok = False
        try:
            from ducky.mem0_runtime import get_memory
            mem = get_memory()
            # 直接调用 mem0 底层 add，但只做 embedding 不做 LLM 提取
            # 通过 metadata 标记为 verbatim tier
            md = dict(req.metadata or {})
            md["memory_tier"] = "verbatim"
            md["source"] = req.source
            md["content_hash"] = content_hash
            md["raw_length"] = len(content)

            # mem0 的 add 会走 LLM，所以我们直接用底层 vector store
            # 但为了简单安全，先用 add + infer=False 跳过推理
            result = mem.add(
                content,
                user_id=req.user_id,
                metadata=md,
                infer=False,  # 关键：跳过 LLM 推理，只做 embedding
            )
            vector_ok = True
        except Exception as e:
            logger.warning(f"Raw 向量入库失败: {e}")
            result = None

        # ── 3. facts.db 登记（可选，用于 TreeMemory 关联）──
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
                    content[:500],  # facts 只存摘要前500字
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
        """原味抽屉统计"""
        try:
            from ducky.text_fts import get_text_conn
            conn = get_text_conn()
            total = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE id LIKE 'raw-%'"
            ).fetchone()[0]
            conn.close()
        except Exception:
            total = -1

        try:
            from ducky.utils import get_facts_conn
            conn = get_facts_conn()
            facts_count = conn.execute(
                "SELECT COUNT(*) FROM facts WHERE memory_tier='verbatim'"
            ).fetchone()[0]
            conn.close()
        except Exception:
            facts_count = -1

        return {
            "status": "ok",
            "raw_memories_fts": total,
            "verbatim_facts": facts_count,
        }
