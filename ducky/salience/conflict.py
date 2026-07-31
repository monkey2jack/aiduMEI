"""ducky.salience.conflict — 反义词矛盾检测"""
from __future__ import annotations

import logging

from ducky.utils import get_salience_conn

logger = logging.getLogger("aiduMEM.salience")

_ANTONYM_PAIRS = [
    ("开", "关"), ("启用", "禁用"), ("允许", "禁止"), ("要", "不要"),
    ("是", "不是"), ("有", "没有"), ("能", "不能"), ("记得", "忘记"),
    ("成功", "失败"), ("对", "错"), ("真", "假"), ("新", "旧"),
    ("快", "慢"), ("大", "小"), ("多", "少"),
]

_CONFLICT_PENALTY = 0.5  # 检测到矛盾时显著性减半


def detect_conflicts() -> list[dict]:
    """扫描同 Lane 内反义词碰撞，返回冲突列表"""
    conn = get_salience_conn()
    rows = conn.execute(
        "SELECT memory_id, lane, content_preview FROM salience WHERE content_preview != ''"
    ).fetchall()
    conn.close()

    if len(rows) < 2:
        return []

    # 按 lane 分组
    lane_groups: dict[str, list[tuple[str, str]]] = {}
    for mid, lane, content in rows:
        lane_groups.setdefault(lane, []).append((mid, content))

    conflicts = []
    for lane, items in lane_groups.items():
        if len(items) < 2:
            continue
        for i in range(len(items)):
            mid_a, ca = items[i]
            for j in range(i + 1, len(items)):
                mid_b, cb = items[j]
                for pos, neg in _ANTONYM_PAIRS:
                    a_pos, a_neg = pos in ca, neg in ca
                    b_pos, b_neg = pos in cb, neg in cb
                    if (a_pos and b_neg) or (a_neg and b_pos):
                        conflicts.append({
                            "lane": lane,
                            "memory_a": mid_a,
                            "memory_b": mid_b,
                            "word_pair": f"{pos}↔{neg}",
                            "preview_a": ca[:60],
                            "preview_b": cb[:60],
                        })
                        break  # 一对记忆只报一次
    return conflicts


def resolve_conflict_salience(conflicts: list[dict]) -> int:
    """降低冲突记忆显著性（对半衰减），返回受影响条数"""
    if not conflicts:
        return 0
    conn = get_salience_conn()
    resolved = 0
    for c in conflicts:
        for mid in (c["memory_a"], c["memory_b"]):
            conn.execute(
                "UPDATE salience SET salience = salience * ? WHERE memory_id = ?",
                (_CONFLICT_PENALTY, mid),
            )
            resolved += 1
        logger.warning(
            "⚠️ 矛盾: %s | lane=%s | %s ↔ %s",
            c["word_pair"], c["lane"],
            c["preview_a"][:30], c["preview_b"][:30],
        )
    conn.commit()
    conn.close()
    return resolved
