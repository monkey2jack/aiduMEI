"""
ducky.event_ledger — 事件溯源账本·轻量版 (v19.4.0 · Mímir 借鉴 B5)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

为什么需要这一层
    aiduMEI 此前的变更追溯能力弱——改了什么、为什么改，事后难查。
    Mímir §5.3 的 memory_events（4,476 条）是它「可追溯、可解释、可撤销」
    的结构基础。本层补上这一课：**每次记忆变更留一条不可篡改的账**。

设计原则（轻量版，对齐单租户场景）
    · 单表 memory_events：谁、何时、对哪条记忆、做了什么、为什么
    · 与记忆写入**同一事务**（Mímir 事务边界思想的精髓：账本和事实
      必须同生共死，不能先改事实后补账）——record_event 接收调用方
      的连接、只 INSERT 不 commit，由调用方统一提交
    · 不做 payload 全量快照（那是 tombstones 的职责），只留 hash + reason，
      控制表体积
    · 不做 correlation_id / causation_id 链——单租户用不上
    · 失败干净降级：账本写入失败只记日志，绝不阻断主链路

target_id 规范（v19.4.0 · 生产审计 🟡-C）
    记录侧统一两种形态：
      · fact:{fact_key}   事实键路径（add / governance approve|reject）
      · fact:{fact_id}    事实数字 id 路径（opinion_set；mem0 记忆删除/
        tombstone/restore 沿用裸 memory_id，其值本身即标识）
    查询侧 get_history 做别名展开：传 `fact:X` 或裸 `X` 都能查到同一
    条记忆的两种形态事件——一个参数查全链，不用猜当初记的是哪种。

对外符号
    ensure_ledger_schema()      建表（幂等）
    content_hash(text)          内容指纹（before/after 用）
    record_event(conn, ...)     在调用方事务内记一条事件（不自行 commit）
    get_history(target_id)      查某条记忆的完整变更史（别名展开）
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from ducky.utils import get_facts_conn

logger = logging.getLogger("aiduMEM.ledger")

_LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS memory_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    actor       TEXT NOT NULL DEFAULT 'system',
    action      TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    reason      TEXT DEFAULT '',
    before_hash TEXT DEFAULT '',
    after_hash  TEXT DEFAULT '',
    user_id     TEXT DEFAULT '',
    bank_id     TEXT DEFAULT ''
)
"""

_LEDGER_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_ledger_target ON memory_events(target_id)",
    "CREATE INDEX IF NOT EXISTS idx_ledger_action ON memory_events(action)",
)

# v20 P0-2 迁移：老账本表补作用域列。存量事件 user_id/bank_id 为空串，
# 查询侧把空串视作 default 域（v19 全库本就是单一默认域），零丢失。
_LEDGER_SCOPE_MIGRATIONS = (
    "ALTER TABLE memory_events ADD COLUMN user_id TEXT DEFAULT ''",
    "ALTER TABLE memory_events ADD COLUMN bank_id TEXT DEFAULT ''",
)


def _ensure_scope_columns(conn) -> None:
    """老表补作用域列（幂等，列已存在时 ALTER 报错被吞掉）。"""
    for stmt in _LEDGER_SCOPE_MIGRATIONS:
        try:
            conn.execute(stmt)
        except Exception:
            pass

# 合法 action 枚举（防拼写漂移，不在表内做 CHECK 约束以保持轻量）
KNOWN_ACTIONS = frozenset({
    "add", "update", "delete", "tombstone", "restore",
    "approve", "reject", "opinion_set",
})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_ledger_schema() -> None:
    """幂等建表。对既有库是 no-op，任何异常只记日志不抛。"""
    try:
        conn = get_facts_conn()
        conn.execute(_LEDGER_DDL)
        _ensure_scope_columns(conn)
        for stmt in _LEDGER_INDEXES:
            try:
                conn.execute(stmt)
            except Exception as exc:
                logger.debug("ledger 索引跳过: %s", exc)
        conn.commit()
    except Exception as exc:
        logger.warning("memory_events 建表跳过（服务继续）: %s", exc)


def content_hash(text) -> str:
    """内容指纹：用于 before/after 对比，不用于检索。空内容返回空串。"""
    s = str(text or "").strip()
    if not s:
        return ""
    return hashlib.sha256(s.encode("utf-8", errors="ignore")).hexdigest()[:16]


def record_event(
    conn,
    actor: str,
    action: str,
    target_id: str,
    reason: str = "",
    before_hash: str = "",
    after_hash: str = "",
    user_id: str = "",
    bank_id: str = "",
) -> int | None:
    """在**调用方事务内**记一条事件（只 INSERT，不 commit）。

    调用方负责随后 conn.commit()，让账本与事实变更同生共死。
    返回 event_id；失败返回 None（绝不抛异常阻断主链路）。

    v20 P0-2：opt-in 作用域戳——调用方手里有 (user_id, bank_id) 就盖上，
    get_history 才能分域查账；不传落空串，查询侧视作 default 域。
    戳不做规范化校验（账本是留痕不是闸门，闸门在写事实的入口），
    但具名域操作必须盖戳，否则它的账会漏进默认域的账本视图。
    """
    if not target_id:
        return None
    params = (
        _now_iso(),
        actor or "system",
        action or "unknown",
        target_id,
        reason or "",
        before_hash or "",
        after_hash or "",
        str(user_id or ""),
        str(bank_id or ""),
    )
    sql = """INSERT INTO memory_events
             (timestamp, actor, action, target_id, reason, before_hash, after_hash,
              user_id, bank_id)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"""
    try:
        conn.execute(_LEDGER_DDL)  # 幂等兜底：调用方连接未必建过表
        try:
            cur = conn.execute(sql, params)
        except Exception:
            # 老表还没有作用域列：就地迁移后重试，账一条都不能丢
            _ensure_scope_columns(conn)
            cur = conn.execute(sql, params)
        return cur.lastrowid
    except Exception as exc:
        logger.debug("ledger 记录跳过: %s", exc)
        return None


def _target_aliases(target_id: str) -> list[str]:
    """target_id 别名展开（v19.4.0 · 生产审计 🟡-C）：一个参数查全链。

    `fact:X` 与裸 `X` 互为别名；数字 X 额外展开 `fact:{X}`
    （opinion 用 fact_id、add/governance 用 fact_key 的历史差异兜底）。
    """
    t = (target_id or "").strip()
    if not t:
        return []
    bare = t[5:] if t.startswith("fact:") else t
    aliases = {t, bare, f"fact:{bare}"}
    if bare.isdigit():
        aliases.add(f"fact:{bare}")
    return sorted(a for a in aliases if a)


def get_history(target_id: str, limit: int = 100,
                user_id: str = "", bank_id: str = "") -> list:
    """查某条记忆的完整变更史（按时间正序，别名展开查全链）。失败返回 []。

    v20 P0-2 opt-in 作用域（默认零改动）：
    · 不传作用域 = v19 管理员级全库视图，原样保留
    · 传了 bank_id：具名域只看盖了该戳的账；default 域把无戳存量
      （空串）一并算进来——v19 的账本来就都是默认域的账
    · 传了 user_id：改名默认身份与 'default' 折叠同域（口径同
      legacy_helpers._fact_feedback_impl），空串存量归 default
    · 非法作用域直接抛 BankScopeError——查账的人传错域是调用方 bug，
      不能悄悄降级成全库视图（那正是要堵的泄漏）
    """
    aliases = _target_aliases(target_id)
    if not aliases:
        return []

    scope_sql, scope_params = "", []
    if user_id or bank_id:
        from ducky.bank_contract import normalize_bank_id, normalize_user_id
        from ducky.utils import DEFAULT_USER_ID
        if bank_id:
            want_bid = normalize_bank_id(bank_id)
            if want_bid == "default":
                scope_sql += " AND bank_id IN ('', 'default')"
            else:
                scope_sql += " AND bank_id = ?"
                scope_params.append(want_bid)
        if user_id:
            want_uid = normalize_user_id(user_id)
            canon = "default" if want_uid == DEFAULT_USER_ID else want_uid
            if canon == "default":
                scope_sql += " AND user_id IN ('', 'default', ?)"
                scope_params.append(DEFAULT_USER_ID)
            else:
                scope_sql += " AND user_id = ?"
                scope_params.append(want_uid)

    try:
        ensure_ledger_schema()
        conn = get_facts_conn()
        placeholders = ",".join("?" for _ in aliases)
        rows = conn.execute(
            f"""SELECT event_id, timestamp, actor, action, target_id, reason,
                       before_hash, after_hash, user_id, bank_id
                FROM memory_events WHERE target_id IN ({placeholders}){scope_sql}
                ORDER BY event_id ASC LIMIT ?""",
            (*aliases, *scope_params, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.debug("get_history 降级返回空: %s", exc)
        return []
