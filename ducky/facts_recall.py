"""Facts 分层召回：确定性 SQL 检索、轨迹与上下文注入。"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from ducky.utils import get_facts_conn

_VALID_LEVELS = {"L0", "L1", "L2"}


def _normalize_level(level: str) -> str:
    normalized = (level or "L2").upper()
    return normalized if normalized in _VALID_LEVELS else "L2"


def _project_fact(row: dict[str, Any], level: str) -> dict[str, Any]:
    item = dict(row)
    if level == "L0":
        item["value"] = item.get("summary") or (item.get("fact_value") or "")[:60]
    elif level == "L1":
        item["value"] = item.get("overview") or item.get("fact_value") or ""
    else:
        item["value"] = item.get("fact_value") or ""
    item.pop("summary", None)
    item.pop("overview", None)
    return item


def search_facts(
    query: str,
    *,
    category: str | None = None,
    top_k: int = 10,
    level: str = "L2",
    min_trust: float = 0.0,
) -> dict[str, Any]:
    """检索 facts.db，返回稳定的分层结构与五阶段轨迹。"""
    started = time.perf_counter()
    level = _normalize_level(level)
    top_k = max(1, min(int(top_k), 100))
    effective_trust = max(0.2, float(min_trust))
    needle = (query or "").strip()
    like = f"%{needle}%"

    conn = get_facts_conn()
    try:
        category_rows = conn.execute(
            "SELECT DISTINCT category FROM facts WHERE archived=0 ORDER BY category"
        ).fetchall()
        categories = [row[0] for row in category_rows]
        category_candidates = [
            name for name in categories if needle and (needle in name or name in needle)
        ]
        intent_ms = round((time.perf_counter() - started) * 1000, 3)

        sql = """
            SELECT * FROM facts
            WHERE archived=0 AND trust_score>=?
              AND (?='' OR category LIKE ? OR fact_key LIKE ? OR fact_value LIKE ?)
        """
        params: list[Any] = [effective_trust, needle, like, like, like]
        if category:
            sql += " AND category=?"
            params.append(category)
        # Chronos 双时间轴：失效(valid_to<now)/未生效(valid_from>now)的事实降到最后，
        # 不删除、不过滤——铁律与无有效期字段(NULL)的事实完全不受影响。
        now_iso = datetime.now(timezone.utc).isoformat()
        sql += """
            ORDER BY
              CASE
                WHEN valid_to   IS NOT NULL AND valid_to   < ? THEN 2
                WHEN valid_from IS NOT NULL AND valid_from > ? THEN 2
                ELSE 0
              END,
              CASE WHEN fact_key=? THEN 0 WHEN category=? THEN 1 ELSE 2 END,
              trust_score DESC, updated_at DESC
            LIMIT ?
        """
        params.extend([now_iso, now_iso, needle, needle, top_k])
        rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
        position_ms = round((time.perf_counter() - started) * 1000 - intent_ms, 3)

        ids = [row["id"] for row in rows]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"""UPDATE facts
                    SET retrieval_count=retrieval_count+1,
                        last_accessed_at=CURRENT_TIMESTAMP
                    WHERE id IN ({placeholders})""",
                ids,
            )
            conn.commit()

        facts = [_project_fact(row, level) for row in rows]
        total_ms = round((time.perf_counter() - started) * 1000, 3)
        trajectory = [
            {"step": "intent_analysis", "category_candidates": category_candidates, "elapsed_ms": intent_ms},
            {"step": "position", "level": level, "elapsed_ms": position_ms},
            {"step": "retrieve", "scanned": len(rows), "hits": len(facts)},
            {"step": "trust_filter", "min_trust": effective_trust, "kept": len(facts)},
            {"step": "return", "count": len(facts), "elapsed_ms": total_ms},
        ]
        return {
            "status": "ok",
            "query": needle,
            "level": level,
            "facts": facts,
            "results": facts,
            "count": len(facts),
            "trajectory": trajectory,
        }
    finally:
        conn.close()


def inject_context(
    query: str,
    *,
    k: int = 5,
    level: str = "L0",
    max_tokens: int = 1000,
) -> dict[str, Any]:
    """按 token 预算拼接事实上下文。"""
    result = search_facts(query, top_k=k, level=level)
    budget_chars = max(0, int(max_tokens)) * 4
    lines: list[str] = []
    for fact in result["facts"]:
        line = f"- [{fact.get('category', 'general')}] {fact.get('fact_key', '')}: {fact.get('value', '')}"
        if budget_chars and sum(len(item) + 1 for item in lines) + len(line) > budget_chars:
            break
        lines.append(line)
    context = "\n".join(lines)
    return {
        "status": "ok",
        "query": query,
        "level": _normalize_level(level),
        "context": context,
        "facts": result["facts"][: len(lines)],
        "injected_facts": len(lines),
        "total_tokens": (len(context) + 3) // 4,
        "trajectory": result["trajectory"],
    }
