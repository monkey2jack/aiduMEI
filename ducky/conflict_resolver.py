"""
ducky.conflict_resolver — 显式冲突消解器 (v17.0 · 借鉴 Mímir 联邦记忆系统)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
用于检测并消解事实/偏好中的显式矛盾与新旧替换。

借鉴来源: Mímir v9.1 联邦记忆系统 (Sandro 项目)
  - 属性级 Key-Value 覆盖检测（同 category+key 的旧值打 valid_to 失效降权）
  - 显式互斥规则消解（规则集可扩展，代替向量相似度盲覆盖）
  - 与 v12 Chronos 双时间轴协同：软失效降权不删除，历史可溯

包含:
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

# ── 互斥属性规则集（可扩展，脱敏版本，不含具体业务内容）──────────────────────────
# 格式: (属性类型正则, 旧值模式正则, 新值模式正则)
# 当新文本匹配 new_re 时，自动失效数据库中匹配 old_re 的条目
MUTUAL_EXCLUSION_PATTERNS: list[tuple[str, str, str]] = [
    # 域名/URL 变动（通用模式，具体域名在运行时通过配置文件注入）
    (r"(域名|domain|site|url|网址)", r"old_domain_placeholder", r"new_domain_placeholder"),
    # 名称/品牌更名（通用模式）
    (r"(名称|名字|title|name|模块)", r"old_name_placeholder", r"new_name_placeholder"),
    # 状态开关（双向）
    (r"(开关|状态|status|mode)", r"(开启|启用|open|enable)", r"(关闭|禁用|close|disable)"),
    (r"(开关|状态|status|mode)", r"(关闭|禁用|close|disable)", r"(开启|启用|open|enable)"),
]


def load_custom_exclusion_patterns(patterns: list[tuple[str, str, str]]) -> None:
    """
    运行时注入自定义互斥规则（替换占位符或追加规则）。
    在 api_server 启动时调用，注入具体业务的域名/名称变动规则。

    示例:
        load_custom_exclusion_patterns([
            (r"(域名|url)", r"old\\.example\\.com", r"new\\.example\\.com"),
        ])
    """
    global MUTUAL_EXCLUSION_PATTERNS
    # 追加运行时规则（不覆盖基础规则）
    MUTUAL_EXCLUSION_PATTERNS = [
        p for p in MUTUAL_EXCLUSION_PATTERNS
        if "placeholder" not in p[1]  # 移除未初始化的占位符
    ] + patterns
    logger.info("🐙 [ConflictResolver] 已加载 %d 条自定义互斥规则", len(patterns))


def resolve_fact_conflict(
    category: str, fact_key: str, new_value: str, user_id: str = "default"
) -> dict[str, Any]:
    """
    当写入/更新某个 (category, fact_key) 时：
    1. 检查是否存在旧的同 category & fact_key 但 value 不同的有效记录；
    2. 若存在，将旧记录的 valid_to 设为当前时间（软失效降权而非物理删除）；
    3. 同时写入 fact_events 变更账本（可溯源）；
    4. 返回消解结果。
    """
    conn = get_facts_conn()
    now_str = datetime.now(timezone.utc).isoformat()
    invalidated_count = 0
    invalidated_ids: list[int] = []
    try:
        cursor = conn.cursor()
        # 仅查找有效状态下 key 相同但 value 不同的记录（利用索引 idx_facts_unique）
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
                invalidated_ids.append(fid)
                logger.info(
                    "🐙 [ConflictResolver] 属性级覆盖: key='%s' old='%s' -> new='%s' (id=%d 已失效)",
                    fact_key, old_val[:80], new_value[:80], fid,
                )

        # 写入变更账本（如果有消解动作）
        if invalidated_ids:
            _append_conflict_event(cursor, category, fact_key, new_value, invalidated_ids, now_str)

        conn.commit()
    except Exception as e:
        logger.error("🐙 [ConflictResolver] resolve_fact_conflict 失败: %s", e)
    finally:
        conn.close()

    return {"invalidated": invalidated_count, "category": category, "fact_key": fact_key}


def scan_and_resolve_text_conflicts(new_text: str, user_id: str = "default") -> list[dict[str, Any]]:
    """
    针对输入的文本内容，检测是否触及显式互斥规则。
    若新文本匹配到新规则模式，则扫描 facts DB 中匹配旧规则的条目，
    将其标记 valid_to = NOW()（软失效，不删除）。

    性能优化: 先在内存中做规则匹配，只在命中时才查数据库。
    """
    # 先判断文本中是否有任何规则被命中（避免无效 DB 查询）
    triggered_patterns = [
        (attr_re, old_re, new_re)
        for attr_re, old_re, new_re in MUTUAL_EXCLUSION_PATTERNS
        if re.search(new_re, new_text, re.IGNORECASE)
    ]
    if not triggered_patterns:
        return []  # 快速返回，不查 DB

    resolved_actions: list[dict[str, Any]] = []
    now_str = datetime.now(timezone.utc).isoformat()
    conn = get_facts_conn()
    try:
        cursor = conn.cursor()
        # 仅对命中的规则做针对性查询
        for attr_re, old_re, new_re in triggered_patterns:
            rows = cursor.execute(
                """
                SELECT id, fact_key, fact_value FROM facts
                WHERE (valid_to IS NULL OR valid_to > ?)
                  AND (fact_value REGEXP ? OR fact_key REGEXP ?)
                """,
                (now_str, old_re, old_re),
            ).fetchall()
            # SQLite 不原生支持 REGEXP，回退到 Python 过滤
            if not rows:
                rows = cursor.execute(
                    "SELECT id, fact_key, fact_value FROM facts WHERE (valid_to IS NULL OR valid_to > ?)",
                    (now_str,),
                ).fetchall()
                rows = [
                    r for r in rows
                    if re.search(old_re, str(r[2]), re.IGNORECASE)
                    or re.search(old_re, str(r[1]), re.IGNORECASE)
                ]

            for fid, fkey, fval in rows:
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
                    "🐙 [ConflictResolver] 规则消解: id=%d key='%s' 旧值='%.60s' 因新规则 '%s' 失效",
                    fid, fkey, fval, new_re,
                )

        if resolved_actions:
            _append_conflict_event(
                cursor, "scan_resolve", new_text[:100],
                f"{len(resolved_actions)} facts invalidated",
                [a["fact_id"] for a in resolved_actions], now_str,
            )
        conn.commit()
    except Exception as e:
        logger.error("🐙 [ConflictResolver] scan_and_resolve_text_conflicts 失败: %s", e)
    finally:
        conn.close()

    return resolved_actions


def _append_conflict_event(
    cursor: Any,
    category: str,
    fact_key: str,
    new_value: str,
    invalidated_ids: list[int],
    now_str: str,
) -> None:
    """
    向 fact_events 变更账本追加一条冲突消解记录（借鉴 Mímir 事件账本设计）。
    若 fact_events 表不存在则静默跳过（兼容旧 schema）。
    """
    try:
        cursor.execute(
            """
            INSERT OR IGNORE INTO fact_events
                (event_type, category, fact_key, new_value, affected_ids, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "conflict.resolved",
                category,
                fact_key,
                new_value[:200],
                str(invalidated_ids),
                now_str,
            ),
        )
    except Exception:
        pass  # fact_events 表不存在时静默跳过，向前兼容
