#!/usr/bin/env python3
"""
aiduMEM Memory Workspace — 活跃记忆工作区（L1 缓存 + SQLite 持久化）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
J-space 启发：全局广播区 ~25 概念上限 → 记忆工作区 ~20 条。

v11.1 Opus 升级：SQLite 持久化层，进程重启后工作区自动恢复。

— 核心机制 —
1. 维护一个容量有限的"活跃记忆"集合（默认 20 条）
2. 新查询先在工作区内搜索 → 命中则秒回（短路常规搜索）
3. 写入新记忆时自动推进工作区（LRU 淘汰）
4. 定时清理冷记忆（access_count 低 + 超时）
5. SQLite 持久化：每次写入同步落盘，启动时自动恢复
"""

import json, os, time, threading, logging, sqlite3
from typing import Optional
from collections import OrderedDict

from ducky.utils import quick_sim, DATA_DIR
from ducky.bank_contract import DEFAULT_BANK_ID, make_scope

logger = logging.getLogger("aiduMEM.workspace")

# ── 配置 ──
WORKSPACE_CAPACITY = 20       # 工作区最大容量
WORKSPACE_TTL_SECONDS = 3600  # 冷记忆过期时间 (1小时)
CLEANUP_INTERVAL = 300        # 清理间隔 (5分钟)
# 🟢22：workspace.db 此前硬拼到 ducky/data/，绕开了 AIDUMEM_DATA_DIR 环境变量注入约定。
# 统一走 utils.DATA_DIR，与其余库（facts/text_fts/salience）落在同一数据目录。
WORKSPACE_DB = os.path.join(DATA_DIR, "workspace.db")

# ── 全局状态 ──
# workspace[user_id] = OrderedDict({memory_id: {data, access_count, last_accessed, created}})
_workspace: dict[str, OrderedDict] = {}
_ws_lock = threading.Lock()
_last_cleanup = 0
_db_initialized = False


# ── SQLite 持久化层 ──

def _ensure_db():
    """确保 workspace.db 存在且 schema 就绪"""
    global _db_initialized
    if _db_initialized:
        return
    os.makedirs(os.path.dirname(WORKSPACE_DB), exist_ok=True)
    conn = sqlite3.connect(WORKSPACE_DB, check_same_thread=False, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA cache_size=-500")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workspace (
            user_id      TEXT NOT NULL,
            memory_id    TEXT NOT NULL,
            text         TEXT NOT NULL DEFAULT '',
            score        REAL NOT NULL DEFAULT 0.0,
            metadata     TEXT NOT NULL DEFAULT '{}',
            created_at   TEXT NOT NULL DEFAULT '',
            access_count INTEGER NOT NULL DEFAULT 1,
            last_accessed REAL NOT NULL DEFAULT 0.0,
            PRIMARY KEY (user_id, memory_id)
        )
    """)
    # v20 additive bank column.  Existing rows stay in default; no rebuild or
    # deletion is performed, and the legacy primary key remains usable while
    # the process rolls forward.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(workspace)").fetchall()}
    if "bank_id" not in cols:
        conn.execute("ALTER TABLE workspace ADD COLUMN bank_id TEXT NOT NULL DEFAULT 'default'")

    # 🔴v20：主键必须含 bank_id，否则域隔离在这张表上**结构性不可能**。
    #
    # 原主键是 (user_id, memory_id)，而 SQLite 无法用 ALTER 改主键 ——
    # 加一列 bank_id 并不会让它进主键。于是 alice 的 work 域与 home 域
    # 只要出现同一个 memory_id，第二次写入就会在 `ON CONFLICT(user_id,
    # memory_id) DO UPDATE` 上命中并**覆盖**掉第一条。
    #
    # 工作区虽是缓存，但它的内容会被注入上下文：症状就是家庭域的记忆
    # 出现在工作域的对话里 —— 恰恰是域隔离要防的那件事，且不报任何错。
    #
    # 因此在此做一次**逐行搬运**的重建（幂等：主键已含 bank_id 则跳过）。
    info = conn.execute("PRAGMA table_info(workspace)").fetchall()
    pk_cols = {row[1] for row in info if row[5] > 0}
    if "bank_id" not in pk_cols:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workspace_v20 (
                user_id      TEXT NOT NULL,
                bank_id      TEXT NOT NULL DEFAULT 'default',
                memory_id    TEXT NOT NULL,
                text         TEXT NOT NULL DEFAULT '',
                score        REAL NOT NULL DEFAULT 0.0,
                metadata     TEXT NOT NULL DEFAULT '{}',
                created_at   TEXT NOT NULL DEFAULT '',
                access_count INTEGER NOT NULL DEFAULT 1,
                last_accessed REAL NOT NULL DEFAULT 0.0,
                PRIMARY KEY (user_id, bank_id, memory_id)
            )
        """)
        # 先搬后删：任何一步失败都回滚，绝不出现「删了没搬到」的中间态。
        conn.execute("""
            INSERT OR IGNORE INTO workspace_v20
                (user_id, bank_id, memory_id, text, score, metadata,
                 created_at, access_count, last_accessed)
            SELECT user_id,
                   COALESCE(NULLIF(TRIM(bank_id), ''), 'default'),
                   memory_id, text, score, metadata,
                   created_at, access_count, last_accessed
            FROM workspace
        """)
        moved = conn.execute("SELECT COUNT(*) FROM workspace_v20").fetchone()[0]
        origin = conn.execute("SELECT COUNT(*) FROM workspace").fetchone()[0]
        if moved < origin:
            # 不满足「一行不少」就整体放弃重建，宁可维持旧主键继续跑，
            # 也不接受「重建成功」这四个字盖住一次悄悄的数据缩水。
            conn.rollback()
            conn.execute("DROP TABLE IF EXISTS workspace_v20")
            conn.commit()
            raise RuntimeError(
                f"workspace 主键重建搬运不全: {origin} -> {moved}，已回滚"
            )
        conn.execute("DROP TABLE workspace")
        conn.execute("ALTER TABLE workspace_v20 RENAME TO workspace")
        conn.commit()

    conn.execute("CREATE INDEX IF NOT EXISTS idx_ws_scope ON workspace(user_id, bank_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ws_user ON workspace(user_id)")
    conn.commit()
    conn.close()
    _db_initialized = True


def _get_db_conn():
    """获取 workspace.db 连接"""
    _ensure_db()
    conn = sqlite3.connect(WORKSPACE_DB, check_same_thread=False, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA cache_size=-500")
    conn.row_factory = sqlite3.Row
    return conn


def _scope_key(user_id: str, bank_id: str = DEFAULT_BANK_ID) -> str:
    scope = make_scope(user_id, bank_id)
    return f"{scope.user_id}\x1f{scope.bank_id}"


def _db_upsert(user_id: str, memory_id: str, data: dict, bank_id: str = DEFAULT_BANK_ID):
    """写入或更新一条工作区记忆到 SQLite"""
    try:
        conn = _get_db_conn()
        conn.execute("""
            INSERT INTO workspace (user_id, bank_id, memory_id, text, score, metadata, created_at, access_count, last_accessed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, bank_id, memory_id) DO UPDATE SET
                text = excluded.text,
                score = excluded.score,
                metadata = excluded.metadata,
                created_at = excluded.created_at,
                access_count = excluded.access_count,
                last_accessed = excluded.last_accessed
        """, (
            user_id, bank_id, memory_id,
            data.get("text", ""),
            data.get("score", 0.0),
            json.dumps(data.get("metadata", {}), ensure_ascii=False),
            data.get("created_at", ""),
            data.get("access_count", 1),
            data.get("last_accessed", 0.0),
        ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Workspace DB upsert 失败: {e}")


def _db_delete(user_id: str, memory_id: str, bank_id: str = DEFAULT_BANK_ID):
    """从 SQLite 删除一条"""
    try:
        conn = _get_db_conn()
        conn.execute("DELETE FROM workspace WHERE user_id = ? AND bank_id = ? AND memory_id = ?", (user_id, bank_id, memory_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Workspace DB delete 失败: {e}")


def _db_delete_user(user_id: str, bank_id: str = DEFAULT_BANK_ID):
    """清空某个用户的全部工作区"""
    try:
        conn = _get_db_conn()
        conn.execute("DELETE FROM workspace WHERE user_id = ? AND bank_id = ?", (user_id, bank_id))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Workspace DB delete_user 失败: {e}")


def _db_load_all() -> dict[str, OrderedDict]:
    """启动时从 SQLite 恢复全部工作区"""
    result: dict[str, OrderedDict] = {}
    try:
        conn = _get_db_conn()
        rows = conn.execute(
            "SELECT * FROM workspace ORDER BY last_accessed ASC"
        ).fetchall()
        conn.close()
        for row in rows:
            uid = _scope_key(row["user_id"], row["bank_id"] or DEFAULT_BANK_ID)
            mid = row["memory_id"]
            if uid not in result:
                result[uid] = OrderedDict()
            result[uid][mid] = {
                "text": row["text"],
                "score": row["score"],
                "metadata": json.loads(row["metadata"] or "{}"),
                "created_at": row["created_at"],
                "access_count": row["access_count"],
                "last_accessed": row["last_accessed"],
            }
        logger.info(f"Workspace 从 SQLite 恢复: {sum(len(v) for v in result.values())} 条记忆, {len(result)} 个用户")
    except Exception as e:
        logger.warning(f"Workspace DB 恢复失败（首次启动正常）: {e}")
    return result


def _init_workspace():
    """进程启动时恢复工作区"""
    global _workspace
    with _ws_lock:
        if not _workspace:  # 仅首次
            _workspace = _db_load_all()


# 模块加载时自动恢复
try:
    _init_workspace()
except Exception as e:
    logger.warning(f"Workspace 初始化跳过: {e}")


# ── 公共 API ──

def ws_lookup(user_id: str, query: str, bank_id: str = DEFAULT_BANK_ID) -> Optional[list]:
    """
    在工作区内搜索匹配记忆。命中则返回结果列表，否则 None。
    简单匹配：query token 与 memory text 的 Jaccard。
    """
    with _ws_lock:
        scope = make_scope(user_id, bank_id)
        scope_key = _scope_key(scope.user_id, scope.bank_id)
        ws = _workspace.get(scope_key)
        if not ws:
            return None

    now = time.time()
    matches = []
    # 不需要锁：遍历副本
    items = list(ws.items())

    for mem_id, data in items:
        text = data.get("text", "")
        if not text:
            continue
        sim = quick_sim(query, text)
        score = data.get("score", sim)
        combined = 0.7 * sim + 0.3 * score
        if combined > 0.5:
            matches.append({
                "id": mem_id,
                "memory": text,
                "score": round(combined, 4),
                "_workspace_hit": True,
                "metadata": data.get("metadata", {}),
                "created_at": data.get("created_at", ""),
            })

    if matches:
        # 更新访问计数 + 时间
        with _ws_lock:
            for m in matches:
                if m["id"] in ws:
                    ws[m["id"]]["access_count"] = ws[m["id"]].get("access_count", 0) + 1
                    ws[m["id"]]["last_accessed"] = now
                    # 异步同步到 SQLite
                    _db_upsert(scope.user_id, m["id"], ws[m["id"]], scope.bank_id)
        matches.sort(key=lambda x: x["score"], reverse=True)
        logger.info(f"⚡ Workspace hit: {len(matches)} 条 (user={scope.user_id}, bank={scope.bank_id})")
        return matches

    return None


def ws_push(user_id: str, memory_id: str, text: str,
            score: float = 0.0, metadata: dict = None, created_at: str = "",
            bank_id: str = DEFAULT_BANK_ID):
    """
    向工作区推入一条新记忆（如新增或搜索命中时）。
    LRU 淘汰：超过 CAPACITY 时移除最久未访问的。
    同步持久化到 SQLite。
    """
    now = time.time()
    evicted_key = None
    with _ws_lock:
        scope = make_scope(user_id, bank_id)
        scope_key = _scope_key(scope.user_id, scope.bank_id)
        if scope_key not in _workspace:
            _workspace[scope_key] = OrderedDict()

        ws = _workspace[scope_key]

        # 如果已存在，更新
        if memory_id in ws:
            ws[memory_id].update({
                "text": text,
                "score": score,
                "metadata": metadata or {},
                "created_at": created_at,
                "last_accessed": now,
            })
            # LRU: 移到末尾
            ws.move_to_end(memory_id)
        else:
            # 新记忆
            ws[memory_id] = {
                "text": text,
                "score": score,
                "metadata": metadata or {},
                "created_at": created_at,
                "access_count": 1,
                "last_accessed": now,
            }
            # LRU 淘汰
            if len(ws) > WORKSPACE_CAPACITY:
                evicted_key, _ = ws.popitem(last=False)
                logger.debug(f"Workspace 淘汰: {evicted_key[:16]} (LRU)")

        # 持久化当前记忆
        _db_upsert(scope.user_id, memory_id, ws[memory_id], scope.bank_id)

    # 淘汰的也要从 SQLite 删
    if evicted_key:
        _db_delete(scope.user_id, evicted_key, scope.bank_id)

    _maybe_cleanup(now)


def ws_feed_from_results(user_id: str, results: list, bank_id: str = DEFAULT_BANK_ID):
    """从搜索结果批量喂入工作区（自动推入 top-10）"""
    for item in results[:10]:
        if not isinstance(item, dict):
            continue
        mid = item.get("id", "")
        text = item.get("memory", "")
        if not mid or not text:
            continue
        score = item.get("score", 0) or (item.get("_ignition_score", 0) or 0)
        ws_push(
            user_id=user_id,
            memory_id=mid,
            text=text,
            score=score,
            metadata=item.get("metadata"),
            created_at=item.get("created_at", ""),
            bank_id=bank_id,
        )


def ws_status(user_id: str, bank_id: str = DEFAULT_BANK_ID) -> dict:
    """查看工作区状态"""
    now = time.time()
    with _ws_lock:
        scope = make_scope(user_id, bank_id)
        ws = _workspace.get(_scope_key(scope.user_id, scope.bank_id), OrderedDict())
        items = []
        for mid, data in ws.items():
            items.append({
                "id": mid[:16],
                "text_preview": data["text"][:60],
                "score": data["score"],
                "access_count": data["access_count"],
                "age_seconds": int(now - data.get("last_accessed", now)),
            })
        return {
            "total": len(items),
            "capacity": WORKSPACE_CAPACITY,
            "persistent": os.path.exists(WORKSPACE_DB),
            "items": items,
        }


def ws_clear(user_id: str, bank_id: str = DEFAULT_BANK_ID) -> int:
    """清空用户工作区（内存 + SQLite）。返回内存侧清掉的条数。

    v20.1 整改轮（R-01）：本函数被接进 `cascade_delete_all` 删除链 ——
    工作区存着记忆正文副本且会被 `/search` 优先命中，删除链不清它，
    已删内容就会以 `found/workspace_hit` 复活（外审 z P1-01）。
    """
    with _ws_lock:
        scope = make_scope(user_id, bank_id)
        evicted = _workspace.pop(_scope_key(scope.user_id, scope.bank_id), None)
        n = len(evicted) if evicted else 0
    _db_delete_user(scope.user_id, scope.bank_id)
    logger.info(f"Workspace 清空: user={scope.user_id}, bank={scope.bank_id}, 内存条数={n}")
    return n


def ws_evict(user_id: str, memory_id: str, bank_id: str = DEFAULT_BANK_ID) -> bool:
    """从工作区驱逐单条记忆（内存 + SQLite）。返回内存侧是否命中。

    v20.1 整改轮（R-01）：单条删除（cascade_delete_memory）的配套 ——
    只清全域不清单条，`/delete` 之后同一条还能从缓存里搜出来。
    """
    scope = make_scope(user_id, bank_id)
    with _ws_lock:
        ws = _workspace.get(_scope_key(scope.user_id, scope.bank_id))
        hit = bool(ws and ws.pop(memory_id, None) is not None)
    _db_delete(scope.user_id, memory_id, scope.bank_id)
    return hit


# ── 内部工具 ──

def _maybe_cleanup(now: float):
    """定时清理过期冷记忆（内存 + SQLite 同步）"""
    global _last_cleanup
    if now - _last_cleanup < CLEANUP_INTERVAL:
        return
    _last_cleanup = now

    evicted = 0
    evicted_pairs = []
    with _ws_lock:
        for uid in list(_workspace.keys()):
            ws = _workspace[uid]
            stale = [
                mid for mid, data in ws.items()
                if now - data.get("last_accessed", 0) > WORKSPACE_TTL_SECONDS
                and data.get("access_count", 1) <= 2
            ]
            for mid in stale:
                del ws[mid]
                evicted_pairs.append((uid, mid))
                evicted += 1
            if not ws:
                del _workspace[uid]

    # 同步删除 SQLite
    # 🔴v20：这里的 uid 是 _scope_key() 拼出的**复合键**（user\x1fbank），
    # 不是 user_id。v19 时 _workspace 直接按 user_id 索引，原样传给
    # _db_delete 是对的；v20 给键加了域维度，这个调用点却没跟上 ——
    # 于是 DELETE 变成 `user_id='alice\x1fwork' AND bank_id='default'`，
    # 匹配 0 行，还一声不吭（rowcount 无人过问）。后果是冷记忆只从内存
    # 淘汰、永远留在盘上，进程一重启 _db_load_all 又原样捞回来：清理形同
    # 虚设，workspace.db 只涨不消。分隔符 \x1f 已被 _SCOPE_RE 排除在合法
    # user/bank 之外，故此处按它拆分是安全的。
    for uid, mid in evicted_pairs:
        user_id, _, bank_id = uid.partition("\x1f")
        _db_delete(user_id, mid, bank_id or DEFAULT_BANK_ID)

    if evicted:
        logger.info(f"Workspace 清理: {evicted} 条冷记忆")
