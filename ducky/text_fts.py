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


# FTS5 trigram 分词器的硬性下限：少于 3 个字符的 MATCH 词元不可能命中索引。
# 这不是可调参数，是 trigram tokenizer 的定义决定的。
_TRIGRAM_MIN_LEN = 3


def _fts_terms(query: str) -> list[str]:
    """中英混合切词，产出**可命中 trigram 索引**的 MATCH 词元。

    🟠P1-2（v19.4.1）根治「切词与索引失配」：
        此前中文切 2-gram，而虚拟表建的是 `tokenize='trigram'` ——
        2 字词元在 trigram 索引里永远匹配不上。实测：
            MATCH '"银行"'   → 0 行
            MATCH '"银行卡"' → 1 行
        于是**每一次中文查询都静默落到 LIKE 全表扫描**，
        「trigram 全文索引」这个宣称对中文实际从未生效。
        20 万条原文实测代价：稀有中文词 32.8 ms（LIKE）vs 0.2 ms（FTS）。

    现在中文按 3-gram 切，与索引对齐；不足 3 字的中文查询（如「祖母」）
    无法用 trigram 表达，交由 LIKE 兜底 —— 这是 trigram 的固有边界，
    不再假装走了索引，由 `_recall_path` 字段显式暴露实际走的哪条路。
    """
    q = (query or "").strip()
    if not q:
        return []
    terms: list[str] = []
    # 英文/数字：trigram 对 ASCII 同样要求 >= 3 字符
    terms.extend(
        t for t in re.findall(r"[A-Za-z0-9_]+", q) if len(t) >= _TRIGRAM_MIN_LEN
    )
    # 中文连续段 → 3-gram 滑窗；整段不足 3 字则整段保留（留给 LIKE 兜底判断）
    for seg in re.findall(r"[\u4e00-\u9fff]+", q):
        if len(seg) < _TRIGRAM_MIN_LEN:
            terms.append(seg)
        else:
            terms.extend(
                seg[i:i + _TRIGRAM_MIN_LEN]
                for i in range(len(seg) - _TRIGRAM_MIN_LEN + 1)
            )
    # 去重保序
    seen, out = set(), []
    for t in terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:16]


def fts_match_terms(query: str) -> list[str]:
    """只保留能真正命中 trigram 索引的词元（长度 >= 3）。

    切词结果里可能混有不足 3 字的短词（如「祖母」），它们放进 MATCH 表达式
    只会让整个 OR 串失配。这里把它们剔掉：有剩余词元就走 FTS，
    一个都不剩说明这条查询天然无法用 trigram 表达，直接走 LIKE。
    """
    return [t for t in _fts_terms(query) if len(t) >= _TRIGRAM_MIN_LEN]


def fts_is_authoritative(query: str) -> bool:
    """FTS 的「零命中」是否可信（可信则无需再做 LIKE 全表扫）。

    trigram 分词器把内容里所有 3 字窗口都建了索引，因此对 >= 3 字的词元，
    `MATCH '"abc"'` 与 `content LIKE '%abc%'` 命中集合等价。
    只要本次查询的**所有**词元都 >= 3 字，FTS 返回空就意味着真的没有，
    再兜一次 LIKE 只是白扫一遍全表（20 万条实测白扫 23.8 ms）。

    若查询里混有不足 3 字的词元（如「祖母」），它们没进 MATCH 表达式，
    FTS 的空结果就不完整，此时必须兜 LIKE。
    """
    terms = _fts_terms(query)
    if not terms:
        return False
    return all(len(t) >= _TRIGRAM_MIN_LEN for t in terms)


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

    # 🟠P1-2：只把长度 >= 3 的词元送进 MATCH。短词元混进 OR 串会让整串失配，
    # 这正是此前中文查询「看起来建了索引却从不命中」的直接原因。
    match_terms = fts_match_terms(q)

    rows = []
    recall_path = "like"
    fts_attempted = False
    if match_terms:
        safe = []
        for t in match_terms[:12]:
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
            fts_attempted = True
            if rows:
                recall_path = "fts"
            elif fts_is_authoritative(q):
                # 权威零命中：这次确实走了索引，只是没有结果
                recall_path = "fts"
        except Exception as e:
            logger.debug(f"FTS MATCH 失败，降级 LIKE: {e}")
            rows = []

    # FTS 已权威给出「零命中」时不再白扫 LIKE（见 fts_is_authoritative）
    if not rows and not (fts_attempted and fts_is_authoritative(q)):
        rows = _like_search(terms or [q], user_id, top_k, conn)
        recall_path = "like"

    conn.close()
    # P1-4 降级可观测：调用方（含测试）可自证这次召回真走的是索引还是全表扫。
    return [dict(r, _recall_path=recall_path) for r in rows]


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
