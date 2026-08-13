"""
ducky.refine_memory — 记忆递归精炼（v19.0 · P1-3 · SimpleMem 借鉴）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
把相关的多条记忆递归合并为更高层抽象，保持信息密度，对抗「记忆只增
不减」的熵增。与 P0-2 self_edit 的分工：

    - self_edit：写入时，新记忆 vs 已有记忆的 1 对 1 判重/冲突
    - refine_memory：后台，多对 1 的聚类压缩，把 N 条碎记忆提炼成
      一条高层抽象 + 一份可回滚快照

治理铁律：同样不允许 LLM 直接 commit。精炼产物写入 refined_memories
表，原记忆只做 soft-superseded（不物理删除），可一键回滚。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from ducky.utils import get_facts_conn

logger = logging.getLogger("aiduMEM.refine_memory")

REFINE_ENABLED = os.environ.get("AIDUMEM_REFINE_ENABLED", "false").strip().lower() not in {
    "0", "false", "no", "off",
}

# 至少 3 条候选才值得递归精炼
_MIN_GROUP = 3

_REFINE_SYSTEM = (
    "你是 aiduMEI 的记忆精炼引擎。把多条相关记忆合并为一条更高层的"
    "抽象摘要。只输出一个 JSON 对象，不要输出任何解释。"
)

_REFINE_USER_TEMPLATE = """请把以下相关记忆合并为一条高层抽象：

{items}

输出 JSON：
{{"summary": "合并后的高层摘要", "reason": "一句话说明合并依据", "confidence": 0.0-1.0}}

要求：
1. summary 保留所有关键信息，去掉重复冗余
2. 不编造记忆里没有的信息
3. 只输出 JSON 对象"""

_checked = False


def ensure_refine_schema() -> None:
    """幂等建 refined_memories 精炼账本表。"""
    global _checked
    if _checked:
        return
    conn = get_facts_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS refined_memories (
                refine_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       TEXT NOT NULL DEFAULT 'default',
                source_ids    TEXT NOT NULL,       -- JSON 数组：被合并的 fact id
                summary       TEXT NOT NULL,
                reason        TEXT DEFAULT '',
                confidence    REAL DEFAULT 0.5,
                state         TEXT DEFAULT 'proposed', -- proposed | applied | rolled_back
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_refined_user ON refined_memories(user_id)"
        )
        conn.commit()
        _checked = True
    except Exception as e:
        logger.warning(f"refined_memories 表初始化失败: {e}")
    finally:
        conn.close()


def _load_candidates(conn, user_id: str, category: str, limit: int) -> list[dict]:
    # facts 表没有 user_id 列，用户标识存在 source 列（默认 'default'）。
    # 语义：default 用户（管理视角）不过滤，看全部；其他用户只看 source 匹配的。
    # 原实现的 (? IS NULL OR source=? OR ?='default') 第一个分支绑定的是
    # category 外的 user_id，恒 false，是死分支，且 default 用户会漏过滤。
    rows = conn.execute(
        """
        SELECT id, category, fact_key, fact_value FROM facts
        WHERE archived=0 AND category=? AND (?='default' OR source=?)
        ORDER BY updated_at DESC LIMIT ?
        """,
        (category, user_id, user_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def _parse_refine_json(raw: str) -> Optional[dict]:
    if not raw:
        return None
    text = raw.strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return None


def _rule_summary(items: list[dict]) -> str:
    """无 LLM 时的规则降级：拼接 fact_key + 去重。"""
    keys = []
    for it in items:
        k = str(it.get("fact_key") or "").strip()
        if k and k not in keys:
            keys.append(k)
    prefix = str(items[0].get("category") or "memory")
    return f"{prefix} 相关的 {len(items)} 条记忆，涉及：{'、'.join(keys[:8])}"


def refine_group(user_id: str, category: str, *, limit: int = 20, use_llm: bool = True) -> dict:
    """对指定 category 下的记忆做一次递归精炼（proposed，不自动应用）。"""
    ensure_refine_schema()
    conn = get_facts_conn()
    try:
        candidates = _load_candidates(conn, user_id, category, limit)
    finally:
        conn.close()

    if len(candidates) < _MIN_GROUP:
        return {
            "status": "skipped",
            "reason": f"候选不足（{len(candidates)} < {_MIN_GROUP}）",
            "category": category,
        }

    summary = None
    llm_used = False
    if use_llm and REFINE_ENABLED:
        try:
            from ducky.llm_client import call_llm
            block = "\n".join(
                f"- [{it['id']}] {it['fact_key']}: {str(it['fact_value'])[:120]}" for it in candidates[:10]
            )
            raw = call_llm(
                _REFINE_USER_TEMPLATE.format(items=block),
                system=_REFINE_SYSTEM,
                max_tokens=500,
                temperature=0.3,
            )
            parsed = _parse_refine_json(raw or "")
            if parsed and str(parsed.get("summary") or "").strip():
                summary = str(parsed["summary"]).strip()
                llm_used = True
        except Exception as e:
            logger.debug(f"LLM 精炼失败（降级规则）: {e}")

    if summary is None:
        summary = _rule_summary(candidates)

    source_ids = [it["id"] for it in candidates]
    confidence = 0.5
    conn = get_facts_conn()
    try:
        # 去重：同一批 source_ids 已 proposed 过则跳过
        sig = hashlib.md5(json.dumps(source_ids, sort_keys=True).encode()).hexdigest()
        dup = conn.execute(
            "SELECT refine_id FROM refined_memories WHERE source_ids=? AND state='proposed'",
            (json.dumps(source_ids),),
        ).fetchone()
        if dup:
            return {
                "status": "skipped",
                "reason": f"同一批候选已精炼过（refine_id={dup['refine_id']}）",
                "category": category,
            }
        conn.execute(
            "INSERT INTO refined_memories (user_id, source_ids, summary, reason, confidence, state) "
            "VALUES (?,?,?,?,?, 'proposed')",
            (user_id, json.dumps(source_ids), summary, f"recursive-refine:{sig[:8]}", confidence),
        )
        conn.commit()
        refine_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    except Exception as e:
        logger.warning(f"精炼结果落库失败: {e}")
        return {"status": "error", "detail": str(e)}
    finally:
        conn.close()

    logger.info("🧼 refine_memory: %s 类 %d 条 → refine_id=%d（llm=%s）", category, len(source_ids), refine_id, llm_used)
    return {
        "status": "ok",
        "refine_id": refine_id,
        "summary": summary,
        "source_ids": source_ids,
        "llm_used": llm_used,
        "state": "proposed",
    }


def list_refinements(user_id: str = "default", state: str = "proposed", limit: int = 20) -> list[dict]:
    ensure_refine_schema()
    conn = get_facts_conn()
    try:
        sql = "SELECT * FROM refined_memories WHERE user_id=?"
        params: list[Any] = [user_id]
        if state != "all":
            sql += " AND state=?"
            params.append(state)
        sql += " ORDER BY refine_id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 200)))
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        for r in rows:
            try:
                r["source_ids"] = json.loads(r.get("source_ids") or "[]")
            except json.JSONDecodeError:
                r["source_ids"] = []
        return rows
    finally:
        conn.close()


def apply_refinement(refine_id: int) -> dict:
    """应用一次精炼：把 source_ids 对应的 facts 标记为 archived（soft-superseded）。"""
    ensure_refine_schema()
    conn = get_facts_conn()
    try:
        row = conn.execute(
            "SELECT * FROM refined_memories WHERE refine_id=? AND state='proposed'", (refine_id,)
        ).fetchone()
        if not row:
            return {"status": "error", "detail": f"refine_id={refine_id} 不存在或已应用"}
        ids = json.loads(row["source_ids"] or "[]")
        for fid in ids:
            conn.execute("UPDATE facts SET archived=1, archived_at=CURRENT_TIMESTAMP WHERE id=?", (fid,))
        conn.execute("UPDATE refined_memories SET state='applied' WHERE refine_id=?", (refine_id,))
        conn.commit()
        return {"status": "ok", "refine_id": refine_id, "archived": len(ids)}
    except Exception as e:
        logger.warning(f"应用精炼失败: {e}")
        return {"status": "error", "detail": str(e)}
    finally:
        conn.close()


def rollback_refinement(refine_id: int) -> dict:
    """回滚一次精炼：把 archived facts 恢复为有效。"""
    ensure_refine_schema()
    conn = get_facts_conn()
    try:
        row = conn.execute(
            "SELECT * FROM refined_memories WHERE refine_id=? AND state='applied'", (refine_id,)
        ).fetchone()
        if not row:
            return {"status": "error", "detail": f"refine_id={refine_id} 不存在或未应用"}
        ids = json.loads(row["source_ids"] or "[]")
        for fid in ids:
            conn.execute("UPDATE facts SET archived=0, archived_at=NULL WHERE id=?", (fid,))
        conn.execute("UPDATE refined_memories SET state='rolled_back' WHERE refine_id=?", (refine_id,))
        conn.commit()
        return {"status": "ok", "refine_id": refine_id, "restored": len(ids)}
    except Exception as e:
        logger.warning(f"回滚精炼失败: {e}")
        return {"status": "error", "detail": str(e)}
    finally:
        conn.close()
