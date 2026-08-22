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
from ducky.bank_contract import DEFAULT_BANK_ID, make_scope, raw_storage_key, scoped_storage_key

logger = logging.getLogger("aiduMEM.text_fts")


# v20 基表形态：主键必须是 (id, user_id, bank_id) 三元组。
# 详见 _migrate_memories_pk 的说明。
_MEMORIES_DDL = """
    id TEXT NOT NULL,
    content TEXT,
    user_id TEXT,
    bank_id TEXT NOT NULL DEFAULT 'default',
    category TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id, user_id, bank_id)
"""


def _migrate_memories_pk(conn: sqlite3.Connection) -> bool:
    """把 v19 的 ``id TEXT PRIMARY KEY`` 重建成 ``(id, user_id, bank_id)``。

    🔴v20 结构性缺陷（与 workspace 主键同一类）：v20 给 memories 加了
    bank_id 列、把每条 SQL 都按 user_id/bank_id 过滤了，唯独主键还是**单列
    id**。而 `scoped_storage_key` 对默认域刻意保留裸 key（为兼容 v19 存量），
    于是默认域里两个租户的同一个 memory_id 会落到同一个主键上：

        v19：``DELETE FROM memories WHERE id=?`` 不带租户条件 ——
             bob 建索引会**静默删掉 alice 的那一行**（跨租户删除原语）。
        v20：``DELETE ... AND user_id=? AND bank_id=?`` 匹配 0 行，
             紧接着的 INSERT 撞主键 → ``UNIQUE constraint failed: memories.id``
             直接抛到调用方（`_index_memory` 没有 try）。

    一个静默毁数据、一个当场炸，都不对。根因是主键表达不了「同一个 id 在不同
    (租户, 域) 下是不同的行」。`scoped_storage_key` 的注释写着「ownership
    stays enforced by the user_id/bank_id columns」—— 那两列确实在过滤读，
    但主键根本不看它们，所以那句话对写入路径不成立。

    返回 True 表示确实重建过（rowid 全部变了，外挂 FTS 必须跟着重建）。
    """
    info = conn.execute("PRAGMA table_info(memories)").fetchall()
    if not info:
        return False
    pk_cols = {str(r[1]) for r in info if r[5] > 0}
    if pk_cols == {"id", "user_id", "bank_id"}:
        return False

    origin = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    logger.info("🔄 memories 主键迁移 %s → (id,user_id,bank_id)，存量 %d 条…", sorted(pk_cols), origin)
    # 触发器绑在旧表上，先摘掉：搬运期间不该有任何 FTS 副作用。
    conn.executescript("""
        DROP TRIGGER IF EXISTS mem_ai;
        DROP TRIGGER IF EXISTS mem_ad;
        DROP TRIGGER IF EXISTS mem_au;
        DROP TABLE IF EXISTS memories_v20;
    """)
    conn.execute(f"CREATE TABLE memories_v20 ({_MEMORIES_DDL})")
    # 旧主键（单列 id）比新主键更严，理论上一行都不会被 IGNORE 掉；
    # 但「理论上」不是验收标准，下面按条数实测。
    conn.execute(
        "INSERT OR IGNORE INTO memories_v20 (id,content,user_id,bank_id,category,created_at) "
        "SELECT id, content, user_id, "
        "       COALESCE(NULLIF(TRIM(bank_id),''), 'default'), category, created_at "
        "FROM memories"
    )
    moved = conn.execute("SELECT COUNT(*) FROM memories_v20").fetchone()[0]
    if moved < origin:
        # 宁可不迁移，也不能在用户的全文索引上留一个「少了几条但没人知道」的洞。
        conn.rollback()
        conn.execute("DROP TABLE IF EXISTS memories_v20")
        conn.commit()
        raise RuntimeError(f"memories 主键迁移会丢行：{origin} → {moved}，已回滚")
    conn.execute("DROP TABLE memories")
    conn.execute("ALTER TABLE memories_v20 RENAME TO memories")
    conn.commit()
    logger.info("✅ memories 主键迁移完成，%d 条全部搬运", moved)
    return True


def _ensure_trigram_fts(conn: sqlite3.Connection):
    """确保 FTS 使用 trigram 分词（中文可子串匹配）。旧 unicode61 表自动迁移重建。"""
    # ① 基表先就位。必须**早于** FTS 建表：memories_fts 是 content= 外挂模式，
    #    靠 rowid 与 memories 对齐，而主键迁移会重排 rowid。
    conn.execute(f"CREATE TABLE IF NOT EXISTS memories ({_MEMORIES_DDL})")
    # 老库补 bank_id 列（ALTER 只能加到末尾，列序与新装不同；全部 SQL 都按列名
    # 访问，故列序无关紧要，但不要在别处假设它）。
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}
        if "bank_id" not in cols:
            conn.execute("ALTER TABLE memories ADD COLUMN bank_id TEXT NOT NULL DEFAULT 'default'")
            conn.commit()
    except Exception as exc:
        logger.debug("memories bank_id migration skipped: %s", exc)
    # ② 主键迁移。重建过就必须连带重建 FTS —— rowid 全变了，旧索引全部错位。
    pk_migrated = _migrate_memories_pk(conn)

    need_rebuild = pk_migrated
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


def _index_memory(memory_id, content, user_id=DEFAULT_USER_ID, category=None, bank_id=DEFAULT_BANK_ID):
    """把一条记忆写进外挂 FTS。

    category 用 None 当哨兵，它和空串是两个意思，不许塌成同一个默认值：

      * None —— 调用方没提分类（例如 /update 的请求体里根本没有这个字段），
        沿用行里已经存着的分类；
      * ""   —— 调用方明确要求分类为空，照写。

    这条区分不是洁癖：本函数是「先删再插」（见下方注释），一旦把「没说」当成
    「说空」，每次不带分类的更新都会把存量分类覆盖掉一次，而向量侧靠 mem0 的
    payload merge 保住了自己那一份 —— 两半都在跑，但两半保留的信息量不同。
    """
    if not memory_id or not content:
        return
    scope = make_scope(user_id, bank_id)
    storage_id = scoped_storage_key(memory_id, scope)
    conn = get_text_conn()
    if category is None:
        # 只有「没说」才回查；说了分类的调用方一律不付这次 SELECT 的代价。
        prev = conn.execute(
            "SELECT category FROM memories WHERE id=? AND user_id=? AND bank_id=?",
            (storage_id, scope.user_id, scope.bank_id),
        ).fetchone()
        category = prev[0] if prev is not None else ""
    # 先删再插，保证 content= 外挂 FTS 与 rowid 同步（避免 REPLACE 残留）
    conn.execute("DELETE FROM memories WHERE id=? AND user_id=? AND bank_id=?", (storage_id, scope.user_id, scope.bank_id))
    conn.execute(
        "INSERT INTO memories (id,content,user_id,bank_id,category) VALUES (?,?,?,?,?)",
        (storage_id, content, scope.user_id, scope.bank_id, category or ""),
    )
    conn.commit()
    conn.close()


def _unindex_memory(memory_id, user_id=DEFAULT_USER_ID, bank_id=DEFAULT_BANK_ID):
    if not memory_id:
        return
    scope = make_scope(user_id, bank_id)
    conn = get_text_conn()
    conn.execute("DELETE FROM memories WHERE id=? AND user_id=? AND bank_id=?", (scoped_storage_key(memory_id, scope), scope.user_id, scope.bank_id))
    conn.commit()
    conn.close()


def _backfill_text_fts(limit: int = 2000, user_id: str = DEFAULT_USER_ID, bank_id: str = DEFAULT_BANK_ID) -> int:
    """从 mem0 拉一批记忆灌入 FTS，供向量失败时兜底。

    ``bank_id`` 目前**不影响写入行为**，别把它当成「把这批回填进某个库」的开关：
    FTS 行的归属只能跟源记忆走 —— payload 上有 bank 戳就用那个戳，没戳的是启用
    多库之前的存量、按迁移契约归默认库。这个形参留着是给读侧的（下面那句
    ``get_all`` 还没有按 bank 过滤），读那半单独定，写这半一律不看它。
    """
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
            # category 缺省不写死空串：payload 里没有分类 ≠ 分类是空的（见 _index_memory 哨兵约定）
            # bank 缺省回落 DEFAULT_BANK_ID 而不是形参：payload 上没戳 bank ≠ 属于
            # 操作者这次点的库。没戳的是启用多库之前的存量，按迁移契约归默认库；
            # 回落到形参等于把一条老记忆永久改判过去，而且 _index_memory 的 DELETE
            # 按 (id, user, bank) 三键定位 —— 错戳出来的是另一行，不是覆盖，再回填
            # 一次也清不掉。
            _index_memory(
                mid,
                text,
                user_id=item.get("user_id", user_id),
                category=meta.get("category"),
                bank_id=meta.get("bank_id", DEFAULT_BANK_ID),
            )
            n += 1
        logger.info(f"✅ FTS 回填完成: {n} 条")
        return n
    except Exception as e:
        logger.warning(f"FTS 回填失败: {e}")
        return 0


def _like_search(terms, user_id, top_k, conn=None, bank_id=DEFAULT_BANK_ID):
    should_close = conn is None
    if should_close:
        conn = get_text_conn()
    scope = make_scope(user_id, bank_id)
    if not terms:
        rows = conn.execute(
            "SELECT id,content,category,bank_id FROM memories WHERE user_id=? AND bank_id=? LIMIT ?",
            (scope.user_id, scope.bank_id, top_k),
        ).fetchall()
    else:
        clauses = ["content LIKE ?" for _ in terms]
        params = [f"%{t}%" for t in terms] + [scope.user_id, scope.bank_id, top_k]
        rows = conn.execute(
            f"SELECT id,content,category,bank_id FROM memories WHERE ({' OR '.join(clauses)}) AND user_id=? AND bank_id=? LIMIT ?",
            params,
        ).fetchall()
    if should_close:
        conn.close()
    return [{**dict(r), "id": raw_storage_key(r["id"], scope)} for r in rows]


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


def _bm25_keyword_search(query: str, top_k: int = 10, user_id: str = DEFAULT_USER_ID, bank_id: str = DEFAULT_BANK_ID) -> list:
    """BM25/关键词检索。FTS 无 user_id 列，必须 JOIN memories 过滤。"""
    scope = make_scope(user_id, bank_id)
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
                SELECT m.id, m.content, m.category, m.bank_id
                FROM memories_fts
                JOIN memories m ON m.rowid = memories_fts.rowid
                WHERE memories_fts MATCH ? AND m.user_id = ? AND m.bank_id = ?
                LIMIT ?
                """,
                (match_expr, scope.user_id, scope.bank_id, top_k),
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
        rows = _like_search(terms or [q], scope.user_id, top_k, conn, scope.bank_id)
        recall_path = "like"

    conn.close()
    # P1-4 降级可观测：调用方（含测试）可自证这次召回真走的是索引还是全表扫。
    return [{**dict(r), "id": raw_storage_key(r["id"], scope), "_recall_path": recall_path} for r in rows]


def _hybrid_search(query: str, top_k: int = 10, user_id: str = DEFAULT_USER_ID,
                   vector_weight: float = 0.7, bank_id: str = DEFAULT_BANK_ID):
    """旧接口 → 委托给 aiduMEM-v7 混合召回（向后兼容）"""
    try:
        from ducky.hybrid_recall import hybrid_search
        try:
            from ducky.mem0_runtime import get_memory
        except Exception:
            from api_server import get_memory
        results = hybrid_search(get_memory(), query, user_id, top_k, bank_id=bank_id)
        return results
    except Exception as e:
        logger.debug(f"hybrid 委托失败，降级 BM25: {e}")
        return _bm25_keyword_search(query, top_k, user_id, bank_id)
