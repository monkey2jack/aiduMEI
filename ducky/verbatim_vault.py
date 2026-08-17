"""
ducky.verbatim_vault — 原文保真层 Verbatim Vault (v19.4.0 · 明镜工程 Phase 1)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

为什么需要这一层
    aiduMEI 的记忆写入走 mem0 的 LLM 抽取：对话进来 → 蒸馏成原子事实 → 落库。
    蒸馏必然丢细节——语气、上下文、原话的精确措辞都在抽取中被抹掉。
    AML 榜单调研（2026-08-17）证实：显式事实召回（A 维度）的头部系统，
    靠的正是「原文一字不丢地存 + 混合检索」，而不是更花的抽取。

    Verbatim Vault 就是补上这一课：**说过的话，一字不丢**。
    它在 mem0 抽取之外，并行存一份逐字原文；召回时原文证据与原子事实融合返回。
    抽取层不动，只增不改——对现有 facts 零影响。

设计原则（对齐 aiduMEI 既有纪律）
    · 全部 CREATE TABLE / INDEX IF NOT EXISTS，对既有库是 no-op，绝不 DROP / 删数据
    · 租户硬隔离：一切读写按 user_id 精确匹配，杜绝跨租户串味（延续 v19.2.0 铁律）
    · 幂等去重：同一 (user_id, 内容, 时间戳) 重放只落一条，防重复写入
    · 失败干净降级：本层任何异常只记日志，绝不阻断 /add 与 /search 主链路
    · 数据物理位置：结构化原文落 facts.db，全文索引落 text_fts.db（沿用既有分库）

原文层的收录边界（v19.4.0 生产审计 🟡-1 拍板，明确写清不含糊）
    · 原文层**只保对话原文**：钩子挂在 /add（sync_turn、on_pre_compress 两条对话写入路径），
      收录的是 user/assistant 逐字原话。
    · **/facts/add 不挂钩子**：该路由写的是 memory 工具已蒸馏的事实（fact_key/fact_value），
      不是对话原文；且其源头对话已经过 /add 进了原文层，再挂会造成语义混淆与重复。
      故 memory 镜像写入走事实层，原文层不重复收录——「说过的话一字不丢」由 /add 侧保证。

对外符号
    ensure_verbatim_schema()          建表（幂等）
    store_verbatim(...)               写入原文（/add 钩子调用）
    verbatim_search(...)              原文全文检索
    fuse_verbatim(...)                把原文证据融合进召回结果
    cascade_delete_verbatim(...)      级联删除某租户原文（wal_engine 调用）
    count_verbatim(...)               统计某租户原文条数（运维/验收用）
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from ducky.utils import DEFAULT_USER_ID, get_facts_conn, get_text_conn

logger = logging.getLogger("aiduMEM.verbatim")

# 融合策略参数（v19.4.0 生产审计 🟡-3：配额可配置 + 相关度门槛）
VERBATIM_FUSE_QUOTA_RATIO = 4   # 原文配额 = limit // 该值
VERBATIM_FUSE_MIN_QUOTA = 2     # 配额下限：至少给原文留 2 条位置
VERBATIM_FUSE_MIN_OVERLAP = 2   # 原文命中须与查询词至少重合 N 个词才配得上占位


# ─────────────────────────────────────────────────────────────
# Schema — facts.db 存结构化原文，text_fts.db 存全文索引
# ─────────────────────────────────────────────────────────────

_VERBATIM_TURNS_DDL = """
CREATE TABLE IF NOT EXISTS verbatim_turns (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL,
    session_id   TEXT DEFAULT '',
    role         TEXT DEFAULT 'user',
    content      TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    recorded_at  TEXT,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_VERBATIM_INDEXES = (
    "CREATE INDEX        IF NOT EXISTS idx_verbatim_user ON verbatim_turns(user_id)",
    # 幂等去重：同租户同内容同时间戳只落一条（防重放），不同时间的重复表述照常保留
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_verbatim_dedup ON verbatim_turns(user_id, content_hash, recorded_at)",
)

# text_fts.db 侧的 FTS 映射表 + trigram 虚拟表（与 memories_fts 同一套外挂 content= 模式）
_VERBATIM_FTS_DDL = """
CREATE TABLE IF NOT EXISTS verbatim_fts_map (
    turn_id INTEGER PRIMARY KEY,
    content TEXT,
    user_id TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS verbatim_fts USING fts5(
    content,
    content=verbatim_fts_map,
    content_rowid=turn_id,
    tokenize='trigram'
);
CREATE TRIGGER IF NOT EXISTS verbatim_ai AFTER INSERT ON verbatim_fts_map BEGIN
    INSERT INTO verbatim_fts(rowid, content) VALUES (new.turn_id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS verbatim_ad AFTER DELETE ON verbatim_fts_map BEGIN
    INSERT INTO verbatim_fts(verbatim_fts, rowid, content) VALUES('delete', old.turn_id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS verbatim_au AFTER UPDATE ON verbatim_fts_map BEGIN
    INSERT INTO verbatim_fts(verbatim_fts, rowid, content) VALUES('delete', old.turn_id, old.content);
    INSERT INTO verbatim_fts(rowid, content) VALUES (new.turn_id, new.content);
END;
"""


def _content_hash(text: str) -> str:
    """内容指纹：用于幂等去重，不用于检索。"""
    return hashlib.sha256((text or "").strip().encode("utf-8", errors="ignore")).hexdigest()


def ensure_verbatim_schema() -> None:
    """幂等建表。对既有库是 no-op，任何异常只记日志不抛。"""
    try:
        fconn = get_facts_conn()
        fconn.execute(_VERBATIM_TURNS_DDL)
        for stmt in _VERBATIM_INDEXES:
            try:
                fconn.execute(stmt)
            except Exception as exc:
                logger.debug("verbatim 索引跳过: %s", exc)
        fconn.commit()
    except Exception as exc:
        logger.warning("verbatim_turns 建表跳过（服务继续）: %s", exc)

    try:
        tconn = get_text_conn()
        tconn.executescript(_VERBATIM_FTS_DDL)
        tconn.commit()
    except Exception as exc:
        logger.warning("verbatim_fts 建表跳过（服务继续）: %s", exc)


# ─────────────────────────────────────────────────────────────
# 写入 — /add 钩子
# ─────────────────────────────────────────────────────────────

def _iter_turns(messages_json):
    """把 /add 的 messages 载荷拆成 (role, content, timestamp) 逐条原文。

    兼容 list[dict] / dict / 纯字符串三种形态（与 messages_to_text 同语义）。
    只产出非空 content 的条目。
    """
    if isinstance(messages_json, list):
        for m in messages_json:
            if not isinstance(m, dict):
                continue
            content = str(m.get("content", "") or "").strip()
            if not content:
                continue
            yield (
                str(m.get("role", "user") or "user"),
                content,
                m.get("timestamp"),
            )
    elif isinstance(messages_json, dict):
        content = str(messages_json.get("content", "") or "").strip()
        if content:
            yield (
                str(messages_json.get("role", "user") or "user"),
                content,
                messages_json.get("timestamp"),
            )
    else:
        content = str(messages_json or "").strip()
        if content:
            yield ("user", content, None)


def _normalize_ts(ts) -> str:
    """把消息时间戳归一成 ISO 字符串；缺失时用当前 UTC 时间。"""
    if isinstance(ts, (int, float)) and ts > 0:
        try:
            # 兼容秒 / 毫秒
            if ts > 1e12:
                ts = ts / 1000.0
            return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
        except Exception:
            pass
    if isinstance(ts, str) and ts.strip():
        return ts.strip()
    return datetime.now(timezone.utc).isoformat()


def store_verbatim(user_id: str, messages_json, metadata: dict | None = None) -> dict:
    """把一次 /add 的原文逐条落库。返回 {stored, skipped}。

    调用方（/add）在注入防御通过后调用本函数；本函数绝不抛异常阻断主链路。
    """
    result = {"stored": 0, "skipped": 0}
    if not user_id:
        return result
    try:
        ensure_verbatim_schema()
        md = metadata or {}
        session_id = str(md.get("session_id") or md.get("conversation_id") or "")
        fconn = get_facts_conn()
        tconn = get_text_conn()

        for role, content, ts in _iter_turns(messages_json):
            recorded_at = _normalize_ts(ts)
            chash = _content_hash(content)
            try:
                cur = fconn.execute(
                    """INSERT OR IGNORE INTO verbatim_turns
                       (user_id, session_id, role, content, content_hash, recorded_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (user_id, session_id, role, content, chash, recorded_at),
                )
                fconn.commit()
                if cur.rowcount and cur.rowcount > 0:
                    turn_id = cur.lastrowid
                    # 同步灌 FTS 映射表（触发器自动维护 trigram 索引）
                    try:
                        tconn.execute(
                            "INSERT OR REPLACE INTO verbatim_fts_map (turn_id, content, user_id) VALUES (?, ?, ?)",
                            (turn_id, content, user_id),
                        )
                        tconn.commit()
                    except Exception as fe:
                        logger.debug("verbatim FTS 灌入跳过: %s", fe)
                    result["stored"] += 1
                else:
                    result["skipped"] += 1
            except Exception as row_err:
                logger.debug("verbatim 单条写入跳过: %s", row_err)
                result["skipped"] += 1

        if result["stored"]:
            logger.info("📼 Verbatim Vault 落库 %d 条原文 (user=%s)", result["stored"], user_id)
    except Exception as exc:
        logger.warning("Verbatim Vault 写入降级（主链路不受影响）: %s", exc)
    return result


# ─────────────────────────────────────────────────────────────
# 检索 — 原文全文召回
# ─────────────────────────────────────────────────────────────

def _fts_terms(query: str) -> list:
    """中英混合切词：中文 2-gram + 英文/数字词（与 text_fts 同策略）。"""
    import re
    q = (query or "").strip()
    if not q:
        return []
    terms = []
    terms.extend(re.findall(r"[A-Za-z0-9_]{2,}", q))
    for seg in re.findall(r"[一-鿿]+", q):
        if len(seg) == 1:
            terms.append(seg)
        else:
            terms.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    seen, out = set(), []
    for t in terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out[:16]


def verbatim_search(query: str, user_id: str = DEFAULT_USER_ID, limit: int = 10) -> list:
    """对原文层做 trigram FTS 检索，返回按 BM25 排序的原文条目。

    返回形如 [{id, memory, user_id, role, session_id, recorded_at, _verbatim: True}]，
    字段对齐召回结果形态，便于 fuse_verbatim 融合。失败返回 []（干净降级）。
    """
    if not query or not user_id:
        return []
    try:
        ensure_verbatim_schema()
        tconn = get_text_conn()
        terms = _fts_terms(query)
        rows = []
        if terms:
            safe = []
            for t in terms[:12]:
                t = t.replace('"', '""')
                if t:
                    safe.append(f'"{t}"')
            match_expr = " OR ".join(safe)
            try:
                rows = tconn.execute(
                    """
                    SELECT m.turn_id, m.content, m.user_id, bm25(verbatim_fts) AS rank
                    FROM verbatim_fts
                    JOIN verbatim_fts_map m ON m.turn_id = verbatim_fts.rowid
                    WHERE verbatim_fts MATCH ? AND m.user_id = ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (match_expr, user_id, max(limit * 2, limit)),
                ).fetchall()
            except Exception as me:
                logger.debug("verbatim FTS MATCH 失败，降级 LIKE: %s", me)
                rows = []

        if not rows:
            # LIKE 兜底（与 text_fts._like_search 同语义）
            like_terms = terms or [query.strip()]
            clauses = ["content LIKE ?" for _ in like_terms]
            params = [f"%{t}%" for t in like_terms] + [user_id, max(limit * 2, limit)]
            try:
                rows = tconn.execute(
                    f"SELECT turn_id, content, user_id, 0 AS rank FROM verbatim_fts_map "
                    f"WHERE ({' OR '.join(clauses)}) AND user_id=? LIMIT ?",
                    params,
                ).fetchall()
            except Exception as le:
                logger.debug("verbatim LIKE 兜底失败: %s", le)
                rows = []

        if not rows:
            return []

        # 回 facts.db 取 role / session / 时间戳等结构化字段
        turn_ids = [r["turn_id"] for r in rows]
        meta_map = {}
        try:
            fconn = get_facts_conn()
            placeholders = ",".join("?" for _ in turn_ids)
            meta_rows = fconn.execute(
                f"SELECT id, role, session_id, recorded_at FROM verbatim_turns WHERE id IN ({placeholders})",
                turn_ids,
            ).fetchall()
            # sqlite3.Row 无 .get，统一转 dict 再回填
            meta_map = {mr["id"]: dict(mr) for mr in meta_rows}
        except Exception as meta_err:
            logger.debug("verbatim 元数据回填跳过: %s", meta_err)

        results = []
        for r in rows:
            tid = r["turn_id"]
            meta = meta_map.get(tid) or {}
            results.append({
                "id": f"verbatim:{tid}",
                "memory": r["content"],
                "content": r["content"],
                "user_id": r["user_id"],
                "role": meta.get("role", "user"),
                "session_id": meta.get("session_id", ""),
                "recorded_at": meta.get("recorded_at"),
                "_verbatim": True,
                "_bm25_rank": r["rank"] if "rank" in r.keys() else 0,
            })
        return results[:limit]
    except Exception as exc:
        logger.debug("verbatim_search 降级返回空: %s", exc)
        return []


# ─────────────────────────────────────────────────────────────
# 融合 — 原文证据并入召回结果
# ─────────────────────────────────────────────────────────────

def fuse_verbatim(results: list, verbatim_hits: list, limit: int = 10, query: str = "") -> list:
    """把原文证据融合进既有召回结果。

    策略（保守、可解释、主干优先）：
      1. 以既有召回结果为主干，保持其排序与结构不变；
      2. 原文命中若与主干某条内容高度重合（规范化后相等），只给该条打 _has_verbatim 标记，
         不新增重复条目；
      3. 未重合的原文命中作为补充证据，标记 memory_type=VERBATIM；
      4. 相关度门槛（v19.4.0 生产审计 🟡-3）：传入 query 且查询词足够多时，
         原文命中须与查询词至少重合 VERBATIM_FUSE_MIN_OVERLAP 个词才入选，
         防止低相关原文挤掉主干事实；
      5. 为原文证据保留配额（最多 max(VERBATIM_FUSE_MIN_QUOTA, limit//VERBATIM_FUSE_QUOTA_RATIO) 条，
         参数均可模块级配置），必要时裁掉主干尾部腾位，
         保证原文一定出得来、又不喧宾夺主；总数不超过 limit。

    失败时原样返回 results（绝不破坏主链路）。
    """
    if not verbatim_hits:
        return results
    try:
        results = list(results or [])

        def _norm(s):
            return "".join(str(s or "").split()).lower()

        existing_norms = set()
        for item in results:
            if isinstance(item, dict):
                existing_norms.add(_norm(item.get("memory") or item.get("content")))

        # 相关度门槛：查询词太少（短于门槛值）时不设卡，命中本身已是 BM25 召回
        q_terms = set(_fts_terms(query)) if query else set()
        gate_on = len(q_terms) >= VERBATIM_FUSE_MIN_OVERLAP

        fresh = []
        for hit in verbatim_hits:
            if not isinstance(hit, dict):
                continue
            hn = _norm(hit.get("memory") or hit.get("content"))
            if hn and hn in existing_norms:
                # 与主干重合 → 给主干对应条目打标，不重复追加
                for item in results:
                    if isinstance(item, dict) and _norm(item.get("memory") or item.get("content")) == hn:
                        item["_has_verbatim"] = True
                        break
                continue
            if gate_on:
                hit_terms = set(_fts_terms(hit.get("memory") or hit.get("content") or ""))
                if len(q_terms & hit_terms) < VERBATIM_FUSE_MIN_OVERLAP:
                    continue  # 低相关原文不配占位，主干事实优先
            hit = dict(hit)
            hit.setdefault("memory_type", "VERBATIM")
            fresh.append(hit)
            existing_norms.add(hn)

        if not fresh:
            return results

        if limit and limit > 0:
            quota = max(VERBATIM_FUSE_MIN_QUOTA, limit // VERBATIM_FUSE_QUOTA_RATIO)
            fresh = fresh[:quota]
            if len(results) + len(fresh) > limit:
                results = results[: max(0, limit - len(fresh))]

        return results + fresh
    except Exception as exc:
        logger.debug("fuse_verbatim 降级返回原结果: %s", exc)
        return results


# ─────────────────────────────────────────────────────────────
# 级联删除 — wal_engine 调用
# ─────────────────────────────────────────────────────────────

def cascade_delete_verbatim(user_id: str) -> int:
    """删除某租户全部原文（facts.db + text_fts.db 双侧）。返回删除条数。

    供 cascade_delete_all 调用，绝不留孤儿。失败只记日志，返回 0。
    与既有级联语义对齐：default 租户全清（防爆门禁在上游已把守），
    其余租户精确按 user_id 删除。
    """
    if not user_id or not user_id.strip():
        return 0
    deleted = 0
    try:
        ensure_verbatim_schema()
        fconn = get_facts_conn()
        # 先拿到 turn_id 用于清理 FTS 映射
        try:
            if user_id == "default":
                ids = [r["id"] for r in fconn.execute(
                    "SELECT id FROM verbatim_turns"
                ).fetchall()]
            else:
                ids = [r["id"] for r in fconn.execute(
                    "SELECT id FROM verbatim_turns WHERE user_id=?", (user_id,)
                ).fetchall()]
        except Exception:
            ids = []

        if user_id == "default":
            c = fconn.execute("DELETE FROM verbatim_turns").rowcount
        else:
            c = fconn.execute("DELETE FROM verbatim_turns WHERE user_id=?", (user_id,)).rowcount
        fconn.commit()
        deleted = c or 0

        if ids:
            try:
                tconn = get_text_conn()
                placeholders = ",".join("?" for _ in ids)
                tconn.execute(f"DELETE FROM verbatim_fts_map WHERE turn_id IN ({placeholders})", ids)
                tconn.commit()
            except Exception as fe:
                logger.debug("verbatim FTS 清理跳过: %s", fe)
    except Exception as exc:
        logger.warning("cascade_delete_verbatim 降级: %s", exc)
    return deleted


def count_verbatim(user_id: str) -> int:
    """统计某租户原文条数（运维/验收用）。失败返回 0。"""
    if not user_id:
        return 0
    try:
        ensure_verbatim_schema()
        fconn = get_facts_conn()
        row = fconn.execute(
            "SELECT COUNT(*) AS n FROM verbatim_turns WHERE user_id=?", (user_id,)
        ).fetchone()
        return int(row["n"]) if row else 0
    except Exception:
        return 0


def count_verbatim_all() -> int:
    """统计全库原文条数（/health 探针用，服务级观测不涉租户操作）。失败返回 0。"""
    try:
        ensure_verbatim_schema()
        fconn = get_facts_conn()
        row = fconn.execute("SELECT COUNT(*) AS n FROM verbatim_turns").fetchone()
        return int(row["n"]) if row else 0
    except Exception:
        return 0
