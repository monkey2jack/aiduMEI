"""ducky.salience.conflict — 反义词矛盾检测"""
from __future__ import annotations

import logging
import sqlite3

from ducky.bank_contract import is_legacy_schema_error
from ducky.utils import DEFAULT_USER_ID, get_salience_conn

logger = logging.getLogger("aiduMEM.salience")


def _canon_uid(uid: str) -> str:
    """把改名后的默认身份与字面量 'default' 折叠为同一组。

    存量 salience 行回填的是字面量 'default'，而新写入盖的是
    AIDUMEM_DEFAULT_USER_ID（部署方可能改名成 dudu）。不折叠的话，
    同一个真实域的新旧记忆会被拆成两组，老记忆的矛盾从此漏检
    （与 reflect._identity_ids 的 v19.4.2 教训同源，只放宽分组不改数据）。
    """
    return "default" if uid == DEFAULT_USER_ID else uid

_ANTONYM_PAIRS = [
    ("开", "关"), ("启用", "禁用"), ("允许", "禁止"), ("要", "不要"),
    ("是", "不是"), ("有", "没有"), ("能", "不能"), ("记得", "忘记"),
    ("成功", "失败"), ("对", "错"), ("真", "假"), ("新", "旧"),
    ("快", "慢"), ("大", "小"), ("多", "少"),
]

_CONFLICT_PENALTY = 0.5  # 检测到矛盾时显著性减半


def detect_conflicts() -> list[dict]:
    """扫描同 (user, bank, lane) 内反义词碰撞，返回冲突列表

    v20 P0-2：v19 只按 lane 分组，甲库一句「要」会跟乙库一句「不要」配对，
    然后 resolve_conflict_salience 把**两库**的显著性都腰斩——跨库写污染。
    现在配对永远不跨作用域；旧库缺作用域列时退回 v19 查询
    （全库本就是单一 default 域，行为不变）。
    """
    conn = get_salience_conn()
    try:
        rows = conn.execute(
            "SELECT memory_id, lane, content_preview, user_id, bank_id "
            "FROM salience WHERE content_preview != ''"
        ).fetchall()
    except sqlite3.Error as exc:
        # 这个降级出口把每一行的作用域**改写**成 ("default","default")。
        # 老库缺作用域列时它是对的（全库本就是单一 default 域）；但原来用
        # except Exception 去接，任何一次查询故障都会让具名域的行被贴上
        # default 标签，于是甲库的「要」重新能跟乙库的「不要」配对，
        # resolve_conflict_salience 再把两库的显著性一起腰斩 —— 正是这段
        # 注释声称已经堵掉的那条跨库写污染。先验明病因。
        if not is_legacy_schema_error(exc):
            raise
        logger.warning("salience 表无作用域列，冲突检测退回 v19 全库口径：%s", exc)
        rows = [
            (mid, lane, content, "default", "default")
            for mid, lane, content in conn.execute(
                "SELECT memory_id, lane, content_preview FROM salience WHERE content_preview != ''"
            ).fetchall()
        ]
    conn.close()

    if len(rows) < 2:
        return []

    # 按 (作用域, lane) 分组——配对绝不跨库
    lane_groups: dict[tuple[str, str, str], list[tuple[str, str]]] = {}
    for mid, lane, content, uid, bid in rows:
        lane_groups.setdefault((_canon_uid(uid), bid, lane), []).append((mid, content))

    conflicts = []
    for (uid, bid, lane), items in lane_groups.items():
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
                            "user_id": uid,
                            "bank_id": bid,
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
            "⚠️ 矛盾: %s | lane=%s | scope=%s/%s | %s ↔ %s",
            c["word_pair"], c["lane"],
            c.get("user_id", "default"), c.get("bank_id", "default"),
            c["preview_a"][:30], c["preview_b"][:30],
        )
    conn.commit()
    conn.close()
    return resolved
