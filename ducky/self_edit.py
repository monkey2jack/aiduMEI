"""
ducky.self_edit — 记忆去重自编辑（v19.0 · P0-2）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
借鉴 Mem0 的「自我编辑」能力：写入新记忆前，先用 LLM 判断它与既有
记忆是「重复 / 冲突 / 全新」，重复则合并而非追加，冲突则保留双方并
标注置信度与时间——记忆不再只增不减。

与 Layer1 Jaccard 去重的关系：
    self-edit 是 LLM 语义级判重（更准、能产出合并文本），Jaccard 是
    零成本兜底。self-edit 先行；LLM 不可用或判定「全新」时回退到
    Layer1 原有流程，向后完全兼容。

可回滚：
    每次合并/冲突更新都会把「旧内容 → 新内容」快照进 memory_edits 表，
    rollback_edit() 可把记忆恢复到编辑前状态（验收：用户可回滚合并）。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from ducky.llm_client import call_llm
from ducky.utils import DEFAULT_USER_ID, get_facts_conn

logger = logging.getLogger("aiduMEM.self_edit")

SELF_EDIT_ENABLED = os.environ.get("AIDUMEM_SELF_EDIT_ENABLED", "true").strip().lower() not in {
    "0", "false", "no", "off",
}

# 语义判重阈值：仅对最相似候选做 LLM 判定，控制 token 成本
_CANDIDATE_SIM_FLOOR = 0.25

SELF_EDIT_SYSTEM = (
    "你是 aiduMEI 的记忆去重自编辑引擎。判断「新记忆」与「既有候选」之间的关系，"
    "只输出一个 JSON 对象，不要输出任何解释。"
)

SELF_EDIT_USER_TEMPLATE = """新记忆：
{new_text}

既有候选：
{candidates}

判断新记忆与候选的关系：
- "duplicate"：新记忆与某条候选是同一事实/偏好的重复，需要合并
- "conflict"：新记忆与某条候选矛盾（如偏好反转、状态变更），需要保留双方
- "distinct"：新记忆是全新内容，无需合并

输出 JSON：
{{"decision": "duplicate|conflict|distinct", "memory_id": "命中的候选id(duplicate/conflict必填)", "merged_content": "合并后的完整文本", "confidence": 0.0-1.0, "reason": "一句话说明"}}

要求：
1. merged_content 必须同时保留所有关键信息；conflict 时用「旧：... | 新：...」并标注时间
2. 只有高度确定才判 duplicate/conflict，不确定一律 distinct
3. 只输出 JSON 对象"""

_checked = False


def ensure_self_edit_schema() -> None:
    """幂等建 memory_edits 编辑账本表。"""
    global _checked
    if _checked:
        return
    conn = get_facts_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_edits (
                edit_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id   TEXT NOT NULL,
                user_id     TEXT NOT NULL DEFAULT 'default',
                action      TEXT NOT NULL,       -- duplicate | conflict
                old_content TEXT NOT NULL,
                new_content TEXT NOT NULL,
                reason      TEXT DEFAULT '',
                confidence  REAL DEFAULT 0.5,
                undone      INTEGER DEFAULT 0,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_edits_memory ON memory_edits(memory_id)"
        )
        conn.commit()
        _checked = True
    except Exception as e:
        logger.warning(f"memory_edits 表初始化失败（服务继续）: {e}")
    finally:
        conn.close()


def _extract_text(messages_json: Any) -> str:
    if isinstance(messages_json, list):
        return " ".join(m.get("content", "") for m in messages_json if isinstance(m, dict))
    if isinstance(messages_json, dict):
        return messages_json.get("content", str(messages_json))
    return str(messages_json)


def _search_candidates(memory, user_id: str, new_text: str, limit: int = 3) -> list[dict]:
    try:
        # mem0 2.0.x search 的关键字是 top_k（不是 limit），传 limit 会被
        # **kwargs 静默吞掉导致候选数恒为默认值。先试 top_k，旧版回退 limit。
        try:
            raw = memory.search(new_text, filters={"user_id": user_id}, top_k=limit)
        except TypeError:
            raw = memory.search(new_text, filters={"user_id": user_id}, limit=limit)
        results = raw.get("results", raw) if isinstance(raw, dict) else raw
        if not isinstance(results, list):
            return []
        return [
            {
                "memory_id": r.get("id", ""),
                "content": (r.get("memory") or "").strip(),
                "score": r.get("score", 0) or 0,
                "created_at": r.get("created_at", ""),
            }
            for r in results
            if isinstance(r, dict) and (r.get("memory") or "").strip()
        ]
    except Exception as e:
        logger.debug(f"候选检索失败（降级）: {e}")
        return []


def _parse_decision(raw: str) -> Optional[dict]:
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


def _detect_relation(memory, user_id: str, new_text: str) -> Optional[dict]:
    """返回 {decision, memory_id, merged_content, confidence, reason} 或 None（判定为全新）。"""
    candidates = _search_candidates(memory, user_id, new_text)
    if not candidates:
        return None

    cand_block = "\n".join(
        f"[id={c['memory_id']}] {c['content'][:200]}" for c in candidates
    )
    raw = call_llm(
        SELF_EDIT_USER_TEMPLATE.format(new_text=new_text[:400], candidates=cand_block),
        system=SELF_EDIT_SYSTEM,
        max_tokens=512,
        temperature=0.2,
    )
    decision = _parse_decision(raw)
    if not decision:
        return None

    verdict = str(decision.get("decision") or "").strip().lower()
    if verdict not in ("duplicate", "conflict"):
        return None

    memory_id = str(decision.get("memory_id") or "").strip()
    merged = str(decision.get("merged_content") or "").strip()
    if not memory_id or not merged:
        return None
    if memory_id not in {c["memory_id"] for c in candidates}:
        # LLM 幻觉出一个不存在的 id → 降级为 distinct，保证安全
        logger.warning("self-edit: LLM 返回了候选之外的 memory_id=%s，判定为 distinct", memory_id)
        return None

    confidence = 0.5
    try:
        confidence = max(0.0, min(1.0, float(decision.get("confidence", 0.5))))
    except (TypeError, ValueError):
        pass

    return {
        "decision": verdict,
        "memory_id": memory_id,
        "merged_content": merged,
        "confidence": confidence,
        "reason": str(decision.get("reason") or ""),
    }


def _snapshot_old(memory, memory_id: str) -> str:
    """读取编辑前的旧内容。优先 get(memory_id) 精查，失败回退 get_all 兜底。

    返回空串表示取不到快照（仍可继续，只是无法回滚）。
    """
    # 1) 精查：mem0 2.x 支持 get(memory_id)，避免全量扫描
    try:
        got = memory.get(memory_id)
        if isinstance(got, dict):
            item = got.get("results", [got]) if isinstance(got.get("results", {}), list) else [got]
            if isinstance(item, list) and item and isinstance(item[0], dict):
                return str(item[0].get("memory") or item[0].get("content") or "").strip()
            if isinstance(got, dict):
                return str(got.get("memory") or got.get("content") or "").strip()
        if isinstance(got, list) and got and isinstance(got[0], dict):
            return str(got[0].get("memory") or got[0].get("content") or "").strip()
    except Exception as e:
        logger.debug(f"memory.get 精查快照失败，回退 get_all: {e}")

    # 2) 兜底：全量扫描（兼容不支持 get() 的旧版本）
    try:
        all_mem = memory.get_all(top_k=10000)
        results = all_mem.get("results", all_mem) if isinstance(all_mem, dict) else all_mem
        for item in results or []:
            if not isinstance(item, dict):
                continue
            mid = item.get("id") or item.get("memory_id", "")
            if mid == memory_id:
                return str(item.get("memory") or item.get("content") or "").strip()
    except Exception as e:
        logger.debug(f"get_all 快照失败: {e}")
    return ""


def _log_edit(memory_id: str, user_id: str, action: str, old_content: str,
              new_content: str, reason: str, confidence: float) -> int:
    ensure_self_edit_schema()
    conn = get_facts_conn()
    try:
        cur = conn.execute(
            "INSERT INTO memory_edits (memory_id, user_id, action, old_content, new_content, reason, confidence) "
            "VALUES (?,?,?,?,?,?,?)",
            (memory_id, user_id, action, old_content, new_content, reason, confidence),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    except Exception as e:
        logger.warning(f"编辑账本写入失败: {e}")
        return 0
    finally:
        conn.close()


def self_edit_on_add(memory, user_id: str, messages_json: Any, metadata: dict) -> Optional[dict]:
    """
    写入前自编辑入口。返回 None 表示「无需合并，按正常流程新增」；
    否则返回 {action, memory_id, merged_content, edit_id, confidence}。
    LLM 不可用 / 判定全新 / 任何异常 → 一律 None（回退 Layer1 原流程）。
    """
    if not SELF_EDIT_ENABLED or memory is None:
        return None

    new_text = _extract_text(messages_json)
    if not new_text:
        return None

    try:
        relation = _detect_relation(memory, user_id, new_text)
    except Exception as e:
        logger.debug(f"self-edit 检测异常（降级）: {e}")
        return None
    if not relation:
        return None

    memory_id = relation["memory_id"]
    old_content = _snapshot_old(memory, memory_id)
    merged = relation["merged_content"]

    # 合并更新时剥离 recorded_at：add.py 为「新增」路径统一 stamp 的
    # 当前时间戳不能覆盖旧记忆的原始时间，否则 before/after 时间推理
    # 会把一条老记忆误判成「刚刚产生」。
    merge_metadata = dict(metadata or {})
    merge_metadata.pop("recorded_at", None)

    try:
        memory.update(memory_id, merged, metadata=merge_metadata)
    except Exception as e:
        logger.warning(f"self-edit 合并更新失败（降级为新增）: {e}")
        return None

    edit_id = _log_edit(
        memory_id, user_id, relation["decision"],
        old_content, merged, relation["reason"], relation["confidence"],
    )
    logger.info(
        "✂️ self-edit: [%s] %s → %s (edit_id=%d)",
        relation["decision"], memory_id[:8], memory_id[:8], edit_id,
    )
    return {
        "action": relation["decision"],
        "memory_id": memory_id,
        "merged_content": merged,
        "edit_id": edit_id,
        "confidence": relation["confidence"],
        "reason": relation["reason"],
    }


def rollback_edit(edit_id: int, memory=None) -> dict:
    """回滚一次自编辑：把记忆恢复到编辑前内容（用户可回滚合并）。"""
    ensure_self_edit_schema()
    conn = get_facts_conn()
    row = conn.execute(
        "SELECT * FROM memory_edits WHERE edit_id=? AND undone=0", (edit_id,)
    ).fetchone()
    if not row:
        conn.close()
        return {"status": "error", "detail": f"edit_id={edit_id} 不存在或已回滚"}

    old_content = row["old_content"]
    memory_id = row["memory_id"]

    if not old_content:
        conn.close()
        return {"status": "error", "detail": "该编辑无旧内容快照，无法回滚"}

    mem = memory
    if mem is None:
        try:
            from ducky.mem0_runtime import get_memory
            mem = get_memory()
        except Exception:
            mem = None
    if mem is None:
        conn.close()
        return {"status": "error", "detail": "mem0 不可用，无法回滚"}

    try:
        mem.update(memory_id, old_content)
    except Exception as e:
        conn.close()
        return {"status": "error", "detail": f"恢复失败: {e}"}

    # 回滚后同步 FTS / salience 索引，否则 BM25 关键词搜索仍会命中
    # 合并后的文本（旧记忆内容已恢复，索引却还是新的）。
    try:
        from ducky.text_fts import _index_memory
        _index_memory(memory_id, old_content, user_id=row["user_id"], category="")
    except Exception as e:
        logger.debug(f"回滚 FTS 同步跳过: {e}")
    try:
        from ducky.salience.core import on_memory_added
        on_memory_added(memory_id, content=old_content, preserve_heat=True)
    except Exception as e:
        logger.debug(f"回滚 salience 同步跳过: {e}")

    conn.execute(
        "UPDATE memory_edits SET undone=1 WHERE edit_id=?", (edit_id,)
    )
    conn.commit()
    conn.close()
    logger.info("↩️ self-edit 回滚: edit_id=%d memory_id=%s", edit_id, memory_id)
    return {"status": "ok", "edit_id": edit_id, "memory_id": memory_id, "restored": old_content}


def list_edits(user_id: str = DEFAULT_USER_ID, limit: int = 20, include_undone: bool = False) -> list[dict]:
    """列出编辑账本（新的在前）。"""
    ensure_self_edit_schema()
    conn = get_facts_conn()
    try:
        sql = "SELECT * FROM memory_edits WHERE user_id=?"
        if not include_undone:
            sql += " AND undone=0"
        sql += " ORDER BY edit_id DESC LIMIT ?"
        rows = [dict(r) for r in conn.execute(sql, (user_id, max(1, min(int(limit), 200)))).fetchall()]
        return rows
    finally:
        conn.close()
