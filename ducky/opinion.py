"""
ducky.opinion — 信念层 Opinion·最小可用版 (v19.4.0 · Mímir 借鉴 B6)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

为什么只做骨架
    Mímir §9 把「AI 对某事的把握度」建模为独立可演化对象——事实是
    「是什么」，信念是「我多确定」，两者分离、信念随证据演化。
    但 Mímir 自己的信念层现在很薄：8 条 opinion 全是 support（点赞按钮），
    oppose/neutral 为零，observation 聚合是单 agent 给自己写好评的回声室。
    赶工只会做成摆设。因此 v19.4.0 只落最小可用版：

      · opinions 表三态（support/oppose/neutral）**都有真实写入路径**
      · observation 聚合直接吸取教训：**必须 ≥2 个不同证据来源才聚合**，
        单来源刷好评不聚合
      · 写入走 B5 账本留痕（action=opinion_set）
      · 完整的信念演化（随反馈自动调 confidence）留到数据积累后的 v19.5+

单租户简化
    不照搬 Mímir 的 UNIQUE(fact_id, owner_principal) 联邦约束，
    简化为 UNIQUE(fact_id, source)：同一证据来源对同一事实只留一条
    最新信念（upsert 覆盖），避免同源重复刷票。

对外符号
    ensure_opinion_schema()      建表（幂等）
    set_opinion(...)             写入/更新一条信念（三态皆可，账本留痕）
    list_opinions(...)           查某事实的信念清单
    aggregate_opinion(...)       聚合判定（≥2 不同来源才聚合）
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone

from ducky.bank_contract import is_legacy_schema_error
from ducky.utils import DEFAULT_USER_ID, get_facts_conn

logger = logging.getLogger("aiduMEM.opinion")

STANCES = ("support", "oppose", "neutral")
MIN_AGGREGATE_SOURCES = 2  # Mímir 教训：单来源回声室不聚合

_OPINIONS_DDL = """
CREATE TABLE IF NOT EXISTS opinions (
    opinion_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_id     INTEGER NOT NULL,
    stance      TEXT NOT NULL CHECK(stance IN ('support', 'oppose', 'neutral')),
    confidence  REAL DEFAULT 0.5,
    evidence_ids TEXT DEFAULT '',
    source      TEXT NOT NULL,
    owner       TEXT NOT NULL,
    created_at  TEXT,
    updated_at  TEXT,
    UNIQUE(fact_id, source)
)
"""

_OPINION_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_opinion_fact ON opinions(fact_id)",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_opinion_schema() -> None:
    """幂等建表。对既有库是 no-op，异常只记日志不抛。"""
    try:
        conn = get_facts_conn()
        conn.execute(_OPINIONS_DDL)
        # v20 P0-2 迁移：信念行盖作用域戳（从所属 facts 行继承），
        # 分库级联删除/审计/统计才有抓手。存量行归 default 域，零丢失。
        cols = [r[1] for r in conn.execute("PRAGMA table_info(opinions)").fetchall()]
        if "user_id" not in cols:
            conn.execute("ALTER TABLE opinions ADD COLUMN user_id TEXT NOT NULL DEFAULT 'default'")
            logger.info("✅ opinions 表已添加 user_id 列")
        if "bank_id" not in cols:
            conn.execute("ALTER TABLE opinions ADD COLUMN bank_id TEXT NOT NULL DEFAULT 'default'")
            logger.info("✅ opinions 表已添加 bank_id 列")
        for stmt in _OPINION_INDEXES:
            try:
                conn.execute(stmt)
            except Exception as exc:
                logger.debug("opinion 索引跳过: %s", exc)
        conn.commit()
    except Exception as exc:
        logger.warning("opinions 建表跳过（服务继续）: %s", exc)


def _fact_scope(conn, fact_id: int) -> tuple[str, str]:
    """从所属 facts 行取 (user_id, bank_id)。

    信念的归属跟着事实走（fact_id 全局唯一，一条事实只属于一个库），
    调用方无权也无需另报作用域。老库 facts 没有作用域列、或事实行
    不存在时落 default/default——与存量回填口径一致。

    甲8 第 7 处，八处里**写侧**后果最重的一处（读侧最重的是
    ``hot/legacy_helpers.py`` 的 ``_extract_key_facts``，那处是跨库泄密；
    这处是把账本改错。一个泄密一个改账，方向不同，不必强行排出高下）。
    原来这里是 ``except Exception → logger.debug →
    return ("default","default")``，两层都出了问题：

    1. **降级出口写的是持久化的错戳**。上面 ``set_opinion`` 的 upsert 带着
       ``DO UPDATE SET user_id=excluded.user_id, bank_id=excluded.bank_id``，
       所以一次瞬时锁库不只是让新行盖错戳——它会把一条**本来盖对了戳**的
       信念行永久改写成 ``default|default``，账本事件 ``opinion_set`` 一并
       跟着盖错。别处的降级是读漏、是跨库合并；这处是**把账本改错**，事后
       审计会拿着这条戳理直气壮地说「它属于 default 库」。
    2. **日志级别是 debug**。生产跑在 INFO 上，这一句一个字都不会出现。
       别处至少还留一句 warning 供人回溯，这处是真正的一声不响。

    修法与兄弟位点同一模板：只有「老库缺列/缺表」才配走降级，其余原样抛出，
    交给 ``set_opinion`` 外层收成 ``{"ok": False, "detail": ...}``——那个诚实
    出口本来就在，只是这条路从来没走到过。

    注意「事实行不存在」根本不走 except：``fetchone()`` 返回 None，直接落到
    末尾那个 default——那条是**有意的**语义，不在本次收窄范围内。
    """
    try:
        row = conn.execute(
            "SELECT user_id, bank_id FROM facts WHERE id=?", (fact_id,)
        ).fetchone()
        if row:
            return (str(row[0] or "default"), str(row[1] or "default"))
    except sqlite3.Error as exc:
        if not is_legacy_schema_error(exc):
            raise
        logger.warning("facts 表无作用域列，信念作用域回退 default: %s", exc)
    return ("default", "default")


def set_opinion(fact_id: int, stance: str, confidence: float = 0.5,
                evidence_ids: list | None = None, source: str = "",
                owner: str = DEFAULT_USER_ID) -> dict:
    """写入/更新一条信念（三态皆可）。同源同事实 upsert 覆盖。

    返回 {ok, opinion_id, stance, detail}；stance 非法直接拒绝。
    写入与账本 opinion_set 事件同事务，同生共死。
    """
    result = {"ok": False, "opinion_id": None, "stance": stance, "detail": ""}
    stance = (stance or "").strip().lower()
    if stance not in STANCES:
        result["detail"] = f"stance 必须是 {'/'.join(STANCES)}"
        return result
    if not fact_id:
        result["detail"] = "fact_id 不能为空"
        return result
    if not source or not str(source).strip():
        result["detail"] = "source（证据来源标识）不能为空"
        return result
    confidence = max(0.0, min(1.0, float(confidence)))
    ev_json = json.dumps(evidence_ids or [], ensure_ascii=False)
    try:
        ensure_opinion_schema()
        conn = get_facts_conn()
        now = _now_iso()
        # v20 P0-2：同事务内从 facts 行继承作用域戳（事实换不了库，
        # upsert 时也一并刷新，纠正历史上错盖的戳）
        scope_user, scope_bank = _fact_scope(conn, fact_id)
        cur = conn.execute(
            """INSERT INTO opinions (fact_id, stance, confidence, evidence_ids,
                                     source, owner, created_at, updated_at,
                                     user_id, bank_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(fact_id, source) DO UPDATE SET
                   stance=excluded.stance, confidence=excluded.confidence,
                   evidence_ids=excluded.evidence_ids, owner=excluded.owner,
                   updated_at=excluded.updated_at,
                   user_id=excluded.user_id, bank_id=excluded.bank_id""",
            (fact_id, stance, confidence, ev_json, source, owner, now, now,
             scope_user, scope_bank),
        )
        oid = cur.lastrowid
        # 📒 事件账本（B5）：信念写入留痕，同事务
        try:
            from ducky.event_ledger import record_event
            record_event(conn, actor=owner or "system", action="opinion_set",
                         target_id=f"fact:{fact_id}",
                         reason=f"stance={stance} source={source} confidence={confidence:.2f}",
                         user_id=scope_user, bank_id=scope_bank)
        except Exception as le:
            logger.debug("ledger 记录跳过: %s", le)
        conn.commit()
        result.update(ok=True, opinion_id=oid, detail="ok")
        return result
    except Exception as exc:
        logger.warning("set_opinion 失败: %s", exc)
        result["detail"] = str(exc)[:120]
        return result


def list_opinions(fact_id: int) -> list:
    """查某事实的信念清单。失败返回 []。"""
    if not fact_id:
        return []
    try:
        ensure_opinion_schema()
        conn = get_facts_conn()
        rows = conn.execute(
            "SELECT * FROM opinions WHERE fact_id=? ORDER BY opinion_id", (fact_id,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.debug("list_opinions 降级返回空: %s", exc)
        return []


def aggregate_opinion(fact_id: int) -> dict:
    """聚合判定（Mímir 教训版）：必须 ≥2 个不同证据来源才聚合。

    返回：
        {aggregated: False, reason: "insufficient_sources", distinct_sources: N}
            来源不足——单来源刷好评不聚合
        {aggregated: True, stance, confidence, distinct_sources, votes}
            聚合结果：stance 取多数票（平票 → neutral 保守态），
            confidence 取该 stance 下的均值
    """
    result = {"aggregated": False, "fact_id": fact_id, "distinct_sources": 0}
    if not fact_id:
        result["reason"] = "fact_id 为空"
        return result
    try:
        ensure_opinion_schema()
        conn = get_facts_conn()
        rows = conn.execute(
            "SELECT stance, confidence, source FROM opinions WHERE fact_id=?", (fact_id,)
        ).fetchall()
        conn.close()
        sources = {r["source"] for r in rows}
        result["distinct_sources"] = len(sources)
        if len(sources) < MIN_AGGREGATE_SOURCES:
            result["reason"] = "insufficient_sources"
            return result

        votes: dict[str, list[float]] = {}
        for r in rows:
            votes.setdefault(r["stance"], []).append(float(r["confidence"]))
        # 多数票；平票保守落 neutral
        top = sorted(votes.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        if len(top) > 1 and len(top[0][1]) == len(top[1][1]):
            stance, confs = "neutral", votes.get("neutral", [0.5])
        else:
            stance, confs = top[0]
        result.update(
            aggregated=True,
            stance=stance,
            confidence=round(sum(confs) / len(confs), 4),
            votes={k: len(v) for k, v in votes.items()},
        )
        return result
    except Exception as exc:
        logger.debug("aggregate_opinion 降级: %s", exc)
        result["reason"] = str(exc)[:120]
        return result
