"""
ducky.conflict_resolver — 显式冲突消解器 (v16.0 Opus Octopod · opus八爪鱼)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
用于检测并消解事实/偏好中的显式矛盾与新旧替换。
包含：
1. 属性级同域新旧覆盖 (Key-Value Override Detection)
2. 反义词与互斥状态碰撞检测 (Antonym & Mutual Exclusion Resolution)
3. 过期降权机制 (valid_to 标记 + 显著性衰减)
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from ducky.utils import get_facts_conn

logger = logging.getLogger("aiduMEM.ConflictResolver")

# 互斥属性与关键词组（显式替换规则）
# 当新事实包含 key 且匹配 value 模式时，将旧匹配项置为无效 (valid_to = now)
MUTUAL_EXCLUSION_PATTERNS = [
    # 域名/URL 变动
    (r"(域名|domain|site|url|网址)", r"(\.ccwu\.cc|\.cc\.cd)", r"(\.hycoforce\.com)"),
    # 名称/品牌更名
    (r"(名称|名字|title|name|模块)", r"(驭马实录)", r"(驭马江湖)"),
    # 状态开关
    (r"(开关|状态|status|mode)", r"(开启|启用|open|enable)", r"(关闭|禁用|close|disable)"),
    (r"(开关|状态|status|mode)", r"(关闭|禁用|close|disable)", r"(开启|启用|open|enable)"),
]


def resolve_fact_conflict(category: str, fact_key: str, new_value: str, user_id: str = "dudu") -> dict[str, Any]:
    """
    当写入/更新某个 (category, fact_key) 时：
    1. 检查是否存在旧的同 category & fact_key 但 value 不同的记录；
    2. 若存在，将旧记录的 valid_to 设为当前时间（软失效降权而非物理删除）；
    3. 返回消解结果。
    """
    conn = get_facts_conn()
    now_str = datetime.now(timezone.utc).isoformat()
    invalidated_count = 0
    try:
        cursor = conn.cursor()
        # 查找有效状态下 key 相同但 value 不同的记录
        rows = cursor.execute(
            """
            SELECT id, fact_value FROM facts 
            WHERE category = ? AND fact_key = ? AND (valid_to IS NULL OR valid_to > ?)
            """,
            (category, fact_key, now_str),
        ).fetchall()

        for fid, old_val in rows:
            if old_val != new_value:
                cursor.execute(
                    "UPDATE facts SET valid_to = ?, updated_at = ? WHERE id = ?",
                    (now_str, now_str, fid),
                )
                invalidated_count += 1
                logger.info(
                    "🐙 [ConflictResolver] 属性级覆盖: key='%s' old='%s' -> new='%s' (id=%d 已失效)",
                    fact_key, old_val, new_value, fid
                )
        conn.commit()
    except Exception as e:
        logger.error("🐙 [ConflictResolver] resolve_fact_conflict 失败: %s", e)
    finally:
        conn.close()

    return {"invalidated": invalidated_count, "category": category, "fact_key": fact_key}


def scan_and_resolve_text_conflicts(new_text: str, user_id: str = "dudu") -> list[dict[str, Any]]:
    """
    针对输入的文本内容，检测是否触及显式互斥规则（如 .old-domain -> .hycoforce.com，驭马实录 -> 驭马江湖）。
    若新文本匹配到新规则，则自动扫描 facts DB 中匹配旧规则的条目，将其标记 valid_to = NOW()。
    """
    resolved_actions = []
    now_str = datetime.now(timezone.utc).isoformat()
    conn = get_facts_conn()
    try:
        cursor = conn.cursor()
        for attr_re, old_re, new_re in MUTUAL_EXCLUSION_PATTERNS:
            if re.search(new_re, new_text, re.IGNORECASE):
                # 命中了新规则，检索旧规则条目
                rows = cursor.execute(
                    "SELECT id, fact_key, fact_value FROM facts WHERE (valid_to IS NULL OR valid_to > ?)",
                    (now_str,),
                ).fetchall()
                for fid, fkey, fval in rows:
                    if re.search(old_re, fval, re.IGNORECASE) or re.search(old_re, fkey, re.IGNORECASE):
                        cursor.execute(
                            "UPDATE facts SET valid_to = ?, updated_at = ? WHERE id = ?",
                            (now_str, now_str, fid),
                        )
                        resolved_actions.append({
                            "fact_id": fid,
                            "fact_key": fkey,
                            "old_value": fval,
                            "reason": f"规则触发: {old_re} -> {new_re}",
                        })
                        logger.info(
                            "🐙 [ConflictResolver] 规则消解: id=%d key='%s' 旧值='%s' 因匹配到新规则 '%s' 被降权失效",
                            fid, fkey, fval, new_re
                        )
        conn.commit()
    except Exception as e:
        logger.error("🐙 [ConflictResolver] scan_and_resolve_text_conflicts 失败: %s", e)
    finally:
        conn.close()

    return resolved_actions
