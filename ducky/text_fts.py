"""
ducky.text_fts — FTS5 / BM25 全文检索（D 档从 legacy_routes 抽出）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
trigram 分词 + 中文 2-gram · 索引/回填/BM25 · hybrid 兜底共用。

对外符号保持 `_` 前缀，兼容：
  from ducky.text_fts import _bm25_keyword_search
"""

from __future__ import annotations

import logging
import re
import threading
import time
import sqlite3

from ducky.utils import DEFAULT_USER_ID, get_text_conn

logger = logging.getLogger("aiduMEM.text_fts")


def _ensure_trigram_fts(conn: sqlite3.Connection):
    """确保 FTS 使用 trigram 分词（中文可子串匹配）。旧 unicode61 表自动迁移重建。"""
    need_rebuild = False
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='memories_fts'"
    ).fetchone()
    if row is None:
        need_rebuild = True
    else:
        sql = (row[0] or "").lower()
        if "trigram" not in sql:
            need_rebuild = True

    if need_rebuild:
        logger.info("🔄 FTS 迁移到 trigram 分词…")
        conn.executescript("""
            DROP TRIGGER IF EXISTS mem_ai;
            DROP TRIGGER IF EXISTS mem_ad;
            DROP TRIGGER IF EXISTS mem_au;
            DROP TABLE IF EXISTS memories_fts;
        """)

    conn.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY, content TEXT, user_id TEXT, category TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            content,
            content=memories,
            content_rowid=rowid,
            tokenize='trigram'
        );
        CREATE TRIGGER IF NOT EXISTS mem_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memories_fts(rowid, content) VALUES (new.rowid, new.content);
        END;
        CREATE TRIGGER IF NOT EXISTS mem_ad AFTER DELETE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.rowid, old.content);
        END;
        CREATE TRIGGER IF NOT EXISTS mem_au AFTER UPDATE ON memories BEGIN
            INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.rowid, old.content);
            INSERT INTO memories_fts(rowid, content) VALUES (new.rowid, new.content);
        END;
    """)

    if need_rebuild:
        # content= 外挂模式：重建后要把现有 memories 灌回 FTS
        try:
            conn.execute("INSERT INTO memories_fts(rowid, content) SELECT rowid, content FROM memories")
            conn.commit()
            n = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            logger.info(f"✅ FTS trigram 重建完成，回灌 {n} 条")
        except Exception as e:
            logger.warning(f"FTS 回灌跳过: {e}")


def _fts_terms(query: str) -> list[str]:
    """中英混合切词：中文 2-gram + 英文/数字词。"""
    q = (query or "").strip()
    if not q:
        return []
    terms: list[str] = []
    # 英文数字
    terms.extend(re.findall(r"[A-Za-z0-9_]{2,}", q))
    # 中文连续段 → 2-gram（单字也保留）
    for seg in re.findall(r"[\u4e00-\u9fff]+", q):
        if len(seg) == 1:
            terms.append(seg)
        else:
            terms.extend(seg[i:i+2] for i in range(len(seg) - 1))
    # 去重保序
    seen, out = set(), []
    for t in terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:16]


def _init_text_fts():
    """初始化 FTS5 schema + 触发器。回填不在启动瞬间做（避免 Qdrant 锁竞态）。"""
    conn = get_text_conn()
    _ensure_trigram_fts(conn)
    conn.commit()
    try:
        cnt = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    except Exception as e:
        logger.debug(f"FTS count 跳过: {e}")
        cnt = 0
    conn.close()

    def _delayed_backfill():
        time.sleep(3)
        try:
            conn2 = get_text_conn()
            cur = conn2.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            conn2.close()
            if cur == 0:
                _backfill_text_fts(limit=2000)
        except Exception as e:
            logger.warning(f"FTS 延迟回填跳过: {e}")
    threading.Thread(target=_delayed_backfill, daemon=True, name="aiduMEM-fts-backfill").start()


def _index_memory(memory_id, content, user_id=DEFAULT_USER_ID, category=""):
    if not memory_id or not content:
        return
    conn = get_text_conn()
    # 先删再插，保证 content= 外挂 FTS 与 rowid 同步（避免 REPLACE 残留）
    conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
    conn.execute(
        "INSERT INTO memories (id,content,user_id,category) VALUES (?,?,?,?)",
        (memory_id, content, user_id, category or ""),
    )
    conn.commit()
    conn.close()


def _unindex_memory(memory_id):
    if not memory_id:
        return
    conn = get_text_conn()
    conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
    conn.commit()
    conn.close()


def _backfill_text_fts(limit: int = 2000, user_id: str = DEFAULT_USER_ID) -> int:
    """从 mem0 拉一批记忆灌入 FTS，供向量失败时兜底。"""
    try:
        # 优先 mem0_runtime，避免强依赖 api_server 组装层
        try:
            from ducky.mem0_runtime import get_memory
        except Exception:
            from api_server import get_memory
        mem = get_memory()
        raw = mem.get_all(filters={"user_id": user_id}, limit=limit)
        items = raw.get("results", raw) if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return 0
        n = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            mid = item.get("id") or item.get("memory_id")
            text = item.get("memory") or item.get("content") or ""
            if not mid or not text:
                continue
            meta = item.get("metadata") or {}
            _index_memory(mid, text, user_id=item.get("user_id", user_id), category=meta.get("category", ""))
            n += 1
        logger.info(f"✅ FTS 回填完成: {n} 条")
        return n
    except Exception as e:
        logger.warning(f"FTS 回填失败: {e}")
        return 0


def _like_search(terms, user_id, top_k, conn=None):
    should_close = conn is None
    if should_close:
        conn = get_text_conn()
    if not terms:
        rows = conn.execute(
            "SELECT id,content,category FROM memories WHERE user_id=? LIMIT ?",
            (user_id, top_k),
        ).fetchall()
    else:
        clauses = ["content LIKE ?" for _ in terms]
        params = [f"%{t}%" for t in terms] + [user_id, top_k]
        rows = conn.execute(
            f"SELECT id,content,category FROM memories WHERE ({' OR '.join(clauses)}) AND user_id=? LIMIT ?",
            params,
        ).fetchall()
    if should_close:
        conn.close()
    return [dict(r) for r in rows]


def calc_bm25_score(query: str, content: str) -> float:
    """计算单条内容相对于 query 的词频匹配得分 (0.0~1.0)"""
    if not query or not content:
        return 0.0
    terms = _fts_terms(query)
    if not terms:
        terms = [t for t in query.split() if t]
    if not terms:
        return 0.0
    hit_count = sum(1 for t in terms if t.lower() in content.lower())
    return min(1.0, hit_count / len(terms))


def _bm25_keyword_search(query: str, top_k: int = 10, user_id: str = DEFAULT_USER_ID) -> list:
    """BM25/关键词检索。FTS 无 user_id 列，必须 JOIN memories 过滤。"""
    conn = get_text_conn()
    # 运行时也兜底确保 trigram（老进程/旧库）
    try:
        _ensure_trigram_fts(conn)
        conn.commit()
    except Exception as e:
        logger.debug(f"FTS ensure 跳过: {e}")

    q = (query or "").strip()
    if not q:
        conn.close()
        return []

    terms = _fts_terms(q)
    if not terms:
        terms = [t for t in q.split() if t]

    rows = []
    if terms:
        safe = []
        for t in terms[:12]:
            t = t.replace('"', '""')
            if t:
                safe.append(f'"{t}"')
        match_expr = " OR ".join(safe)
        try:
            rows = conn.execute(
                """
                SELECT m.id, m.content, m.category
                FROM memories_fts
                JOIN memories m ON m.rowid = memories_fts.rowid
                WHERE memories_fts MATCH ? AND m.user_id = ?
                LIMIT ?
                """,
                (match_expr, user_id, top_k),
            ).fetchall()
        except Exception as e:
            logger.debug(f"FTS MATCH 失败，降级 LIKE: {e}")
            rows = []

    if not rows:
        rows = _like_search(terms or [q], user_id, top_k, conn)

    conn.close()
    return [dict(r) for r in rows]


def _hybrid_search(query: str, top_k: int = 10, user_id: str = DEFAULT_USER_ID,
                   vector_weight: float = 0.7):
    """旧接口 → 委托给 aiduMEM-v7 混合召回（向后兼容）"""
    try:
        from ducky.hybrid_recall import hybrid_search
        try:
            from ducky.mem0_runtime import get_memory
        except Exception:
            from api_server import get_memory
        results = hybrid_search(get_memory(), query, user_id, top_k)
        return results
    except Exception as e:
        logger.debug(f"hybrid 委托失败，降级 BM25: {e}")
        return _bm25_keyword_search(query, top_k, user_id)
