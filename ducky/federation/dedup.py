"""
ducky.federation.dedup — 写入时自动去重
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

「不写垃圾，比事后清理便宜一百倍。」

三态判定（复用 ducky.utils.jaccard_sim，零新依赖）
    sim ≥ 0.85  → MERGE   合并：保留信息量更大的一条，并集标签
    0.70 ≤ sim  → UPDATE  更新：视为同一事实的新版本，覆盖旧值
    sim < 0.70  → INSERT  新增

只在「同 agent + 同 (user, bank) + 同 category」范围内比对：
不同 Agent 的相似认知各自保留，这是联邦语义的一部分；
不同 bank 的相似内容也各自保留（v20 P0-2），去重绝不跨库合并。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ducky.bank_contract import (
    DEFAULT_BANK_ID,
    normalize_bank_id,
    normalize_user_id,
)
from ducky.federation import sqlbits
from ducky.federation.schema import DEFAULT_AGENT
from ducky.utils import DEFAULT_USER_ID, get_facts_conn, jaccard_sim

logger = logging.getLogger("aiduMEM.Federation.Dedup")

MERGE_THRESHOLD = 0.85
UPDATE_THRESHOLD = 0.70

# 单次去重最多扫描的同类事实数，防止大类目拖慢写入
SCAN_LIMIT = 200

ACTION_MERGE = "merge"
ACTION_UPDATE = "update"
ACTION_INSERT = "insert"


@dataclass(frozen=True)
class DedupVerdict:
    """去重判定结果。fact_id 为命中的既有事实 id（INSERT 时为 None）。"""

    action: str
    similarity: float
    fact_id: int | None = None
    fact_key: str | None = None

    @property
    def is_new(self) -> bool:
        return self.action == ACTION_INSERT

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "similarity": round(self.similarity, 4),
            "matched_fact_id": self.fact_id,
            "matched_fact_key": self.fact_key,
        }


def _merge_tags(old: str, new: str) -> str:
    """标签并集，保持稳定顺序，逗号分隔。"""
    seen: list[str] = []
    for raw in (old or "").split(",") + (new or "").split(","):
        tag = raw.strip()
        if tag and tag not in seen:
            seen.append(tag)
    return ",".join(seen)


def check_duplicate(
    fact_value: str,
    *,
    category: str = "general",
    agent_id: str = DEFAULT_AGENT,
    user_id: str = DEFAULT_USER_ID,
    bank_id: str = DEFAULT_BANK_ID,
    conn=None,
) -> DedupVerdict:
    """在同 agent + 同 (user, bank) + 同 category 内查找最相似的既有事实并给出三态判定。

    v20 P0-2：扫描按 bank 作用域收口。此前不带 bank 条件时，B 库写入会命中
    A 库的相似行 → verdict 携带 A 库的 fact_id → 上游 UPDATE/MERGE 直接改写
    A 库数据。降级路径（扫描异常→INSERT）保持不变：最坏是同库重复一行，
    绝不跨库污染。
    """
    needle = (fact_value or "").strip()
    if not needle:
        return DedupVerdict(ACTION_INSERT, 0.0)

    uid = normalize_user_id(user_id)
    bid = normalize_bank_id(bank_id)

    own_conn = conn is None
    conn = conn or get_facts_conn()
    try:
        agent_frag, agent_params = sqlbits.agent_eq(agent_id)
        rows = conn.execute(
            f"""SELECT id, fact_key, fact_value FROM facts
                WHERE archived=0 AND category=? AND {agent_frag}
                  AND user_id=? AND bank_id=?
                ORDER BY updated_at DESC LIMIT ?""",
            (category, *agent_params, uid, bid, SCAN_LIMIT),
        ).fetchall()
    except Exception as exc:
        logger.debug("去重扫描失败，降级为直接新增: %s", exc)
        return DedupVerdict(ACTION_INSERT, 0.0)
    finally:
        if own_conn:
            conn.close()

    best_sim = 0.0
    best_row = None
    for row in rows:
        sim = jaccard_sim(needle, row["fact_value"] or "")
        if sim > best_sim:
            best_sim, best_row = sim, row

    if best_row is None:
        return DedupVerdict(ACTION_INSERT, 0.0)
    if best_sim >= MERGE_THRESHOLD:
        return DedupVerdict(ACTION_MERGE, best_sim, best_row["id"], best_row["fact_key"])
    if best_sim >= UPDATE_THRESHOLD:
        return DedupVerdict(ACTION_UPDATE, best_sim, best_row["id"], best_row["fact_key"])
    return DedupVerdict(ACTION_INSERT, best_sim, best_row["id"], best_row["fact_key"])


def apply_merge(fact_id: int, new_value: str, new_tags: str = "", *, conn=None) -> dict[str, Any]:
    """
    合并到既有事实：保留信息量更大的正文（更长者胜），标签取并集。
    不新增行——这是去重的全部意义。
    """
    own_conn = conn is None
    conn = conn or get_facts_conn()
    try:
        row = conn.execute(
            "SELECT fact_value, COALESCE(tags,'') AS tags FROM facts WHERE id=?", (fact_id,)
        ).fetchone()
        if row is None:
            return {"status": "error", "detail": f"fact {fact_id} 不存在"}

        kept = row["fact_value"] if len(row["fact_value"] or "") >= len(new_value or "") else new_value
        merged_tags = _merge_tags(row["tags"], new_tags)
        conn.execute(
            """UPDATE facts
               SET fact_value=?, overview=?, summary=?, tags=?,
                   updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (kept, kept, kept[:60], merged_tags, fact_id),
        )
        conn.commit()
    finally:
        if own_conn:
            conn.close()

    return {"status": "ok", "action": ACTION_MERGE, "fact_id": fact_id, "tags": merged_tags}
