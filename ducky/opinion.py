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
from datetime import datetime, timezone

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
        for stmt in _OPINION_INDEXES:
            try:
                conn.execute(stmt)
            except Exception as exc:
                logger.debug("opinion 索引跳过: %s", exc)
        conn.commit()
    except Exception as exc:
        logger.warning("opinions 建表跳过（服务继续）: %s", exc)


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
        cur = conn.execute(
            """INSERT INTO opinions (fact_id, stance, confidence, evidence_ids,
                                     source, owner, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(fact_id, source) DO UPDATE SET
                   stance=excluded.stance, confidence=excluded.confidence,
                   evidence_ids=excluded.evidence_ids, owner=excluded.owner,
                   updated_at=excluded.updated_at""",
            (fact_id, stance, confidence, ev_json, source, owner, now, now),
        )
        oid = cur.lastrowid
        # 📒 事件账本（B5）：信念写入留痕，同事务
        try:
            from ducky.event_ledger import record_event
            record_event(conn, actor=owner or "system", action="opinion_set",
                         target_id=f"fact:{fact_id}",
                         reason=f"stance={stance} source={source} confidence={confidence:.2f}")
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
