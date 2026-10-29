"""native 内核(f0.6+++ WP3a):单库 memory.db = 记忆真源 + FTS5 + 向量 + 历史 + 墓碑 + 图边。

只做 mem0 Memory 的真实调用面(add/search/update/delete/get/get_all/history/delete_all),
每次写一个事务:正文、向量、历史、实体、图边同进同出——不再有多库双写裂痕。

  检索 = FTS5 trigram(bm25) ∪ 向量(cosine) → RRF(k=60);返回 score 取 cosine
        (与 mem0 同量纲,下游阈值/判语不用改),融合细节放 score_details。
  向量 = sqlite-vec 的 vec_distance_cosine(装了就用 C 实现);没装回落 numpy 整域一次矩阵运算。
  嵌入 = 与 mem0 同一份 embedder 配置(OpenAI 兼容 /embeddings),不新增服务;
        影子模式直接借 mem0 的 embedder 并共享缓存,零额外调用。
  图边 = 写入同事务派生三类:update_chain(旧版本→现行)、same_session(会话内相邻)、
        co_occurrence(共享确定性实体,每实体最多连最近 5 条)。

tombstones/graph_edges 直接复用现有 DDL 常量;facts/verbatim/core 等兼容表由迁移脚本按源库原样建表。
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import sqlite3
import struct
import threading
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from ducky.memory_kernel.registry import logger  # 复用包 logger(aiduMEM.memory_kernel),不新增采集点

SCHEMA_VERSION = 1
RRF_K = 60
# f0.7 复核（外审 haiku N4）：改写时新向量没算出来、旧向量暂留（_embed_pending）——语义腿不认这种
# 旧向量，否则搜旧值会命中已改成新值的记忆；FTS 腿照常（正文已是新值）。补嵌入：migrate_to_native --embed-missing。
# P5 自审 S-6：metadata 为空串/非 JSON 时 json_extract 会报 malformed JSON 掀翻整个租户的检索——先 json_valid。
_FRESH_VEC = (" AND COALESCE(CASE WHEN json_valid(m.metadata) THEN json_extract(m.metadata, '$._embed_pending')"
              " END, 0) <> 1")
_CO_OCC_FANOUT = 5


def _md(raw) -> dict:
    """metadata 列读成 dict;空串/非 JSON/非对象一律当空(P5 自审 S-6:坏一行不许掀翻整租户的检索)。"""
    try:
        v = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return v if isinstance(v, dict) else {}
_GENERIC_ENTITIES = {"user", "用户", "ai", "assistant", "助手"}   # 角色词连谁都共现,不算关系
_CORE_KEYS = {"data", "hash", "created_at", "updated_at", "id", "text_lemmatized",
              "user_id", "agent_id", "run_id", "actor_id", "role", "bank_id"}

_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS memories (
  mid INTEGER PRIMARY KEY,                 -- 稳定 rowid(VACUUM 不变),FTS/向量都挂它
  id TEXT NOT NULL, content TEXT NOT NULL,
  user_id TEXT NOT NULL, bank_id TEXT NOT NULL DEFAULT 'default',
  agent_id TEXT, run_id TEXT, session_id TEXT,
  category TEXT, memory_type TEXT NOT NULL DEFAULT 'FACTS',
  hash TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}',
  created_at TEXT, updated_at TEXT, expires_at TEXT,
  UNIQUE (id, user_id, bank_id)            -- 与 text_fts.memories 同一租户主键语义
);
CREATE INDEX IF NOT EXISTS idx_nm_scope ON memories(user_id, bank_id, created_at);
CREATE INDEX IF NOT EXISTS idx_nm_session ON memories(user_id, bank_id, session_id);
CREATE INDEX IF NOT EXISTS idx_nm_id ON memories(id);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  content, content=memories, content_rowid=mid, tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS nm_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, content) VALUES (new.mid, new.content); END;
CREATE TRIGGER IF NOT EXISTS nm_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.mid, old.content); END;
CREATE TRIGGER IF NOT EXISTS nm_au AFTER UPDATE OF content ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', old.mid, old.content);
  INSERT INTO memories_fts(rowid, content) VALUES (new.mid, new.content); END;
CREATE TABLE IF NOT EXISTS memory_vectors (
  mid INTEGER PRIMARY KEY REFERENCES memories(mid) ON DELETE CASCADE,
  dim INTEGER NOT NULL, embedding BLOB NOT NULL);   -- float32 小端,sqlite-vec 原生格式
CREATE TABLE IF NOT EXISTS memory_entities (
  mid INTEGER NOT NULL REFERENCES memories(mid) ON DELETE CASCADE,
  entity TEXT NOT NULL, PRIMARY KEY (mid, entity));
CREATE INDEX IF NOT EXISTS idx_nme_entity ON memory_entities(entity);
CREATE TABLE IF NOT EXISTS memory_history (       -- 列与 mem0 history.db 同形,迁移直拷
  id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, old_memory TEXT, new_memory TEXT,
  event TEXT NOT NULL, created_at TEXT, updated_at TEXT, is_deleted INTEGER DEFAULT 0,
  actor_id TEXT, role TEXT, user_id TEXT, bank_id TEXT);
CREATE INDEX IF NOT EXISTS idx_nmh_mem ON memory_history(memory_id);
CREATE TABLE IF NOT EXISTS memory_facts (          -- 记忆 → 派生事实(pattern_extract)的链接,改/删据此级联
  mid INTEGER NOT NULL REFERENCES memories(mid) ON DELETE CASCADE,
  fact_id INTEGER NOT NULL, PRIMARY KEY (mid, fact_id));
CREATE INDEX IF NOT EXISTS idx_nmf_fact ON memory_facts(fact_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def _pack(vec) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _cosines(blobs: list[bytes], qv) -> list:
    """numpy 回落:一批 float32 BLOB 对查询向量的余弦(零向量 → None)。"""
    import numpy as np
    m = np.frombuffer(b"".join(blobs), dtype="<f4").reshape(len(blobs), -1)
    q = np.asarray(qv, dtype=np.float32)
    den = np.linalg.norm(m, axis=1) * float(np.linalg.norm(q))
    sims = (m @ q) / np.where(den > 0, den, 1.0)
    return [float(x) if d > 0 else None for x, d in zip(sims, den)]


def _np_cosine_distance(a: bytes, b: bytes):
    """同名 SQL 函数(迁移脚本/临时查询用);热路径走 _cosines 批量算。"""
    if a is None or b is None or len(a) != len(b):
        return None
    c = _cosines([a], struct.unpack(f"<{len(b) // 4}f", b))[0]
    return None if c is None else 1.0 - c


def default_db_path() -> str:
    from ducky.utils import DATA_DIR
    return os.path.join(DATA_DIR, "memory.db")


def _and_tenant(sql: str, args: list, user_id=None, bank_id=None) -> tuple[str, list]:
    """按 id 定位时追加给定的租户等值条件(f0.7 P0-2);未给的维度不加。"""
    for col, val in (("user_id", user_id), ("bank_id", bank_id)):
        if val:
            sql, args = f"{sql} AND {col}=?", [*args, str(val)]
    return sql, args


def _tombstone_expiry():
    """与 facts.db 墓碑同一 TTL(AIDUMEI_TOMBSTONE_TTL_DAYS,默认 30,0=永不过期)。"""
    from ducky.env_config import int_env
    days = int_env("AIDUMEI_TOMBSTONE_TTL_DAYS", 30, minimum=0)
    import time
    return int(time.time() + days * 86400) if days > 0 else None


def _dedupe_fact_keys(conn: sqlite3.Connection) -> int:
    """f0.7 P0-4(外审 ds A-01):旧库 facts 没有唯一约束,同键多行时建唯一索引会抛 IntegrityError、
    整个内核打不开。建索引前先查重:每组留 updated_at 最新的一行,其余进墓碑后删除。"""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_facts_unique'").fetchone():
        return 0
    dups = conn.execute(
        "SELECT agent_id, user_id, bank_id, category, fact_key FROM facts"
        " GROUP BY agent_id, user_id, bank_id, category, fact_key HAVING COUNT(*) > 1").fetchall()
    n = 0
    for a, u, b, cat, key in dups:
        rows = conn.execute(
            "SELECT id, fact_value FROM facts WHERE agent_id IS ? AND user_id=? AND bank_id=? AND category=?"
            " AND fact_key=? ORDER BY COALESCE(updated_at, created_at) DESC, id DESC", (a, u, b, cat, key)).fetchall()
        for fid, val in rows[1:]:
            conn.execute("INSERT INTO tombstones (target_id,target_type,user_id,bank_id,content_snapshot,reason,actor,"
                         "tombstoned_at,snapshot_expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
                         (str(fid), "fact", u, b, f"{key}={val}", "duplicate fact key before unique index",
                          "native_kernel", _now(), _tombstone_expiry()))
            conn.execute("DELETE FROM facts WHERE id=?", (fid,))
            n += 1
    if n:
        logger.warning("native: 建 facts 唯一索引前合并了 %d 条同键重复事实(已进墓碑)", n)
    return n


def connect(path: str) -> sqlite3.Connection:
    """打开并就位 schema(幂等)。sqlite-vec 可用就加载,否则注册同名纯 Python 函数。"""
    conn = sqlite3.connect(path, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except (ImportError, AttributeError, sqlite3.OperationalError):
        conn.create_function("vec_distance_cosine", 2, _np_cosine_distance, deterministic=True)
    from ducky.tombstone import _TOMBSTONES_DDL, _TOMBSTONE_INDEXES
    from ducky.graph_memory import _DDL as _GRAPH_DDL
    from ducky.schema_bootstrap import _FACTS_DDL
    conn.executescript(_DDL + _TOMBSTONES_DDL + ";" + ";".join(_TOMBSTONE_INDEXES) + ";" + _GRAPH_DDL
                       + ";" + _FACTS_DDL)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(facts)")}
    if "user_id" not in cols:     # 生产 facts 的租户列是后加的;与 schema_bootstrap 迁移同形
        conn.execute("ALTER TABLE facts ADD COLUMN user_id TEXT NOT NULL DEFAULT 'default'")
    if "bank_id" not in cols:
        conn.execute("ALTER TABLE facts ADD COLUMN bank_id TEXT NOT NULL DEFAULT 'default'")
    tcols = {r[1] for r in conn.execute("PRAGMA table_info(tombstones)")}
    if "snapshot_expires_at" not in tcols:      # f0.7 P0-3:native 墓碑同样受 TTL 约束
        conn.execute("ALTER TABLE tombstones ADD COLUMN snapshot_expires_at INTEGER")
    _dedupe_fact_keys(conn)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_unique "
                 "ON facts(agent_id, user_id, bank_id, category, fact_key)")
    conn.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    conn.commit()
    return conn


class HttpEmbedder:
    """OpenAI 兼容 /embeddings 的最小客户端;读 mem0 同一份 embedder 配置(含密钥解析链)。"""

    def __init__(self, cfg: dict):
        c = dict(cfg.get("config") or {})
        if (cfg.get("provider") or "openai") != "openai":
            raise RuntimeError(f"native 内核只支持 OpenAI 兼容 embedder,当前 {cfg.get('provider')!r}")
        self.model, self.dims = c.get("model") or "text-embedding-3-small", c.get("embedding_dims")
        self.base = (c.get("openai_base_url") or "https://api.openai.com/v1").rstrip("/")
        self._key, self._client = c.get("api_key") or "", None

    def embed(self, text, memory_action=None):
        import httpx
        from ducky.engine_mode import cloud_egress_allowed
        if not cloud_egress_allowed("embedding"):        # local 档:不外呼,调用方降级为 FTS-only
            raise RuntimeError("blocked_by_engine_mode")
        if self._client is None:
            self._client = httpx.Client(timeout=5)    # CC 钩子预算短:嵌入挂了 5 秒内退 FTS-only
        body = {"input": [str(text).replace("\n", " ")], "model": self.model, "encoding_format": "float"}
        if self.dims:
            body["dimensions"] = self.dims
        try:
            r = self._client.post(self.base + "/embeddings", json=body,
                                  headers={"Authorization": f"Bearer {self._key}"})
            r.raise_for_status()
            return r.json()["data"][0]["embedding"]
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:   # 统一成 RuntimeError 交给 _embed 降级
            raise RuntimeError(f"embedding failed: {type(exc).__name__}") from exc


def _texts(messages) -> list[tuple[str, str]]:
    """mem0 infer=False 语义:每条非 system 消息各成一条记忆。"""
    if isinstance(messages, str):
        return [("user", messages)] if messages.strip() else []
    if isinstance(messages, dict):
        messages = [messages]
    out = []
    for m in messages or []:
        role, content = (m.get("role") or "user"), m.get("content")
        if role != "system" and isinstance(content, str) and content.strip():
            out.append((role, content))
    return out


def _entities(text: str) -> list[str]:
    from ducky.graph_memory import _extract_entities
    ents = {e.strip().lower() for e in _extract_entities(text)}
    return sorted(e for e in ents if 1 < len(e) <= 64 and e not in _GENERIC_ENTITIES)[:12]


class NativeMemory:
    """mem0 Memory 形状的单库内核句柄。"""

    def __init__(self, path: str = "", embedder=None, derive_facts: bool = False):
        self.path = path or default_db_path()
        self.embedding_model = embedder
        # native 档由内核派生 pattern 事实并链接(改/删级联);影子档关闭——事实仍由旧链路写 facts.db,
        # 避免与迁移导入的 facts id 撞号。
        self.derive_facts = derive_facts
        # 单连接 + 可重入锁:线程池里每线程一连接实测 RSS +60MB;SQLite 写本就串行,读 ~1ms
        self.conn = connect(self.path)
        self._lock = threading.RLock()
        self._sqlite_vec = self.conn.execute(
            "SELECT 1 FROM pragma_function_list WHERE name='vec_version'").fetchone() is not None
        self.vector_store = NativeVectorStore(self)   # mem0 VectorStore 形状,client=None
        # f0.7 P0-5(外审 ds A-04):嵌入是网络 I/O,不许持全库锁。insert/add/update/search 先在锁外算好
        # 向量,再在方法体内自己拿锁;其余纯 SQL 方法整体包锁。
        for name in ("delete", "delete_all", "get", "get_all", "history"):
            setattr(self, name, self._locked(getattr(self, name)))

    supports_scope = True      # f0.7 P0-2:按 id 的读改删可带 (user_id, bank_id) 定位;调用方据此传租户

    def _locked(self, fn):
        @functools.wraps(fn)          # 保留签名:mutation journal 按 signature.bind 取 user_id/metadata
        def run(*a, **kw):
            with self._lock:
                return fn(*a, **kw)
        return run

    # ── 工具 ──────────────────────────────────────────────────────
    def _embed(self, text: str, action: str):
        if self.embedding_model is None:
            return None
        try:
            return self.embedding_model.embed(text, action)
        except RuntimeError as exc:       # 向量失败不丢正文:FTS 仍可召回,迁移脚本 --embed-missing 可补
            logger.warning("native embed failed (%s): %s", action, exc)
            return None

    @staticmethod
    def _item(r: sqlite3.Row, score=None) -> dict:
        md = _md(r["metadata"])
        md.setdefault("bank_id", r["bank_id"])
        it = {"id": r["id"], "memory": r["content"], "hash": r["hash"], "metadata": md,
              "created_at": r["created_at"], "updated_at": r["updated_at"], "user_id": r["user_id"]}
        for k in ("agent_id", "run_id"):
            if r[k]:
                it[k] = r[k]
        if score is not None:
            it["score"] = score
        return it

    @staticmethod
    def _where(filters: dict | None) -> tuple[str, list]:
        f = dict(filters or {})
        if not f.get("user_id"):
            raise ValueError("filters 必须含 user_id")
        sql, args = ["m.user_id=?"], [f.pop("user_id")]
        for col in ("bank_id", "agent_id", "run_id"):     # 默认域不下推 bank(与 mem0 存量语义一致)
            if f.get(col) not in (None, ""):
                sql.append(f"m.{col}=?")
                args.append(f.pop(col))
        for k, v in f.items():
            if isinstance(v, (dict, list)):
                raise ValueError(f"native 内核不支持算子过滤 {k!r}(调用方实测只用等值)")
            if not re.fullmatch(r"[A-Za-z0-9_]+", k):
                raise ValueError(f"非法过滤键 {k!r}")
            sql.append(f"json_extract(m.metadata, '$.{k}')=?")
            args.append(v)
        sql.append("(m.expires_at IS NULL OR m.expires_at='' OR m.expires_at>=date('now'))")
        return " AND ".join(sql), args

    def _row(self, memory_id: str, user_id=None, bank_id=None):
        """f0.7 P0-2(外审六路共识):按 id 定位必须带租户。唯一键是 (id,user_id,bank_id),同 id 可在多租户
        合法共存;给了租户就按租户找,没给且命中多租户 → 歧义,拒绝(fail-closed),绝不挑第一行。"""
        sql, args = _and_tenant("SELECT * FROM memories WHERE id=?", [str(memory_id)], user_id, bank_id)
        rows = self.conn.execute(sql + " LIMIT 2", args).fetchall()
        if len(rows) > 1:
            raise ValueError(f"memory id {memory_id!r} exists in more than one tenant; pass user_id/bank_id")
        return rows[0] if rows else None

    def _edge(self, c, st, si, dt, di, et, uid, bank):
        c.execute("INSERT OR IGNORE INTO graph_edges (src_type,src_id,dst_type,dst_id,edge_type,user_id,bank_id)"
                  " VALUES (?,?,?,?,?,?,?)", (st, str(si), dt, str(di), et, uid, bank))

    def _link_entities(self, c, mid: int, mem_id: str, text: str, uid: str, bank: str) -> None:
        c.execute("DELETE FROM memory_entities WHERE mid=?", (mid,))
        for ent in _entities(text):
            c.execute("INSERT OR IGNORE INTO memory_entities VALUES (?,?)", (mid, ent))
            for (other,) in c.execute(
                    "SELECT m.id FROM memory_entities e JOIN memories m ON m.mid=e.mid WHERE e.entity=?"
                    " AND m.user_id=? AND m.bank_id=? AND m.mid<>? ORDER BY m.mid DESC LIMIT ?",
                    (ent, uid, bank, mid, _CO_OCC_FANOUT)).fetchall():
                self._edge(c, "memory", other, "memory", mem_id, "co_occurrence", uid, bank)

    def _history(self, c, mem_id, old, new, event, uid, bank, role=None) -> str:
        hid, now = str(uuid.uuid4()), _now()
        c.execute("INSERT INTO memory_history (id,memory_id,old_memory,new_memory,event,created_at,updated_at,"
                  "is_deleted,role,user_id,bank_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (hid, mem_id, old, new, event, now, now, int(event == "DELETE"), role, uid, bank))
        return hid

    def _derive_facts(self, c, mid: int, text: str, uid: str, bank: str, recorded_at=None) -> None:
        """pattern_extract 派生事实落 facts 并链接到本条记忆。人工/其它来源的同键事实不覆盖。"""
        from ducky.pattern_extract import (MAX_FACTS_PER_ADD, PATTERN_EXTRACT_SOURCE, extract_patterns,
                                           is_pattern_extract_enabled)
        if not (self.derive_facts and is_pattern_extract_enabled()):
            return
        now = _now()
        for it in extract_patterns(text, recorded_at=recorded_at)[:MAX_FACTS_PER_ADD]:
            c.execute("INSERT INTO facts (category,fact_key,fact_value,source,user_id,bank_id,created_at,updated_at)"
                      " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(agent_id,user_id,bank_id,category,fact_key) DO UPDATE"
                      " SET fact_value=excluded.fact_value, updated_at=excluded.updated_at, archived=0"
                      " WHERE facts.source=excluded.source",
                      (it["category"], it["fact_key"], it["fact_value"], PATTERN_EXTRACT_SOURCE, uid, bank, now, now))
            row = c.execute("SELECT id FROM facts WHERE user_id=? AND bank_id=? AND category=? AND fact_key=?"
                            " AND source=? AND fact_value=?", (uid, bank, it["category"], it["fact_key"],
                                                              PATTERN_EXTRACT_SOURCE, it["fact_value"])).fetchone()
            if row:
                c.execute("INSERT OR IGNORE INTO memory_facts VALUES (?,?)", (mid, row[0]))

    def _cascade_facts(self, c, mid: int) -> None:
        """解除本条记忆的派生事实:无人再链接的删掉(留墓碑);仍被别条链接的,值回退到最近那条的派生值。"""
        from ducky.pattern_extract import extract_patterns
        fids = [r[0] for r in c.execute("SELECT fact_id FROM memory_facts WHERE mid=?", (mid,))]
        c.execute("DELETE FROM memory_facts WHERE mid=?", (mid,))
        for fid in fids:
            f = c.execute("SELECT * FROM facts WHERE id=?", (fid,)).fetchone()
            if f is None:
                continue
            other = c.execute("SELECT m.content, m.created_at FROM memory_facts l JOIN memories m ON m.mid=l.mid"
                              " WHERE l.fact_id=? ORDER BY m.mid DESC LIMIT 1", (fid,)).fetchone()
            vals = [it["fact_value"] for it in (extract_patterns(other[0], recorded_at=other[1]) if other else [])
                    if it["category"] == f["category"] and it["fact_key"] == f["fact_key"]]
            if vals:
                c.execute("UPDATE facts SET fact_value=?, updated_at=? WHERE id=?", (vals[0], _now(), fid))
                continue
            c.execute("INSERT INTO tombstones (target_id,target_type,user_id,bank_id,content_snapshot,reason,actor,"
                      "tombstoned_at,snapshot_expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
                      (str(fid), "fact", f["user_id"], f["bank_id"], f"{f['fact_key']}={f['fact_value']}",
                       "source memory changed", "native_kernel", _now(), _tombstone_expiry()))
            c.execute("DELETE FROM facts WHERE id=?", (fid,))

    def _remove(self, c, r, reason: str, actor: str) -> None:
        """单条删除的全部层:墓碑、历史、派生事实、图边、正文(向量/实体/链接随 CASCADE,FTS 随触发器)。

        f0.7 P0-3(外审 sesame F-03):删除即擦除。墓碑快照是唯一保留的原文(可恢复,受 TTL 约束);
        这条记忆此前的 ADD/UPDATE 历史行连同原文一并清掉,只留一行不含正文的 DELETE 标记。"""
        c.execute("INSERT INTO tombstones (target_id,target_type,user_id,bank_id,content_snapshot,reason,actor,"
                  "tombstoned_at,snapshot_expires_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  (r["id"], "memory", r["user_id"], r["bank_id"], r["content"], reason, actor, _now(),
                   _tombstone_expiry()))
        c.execute("DELETE FROM memory_history WHERE memory_id=? AND (user_id IS NULL OR (user_id=? AND bank_id=?))",
                  (r["id"], r["user_id"], r["bank_id"]))
        self._history(c, r["id"], None, None, "DELETE", r["user_id"], r["bank_id"])
        self._cascade_facts(c, r["mid"])
        c.execute("DELETE FROM graph_edges WHERE user_id=? AND bank_id=? AND ((src_type='memory' AND src_id=?)"
                  " OR (dst_type='memory' AND dst_id=?))", (r["user_id"], r["bank_id"], r["id"], r["id"]))
        c.execute("DELETE FROM memories WHERE mid=?", (r["mid"],))

    def _drop_copies(self, c, r, reason: str) -> list:
        """同域同文的**原文副本**(memory_type=VERBATIM,迁移并入的 verbatim:N 抽屉行)一并清掉——对齐
        mem0 侧 replace_stale_copies 的范围(只清原文副本,不碰正式记忆),否则旧值/已删内容会借副本被召回。

        f0.7 P0-1(外审 haiku/qwen/ds/muse):此前只凭 hash 判副本,同租户里每一条同文的独立记忆
        (逐轮原话里的「好的」「[SILENT]」、不同会话说过的同一句话)都会被连带删掉,生产库实测 10 组 34 条。
        现在只认原文副本;返回被连带清掉的 id,调用方如实回报。"""
        same = c.execute("SELECT * FROM memories WHERE user_id=? AND bank_id=? AND hash=? AND mid<>?",
                         (r["user_id"], r["bank_id"], r["hash"], r["mid"])).fetchall()
        copies = [cp for cp in same if cp["memory_type"] == "VERBATIM"]
        if r["memory_type"] != "VERBATIM":
            # f0.7 P5 自查:与单删原文层同一认领规则(verbatim_vault.allocate_verbatim_claim)——还有别的独立
            # 记忆说着同一句时,给它们各留一份原文副本(先按会话,会话不明从严),其余带走。
            from ducky.verbatim_vault import allocate_verbatim_claim
            claim = set(allocate_verbatim_claim([(cp["mid"], cp["session_id"]) for cp in copies],
                                                [cp["session_id"] or "" for cp in same if cp["memory_type"] != "VERBATIM"]))
            copies = [cp for cp in copies if cp["mid"] in claim]
        gone = []
        for cp in copies:
            self._remove(c, cp, reason, "native_kernel")
            gone.append(cp["id"])
        return gone

    def restore_copies(self, memory_id, *, user_id, bank_id="default") -> int:
        """f0.7 P5 自查:恢复一条记忆时,把删除时连带清掉的原文副本(墓碑 reason=copy of deleted <id>)一并放回。
        此前 /tombstone/restore 只回主记忆,副本永久丢失,native 档原文召回因此缺腿。"""
        with self._lock:
            rows = self.conn.execute(
                "SELECT tombstone_id, target_id, content_snapshot, user_id, bank_id FROM tombstones"
                " WHERE target_type='memory' AND reason=? AND restored_at IS NULL AND content_snapshot IS NOT NULL",
                (f"copy of deleted {memory_id}",)).fetchall()
        n = 0
        for tid, cid, text, u, b in rows:
            if (u, b) != (user_id, bank_id):          # 只放回本租户的副本
                continue
            self.insert(cid, text, user_id=user_id, metadata={"bank_id": bank_id, "memory_type": "VERBATIM"},
                        vector=[], history=False)
            with self._lock, self.conn as c:
                c.execute("UPDATE tombstones SET restored_at=? WHERE tombstone_id=?", (_now(), tid))
            n += 1
        return n

    # ── 写路径(每个公开方法 = 一个事务) ─────────────────────────────
    def insert(self, mem_id: str, text: str, *, user_id: str, metadata: dict | None = None,
               agent_id=None, run_id=None, role="user", vector=None, created_at=None, history: bool = True) -> bool:
        """单条落库(影子镜像/迁移共用)。同 (id,租户) 已存在 → 幂等跳过,返回 False。"""
        if not str(user_id or "").strip():
            raise ValueError("native insert 需要非空 user_id")       # f0.7 P0-2(外审 ds A-07):无租户行永远查不到
        vec = vector if vector is not None else self._embed(text, "add")    # 锁外嵌入
        with self._lock, self.conn:
            return self._write(mem_id, text, vec, user_id=user_id, metadata=metadata, agent_id=agent_id,
                               run_id=run_id, role=role, created_at=created_at, history=history)

    def _write(self, mem_id, text, vec, *, user_id, metadata=None, agent_id=None, run_id=None, role="user",
               created_at=None, history=True) -> bool:
        """insert 的事务体(调用方持有事务):add 多条消息共用一个事务,失败整笔回滚。"""
        md = dict(metadata or {})
        bank = str(md.get("bank_id") or "default")
        session = md.get("_origin_session_id") or md.get("session_id") or run_id or None
        now = created_at or md.pop("created_at", None) or _now()
        extra = {k: v for k, v in md.items() if k not in _CORE_KEYS}
        if role and role != "user":
            extra["role"] = role
        c = self.conn
        cur = c.execute(
            "INSERT OR IGNORE INTO memories (id,content,user_id,bank_id,agent_id,run_id,session_id,category,"
            "memory_type,hash,metadata,created_at,updated_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mem_id, text, user_id, bank, agent_id, run_id, session, md.get("category"),
             md.get("memory_type") or "FACTS", hashlib.md5(text.encode()).hexdigest(),
             json.dumps(extra, ensure_ascii=False, default=str), now, now, md.get("expiration_date")))
        if not cur.rowcount:
            return False
        mid = cur.lastrowid
        if vec:
            c.execute("INSERT INTO memory_vectors VALUES (?,?,?)", (mid, len(vec), _pack(vec)))
        if history:
            self._history(c, mem_id, None, text, "ADD", user_id, bank, role)
        self._link_entities(c, mid, mem_id, text, user_id, bank)
        self._derive_facts(c, mid, text, user_id, bank, md.get("recorded_at") or now)
        if session:
            prev = c.execute("SELECT id FROM memories WHERE user_id=? AND bank_id=? AND session_id=?"
                             " AND mid<? ORDER BY mid DESC LIMIT 1", (user_id, bank, session, mid)).fetchone()
            if prev:
                self._edge(c, "memory", prev[0], "memory", mem_id, "same_session", user_id, bank)
        return True

    def add(self, messages, *, user_id=None, agent_id=None, run_id=None, metadata=None,
            infer=True, memory_type=None, prompt=None, **_kw):
        """native /add = 只存原话(2026-10-27 维护者拍板:不做逐轮 LLM 抽取,蒸馏只走 SessionEnd 的
        /session/distill)。infer 参数只为契约兼容而接收、不生效。若将来要逐轮抽取,接在这里:
        先 LLM 抽事实、再逐条 _write(同一事务),不要改 HTTP 层。整笔一个事务,失败无半写。"""
        if not user_id:
            raise ValueError("native add 需要 user_id")
        md = dict(metadata or {})
        md.pop("created_at", None)        # 与 mem0 同:created_at 恒为 ISO 入库时间(时间衰减靠它);事件时间在 recorded_at
        if memory_type:
            md["memory_type"] = memory_type
        md.setdefault("lane", "raw")      # 逐轮原话标记:打分层据此给长原话降权(蒸馏线自带 lane=distill)
        todo = [(str(uuid.uuid4()), role, text, self._embed(text, "add")) for role, text in _texts(messages)]
        with self._lock, self.conn:       # 嵌入已在锁外算完;正文/向量/历史/图边一个事务
            for mem_id, role, text, vec in todo:
                self._write(mem_id, text, vec, user_id=user_id, metadata=md, agent_id=agent_id, run_id=run_id,
                            role=role)
        return {"results": [{"id": i, "memory": t, "event": "ADD"} for i, _r, t, _v in todo]}

    def _apply_update(self, c, r, new: str, md: dict, vec, *, memory_type=None, category=None) -> list:
        """改一条记忆的事务体(update 与 vector_store 旁路共用,调用方持锁并持有事务)。

        f0.7 P0-5(外审 haiku P2-7):新向量没算出来时不删旧向量,打 _embed_pending 标记等补;
        f0.7 P0-2(外审 muse P1-05):旁路写也走这里,历史/图边/实体/副本/事实一律照走。"""
        changed = new != r["content"]
        if changed and not vec:
            md = {**md, "_embed_pending": True}
        elif vec:
            md = {k: v for k, v in md.items() if k != "_embed_pending"}
        c.execute("UPDATE memories SET content=?, hash=?, memory_type=COALESCE(?, memory_type),"
                  " category=COALESCE(?, category), metadata=?, updated_at=? WHERE mid=?",
                  (new, hashlib.md5(new.encode()).hexdigest(), memory_type, category,
                   json.dumps(md, ensure_ascii=False, default=str), _now(), r["mid"]))
        if vec:
            c.execute("INSERT OR REPLACE INTO memory_vectors VALUES (?,?,?)", (r["mid"], len(vec), _pack(vec)))
        if not changed:
            return []
        hid = self._history(c, r["id"], r["content"], new, "UPDATE", r["user_id"], r["bank_id"])
        self._edge(c, "memory_version", hid, "memory", r["id"], "update_chain", r["user_id"], r["bank_id"])
        self._link_entities(c, r["mid"], r["id"], new, r["user_id"], r["bank_id"])
        gone = self._drop_copies(c, r, f"superseded by update of {r['id']}")
        self._cascade_facts(c, r["mid"])
        self._derive_facts(c, r["mid"], new, r["user_id"], r["bank_id"], md.get("recorded_at") or r["created_at"])
        return gone

    def update(self, memory_id, text=None, metadata=None, data=None, *, user_id=None, bank_id=None, **_kw):
        text = text if text is not None else data
        vec = self._embed(text, "update") if text is not None else None      # 锁外嵌入
        with self._lock:
            r = self._row(memory_id, user_id, bank_id)
            if r is None:
                raise ValueError(f"Memory with id {memory_id} not found")
            md = _md(r["metadata"])
            md.update({k: v for k, v in (metadata or {}).items() if k not in _CORE_KEYS})
            with self.conn as c:
                gone = self._apply_update(c, r, text if text is not None else r["content"], md, vec)
        return {"message": "Memory updated successfully!", "collateral_deleted": gone}

    def delete(self, memory_id, *, reason: str = "", actor: str = "system", user_id=None, bank_id=None):
        r = self._row(memory_id, user_id, bank_id)
        if r is None:
            raise ValueError(f"Memory with id {memory_id} not found")
        with self.conn as c:
            gone = self._drop_copies(c, r, f"copy of deleted {r['id']}")
            self._remove(c, r, reason, actor)
        return {"message": "Memory deleted successfully!", "collateral_deleted": gone}

    def delete_all(self, user_id=None, agent_id=None, run_id=None, **_kw):
        """f0.7 P0-1(外审 muse P1-04):整域一个事务;前面某条连带清掉的副本,轮到它时幂等跳过。"""
        flt = {k: v for k, v in (("user_id", user_id), ("agent_id", agent_id), ("run_id", run_id)) if v}
        where, args = self._where(flt)
        n = 0
        with self.conn as c:
            for (mid,) in c.execute(f"SELECT m.mid FROM memories m WHERE {where}", args).fetchall():
                r = c.execute("SELECT * FROM memories WHERE mid=?", (mid,)).fetchone()
                if r is None:
                    continue
                self._drop_copies(c, r, f"copy of deleted {r['id']}")
                self._remove(c, r, "delete_all", "system")
                n += 1
        return {"message": "Memories deleted successfully!", "deleted": n}

    # ── 读路径 ────────────────────────────────────────────────────
    def get(self, memory_id, *, user_id=None, bank_id=None):
        r = self._row(memory_id, user_id, bank_id)
        return self._item(r) if r else None

    def get_all(self, *, filters=None, top_k=20, limit=None, **_kw):
        where, args = self._where(filters)
        rows = self.conn.execute(f"SELECT m.* FROM memories m WHERE {where} ORDER BY m.mid DESC LIMIT ?",
                                 (*args, int(limit or top_k))).fetchall()
        return {"results": [self._item(r) for r in rows]}

    def history(self, memory_id, *, user_id=None, bank_id=None):
        sql, args = _and_tenant("SELECT * FROM memory_history WHERE memory_id=?", [str(memory_id)], user_id, bank_id)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY created_at", args).fetchall()]

    def _fts(self, query: str, where: str, args: list, n: int) -> list[int]:
        """与 text_fts 同口径:>=3 字词元走 trigram MATCH;混有 2 字词元(如「跑分」)时 MATCH 不完整,
        再用 LIKE 补这些短词的命中(排在 MATCH 之后)。"""
        from ducky.text_fts import _fts_terms, fts_is_authoritative, fts_match_terms
        terms, out = fts_match_terms(query)[:12], []
        if terms:
            expr = " OR ".join('"%s"' % t.replace('"', '""') for t in terms)
            try:
                out = [r[0] for r in self.conn.execute(
                    f"SELECT m.mid FROM memories_fts f JOIN memories m ON m.mid=f.rowid WHERE memories_fts MATCH ?"
                    f" AND {where} ORDER BY bm25(memories_fts) LIMIT ?", (expr, *args, n)).fetchall()]
            except sqlite3.OperationalError as exc:
                logger.debug("native FTS skipped: %s", exc)
            if fts_is_authoritative(query):
                return out
        short = [t for t in _fts_terms(query) if len(t) == 2][:4]
        if not short:
            short = re.findall(r"[\u4e00-\u9fff]{2}|[A-Za-z0-9]{2}", query or "")[:4]
        if not short:
            return out
        like = " OR ".join("m.content LIKE ?" for _ in short)
        seen = set(out)
        out += [r[0] for r in self.conn.execute(f"SELECT m.mid FROM memories m WHERE {where} AND ({like})"
                                                f" ORDER BY m.mid DESC LIMIT ?",
                                                (*args, *[f"%{s}%" for s in short], n)).fetchall()
                if r[0] not in seen]
        return out[:n]

    def search(self, query, *, top_k=20, filters=None, threshold=0.1, limit=None, **_kw):
        where, args = self._where(filters)        # 参数先校验,不为坏请求白花一次嵌入
        qv = self._embed(query, "search")          # 锁外嵌入(f0.7 P0-5)
        with self._lock:
            return self._search_locked(query, qv, where, args, top_k=top_k, threshold=threshold, limit=limit)

    def _search_locked(self, query, qv, where, args, *, top_k, threshold, limit):
        k = max(1, min(int(limit or top_k), 300))
        n = max(k * 4, 40)
        fts = self._fts(query, where, args, n)
        cos: dict[int, float] = {}
        vec: list[int] = []
        if qv and not self._sqlite_vec:        # numpy:整域一次矩阵乘,FTS 命中的余弦顺带有了
            rows = self.conn.execute(f"SELECT m.mid, v.embedding FROM memory_vectors v JOIN memories m"
                                     f" ON m.mid=v.mid WHERE {where} AND v.dim=?{_FRESH_VEC}",
                                     (*args, len(qv))).fetchall()
            try:
                sims = _cosines([r[1] for r in rows], qv) if rows else []
            except ImportError as exc:            # f0.7(外审 ds A-13):缺 numpy 降级 FTS-only,不 500
                logger.warning("native vector leg skipped (numpy unavailable): %s", exc)
                rows, sims = [], []
            if rows:
                for (mid, _b), c in zip(rows, sims):
                    if c is not None:
                        cos[mid] = c
                vec = [m for m in sorted(cos, key=lambda m: -cos[m])[:n] if cos[m] >= (threshold or 0.0)]
        elif qv:
            blob = _pack(qv)
            for mid, d in self.conn.execute(
                    f"SELECT m.mid, vec_distance_cosine(v.embedding, ?) d FROM memory_vectors v JOIN memories m"
                    f" ON m.mid=v.mid WHERE {where} AND v.dim=?{_FRESH_VEC} ORDER BY d LIMIT ?",
                    (blob, *args, len(qv), n)).fetchall():
                if d is not None:
                    cos[mid] = 1.0 - d
                    vec.append(mid)
            vec = [m for m in vec if cos[m] >= (threshold or 0.0)]
            missing = [m for m in fts if m not in cos]
            if missing:
                q = ",".join("?" * len(missing))
                for mid, d in self.conn.execute(f"SELECT v.mid, vec_distance_cosine(v.embedding, ?) FROM memory_vectors v"
                                                f" JOIN memories m ON m.mid=v.mid WHERE v.mid IN ({q}) AND v.dim=?"
                                                f"{_FRESH_VEC}", (blob, *missing, len(qv))):
                    cos[mid] = 1.0 - d if d is not None else 0.0
        rrf: dict[int, float] = {}
        for ranks in (fts, vec):
            for i, mid in enumerate(ranks):
                rrf[mid] = rrf.get(mid, 0.0) + 1.0 / (RRF_K + i + 1)
        top = sorted(rrf, key=lambda m: -rrf[m])[:k]
        if not top:
            return {"results": []}
        rows = {r["mid"]: r for r in self.conn.execute(
            f"SELECT * FROM memories WHERE mid IN ({','.join('?' * len(top))})", top).fetchall()}
        out = []
        for mid in top:
            if mid not in rows:       # 另一进程/实例刚删掉:跳过,不抛 KeyError(外审 muse P2-04)
                continue
            it = self._item(rows[mid], round(cos.get(mid, 0.0), 6))
            it["score_details"] = {"rrf": round(rrf[mid], 6), "bm25_rank": fts.index(mid) + 1 if mid in fts else None,
                                   "vector_rank": vec.index(mid) + 1 if mid in vec else None}
            out.append(it)
        return {"results": out}


class NativeVectorStore:
    """mem0 VectorStore 形状的薄适配器(get/insert/update/set_payload/delete/list),全走 memory.db。

    给还在摸 `mem.vector_store.*` 的旧模块(墓碑快照/恢复、CoreMemory 向量块、六型同步)一个真实后端;
    payload 用 mem0 写入形状(data/hash/user_id/bank_id/... + metadata 平铺)。client=None:
    任何还想直连 Qdrant 的代码当场显式失败,不会悄悄写到别处。"""

    is_native = True
    client = None
    collection_name = ""

    def __init__(self, mem: "NativeMemory"):
        self.mem = mem

    @staticmethod
    def _out(r):
        p = _md(r["metadata"])
        p.update(data=r["content"], hash=r["hash"], user_id=r["user_id"], bank_id=r["bank_id"],
                 created_at=r["created_at"], updated_at=r["updated_at"], memory_type=r["memory_type"])
        for k in ("agent_id", "run_id", "category"):
            if r[k]:
                p[k] = r[k]
        return SimpleNamespace(id=r["id"], score=None, payload=p)

    def get(self, vector_id, user_id=None, bank_id=None):
        with self.mem._lock:
            r = self.mem._row(vector_id, user_id, bank_id)
            return self._out(r) if r else None

    def list(self, filters=None, limit=None):
        with self.mem._lock:
            where, args = self.mem._where(filters)
            rows = self.mem.conn.execute(f"SELECT m.* FROM memories m WHERE {where} ORDER BY m.mid DESC LIMIT ?",
                                         (*args, int(limit or 100))).fetchall()
            return [[self._out(r) for r in rows]]

    def _replace(self, r, payload: dict, vector=None) -> None:
        """同 id 覆盖(Qdrant upsert 语义)。f0.7 P0-2(外审 muse P1-05):不再绕开内核不变量直接 UPDATE——
        正文变了就走 _apply_update,历史/图边/实体/副本/派生事实照走;无新向量时旧向量保留并标待补。"""
        text = str(payload.get("data", r["content"]))
        md = {k: v for k, v in payload.items() if k not in _CORE_KEYS}
        with self.mem.conn as c:
            self.mem._apply_update(c, r, text, md, vector, memory_type=payload.get("memory_type"),
                                   category=payload.get("category"))

    def insert(self, vectors, payloads=None, ids=None):
        for i, vec in enumerate(vectors):
            p = dict((payloads or [{}] * len(vectors))[i] or {})
            vid = str(ids[i]) if ids else str(uuid.uuid4())
            uid = str(p.get("user_id") or "").strip()
            if not uid:
                raise ValueError("vector_store.insert 需要 payload.user_id")   # 外审 ds A-07:不落无租户行
            with self.mem._lock:
                r = self.mem._row(vid, uid, str(p.get("bank_id") or "default"))   # 只在本租户里找同 id
                if r is not None:
                    self._replace(r, p, vec)
                    continue
            self.mem.insert(vid, str(p.get("data", "")), user_id=uid, metadata=p,
                            agent_id=p.get("agent_id"), run_id=p.get("run_id"), vector=vec or [],
                            created_at=p.get("created_at"), history=False)

    def update(self, vector_id, vector=None, payload=None, user_id=None, bank_id=None):
        with self.mem._lock:
            r = self.mem._row(vector_id, user_id, bank_id)
            if r is None:
                raise ValueError(f"vector {vector_id} not found")
            self._replace(r, payload if payload is not None else self._out(r).payload, vector)

    def set_payload(self, vector_id, payload: dict, user_id=None, bank_id=None) -> None:
        """只并入给定键(Qdrant set_payload 语义),如六型标签。"""
        with self.mem._lock:
            r = self.mem._row(vector_id, user_id, bank_id)
            if r is not None:
                self._replace(r, {**self._out(r).payload, **payload})

    def delete(self, vector_id, user_id=None, bank_id=None):
        """f0.7 P0-2(外审 qwen F-2 / muse P1-05):不再无墓碑硬删;转调内核 delete(墓碑+历史+级联)。
        f0.7 复核(外审 haiku N8):必须带租户——无租户旁路一律拒绝(id 唯一时也不许跨租户删)。"""
        if not user_id:
            raise ValueError("vector_store.delete requires user_id (tenant-scoped delete only)")
        try:
            return self.mem.delete(vector_id, reason="vector_store.delete", actor="vector_store",
                                   user_id=user_id, bank_id=bank_id)
        except ValueError as exc:
            if "not found" in str(exc):
                return None
            raise


# ── 影子模式:写双份、读旧内核,同步算 top-5 重合率落 jsonl(不起线程) ─────────
class _CachedEmbed:
    """mem0 embedder 外挂一层小 LRU:mem0 先嵌入,native 同文本直接命中,零额外外呼。"""

    def __init__(self, inner, size: int = 256):
        self.inner, self.size, self._c = inner, size, {}
        self._lock = threading.Lock()

    def embed(self, text, memory_action=None):
        key = str(text)
        with self._lock:
            if key in self._c:
                return self._c[key]
        v = self.inner.embed(text, memory_action)
        with self._lock:
            self._c[key] = v
            if len(self._c) > self.size:
                self._c.pop(next(iter(self._c)))
        return v

    def __getattr__(self, name):
        return getattr(self.inner, name)


class ShadowMemory:
    """mem0 为主、native 为影。任何影子侧异常只记日志,绝不影响主路返回。"""

    def __init__(self, primary, native: NativeMemory, log_dir: str = ""):
        self.primary, self.native = primary, native
        from ducky.utils import DATA_DIR
        self.log_dir = log_dir or os.path.join(DATA_DIR, "shadow")
        cached = _CachedEmbed(primary.embedding_model)
        primary.embedding_model = cached
        native.embedding_model = cached

    def __getattr__(self, name):                 # llm / db / vector_store 等一律透传 mem0
        return getattr(self.primary, name)

    def _shadow(self, op: str, fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except Exception as exc:  # noqa: BLE001 —— 影子侧任何故障都不许波及主路(契约即宽捕获)
            logger.warning("shadow %s failed: %s", op, exc)
            self._log({"op": op, "error": type(exc).__name__})
            return None

    def _log(self, rec: dict) -> None:
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            rec["ts"] = _now()
            path = os.path.join(self.log_dir, f"shadow-{rec['ts'][:10].replace('-', '')}.jsonl")
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def add(self, messages, **kw):
        res = self.primary.add(messages, **kw)
        uid, md = kw.get("user_id"), kw.get("metadata")
        for it in (res or {}).get("results", []) if isinstance(res, dict) else []:
            ev, mid, text = it.get("event"), it.get("id"), it.get("memory") or ""
            if ev == "ADD" and mid and text:
                self._shadow("add", self.native.insert, mid, text, user_id=uid, metadata=md,
                             agent_id=kw.get("agent_id"), run_id=kw.get("run_id"))
            elif ev == "UPDATE" and mid:
                self._shadow("update", self.native.update, mid, text)
            elif ev == "DELETE" and mid:
                self._shadow("delete", self.native.delete, mid)
        return res

    def update(self, memory_id, *a, **kw):
        res = self.primary.update(memory_id, *a, **kw)
        self._shadow("update", self.native.update, memory_id, *a, **kw)
        return res

    def delete(self, memory_id, *a, **kw):
        res = self.primary.delete(memory_id, *a, **kw)
        self._shadow("delete", self.native.delete, memory_id)
        return res

    def search(self, query, **kw):
        res = self.primary.search(query, **kw)
        shadow = self._shadow("search", self.native.search, query, **kw)
        if shadow is not None:
            a = [r.get("id") for r in (res.get("results", res) if isinstance(res, dict) else res or [])[:5]]
            b = [r["id"] for r in shadow["results"][:5]]
            self._log({"op": "search", "q_fp": hashlib.sha256(str(query).encode()).hexdigest()[:12],
                       "old_n": len(a), "new_n": len(b),
                       "top5_overlap": round(len(set(a) & set(b)) / max(1, len(a)), 3) if a else None})
        return res


def shadow_summary(path: str) -> dict:
    """把一天的 jsonl 汇总成 shadow-YYYYMMDD.json 所需数字(由报告/人工调用,不排定时任务)。"""
    n = errs = 0
    overlaps = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            rec = json.loads(line)
            n += 1
            errs += "error" in rec
            if rec.get("top5_overlap") is not None:
                overlaps.append(rec["top5_overlap"])
    return {"records": n, "errors": errs, "searches": len(overlaps),
            "top5_overlap_mean": round(sum(overlaps) / len(overlaps), 4) if overlaps else None,
            "top5_overlap_ge_0_9": round(sum(o >= 0.9 for o in overlaps) / len(overlaps), 4) if overlaps else None}


# ── 工厂(注册表调用;单例,与 mem0 单例同生命周期) ───────────────────────
_SINGLETON: dict = {}
_LOCK = threading.Lock()


def create_native_kernel():
    with _LOCK:
        if "native" not in _SINGLETON:
            from ducky.mem0_runtime import Mem0NotConfiguredError, _read_resolved_mem0_config
            try:
                emb_cfg = _read_resolved_mem0_config().get("embedder") or {}
            except Mem0NotConfiguredError:
                from ducky.engine_mode import configured_mode
                if configured_mode() != "local":
                    raise
                emb_cfg = {}       # 本地档零外呼:没有配置文件也能起,向量降为 FTS-only
            # f0.7 P0-3(外审 haiku P1-2 / ds A-05):事实层单一真源 = facts.db(UI 与召回都读它,路由层
            # pattern_extract 写它,删除链按来源级联它)。内核不再往 memory.db 再派生一份。
            mem = NativeMemory(embedder=HttpEmbedder(emb_cfg),
                               derive_facts=False)
            from ducky.mutation_journal import install_memory_journal
            install_memory_journal(mem)       # native 的 add/update 同样进 mutation journal(写了没写有据)
            _SINGLETON["native"] = mem
        return _SINGLETON["native"]


def create_shadow_kernel():
    with _LOCK:
        if "shadow" not in _SINGLETON:
            from ducky.mem0_runtime import _get_mem0_memory
            _SINGLETON["shadow"] = ShadowMemory(_get_mem0_memory(), NativeMemory())
        return _SINGLETON["shadow"]


def reset_native_singletons() -> None:
    with _LOCK:
        _SINGLETON.clear()
