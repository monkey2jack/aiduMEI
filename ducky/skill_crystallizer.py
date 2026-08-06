"""
ducky.skill_crystallizer — 记忆向技能结晶器 (v16.0 Opus Octopod · opus八爪鱼)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
自动感知与归纳高频重复事实 / 操作流程，将其“结晶”(Crystallize) 为结构化 Skill 候选项。
"""
from __future__ import annotations

import logging
from typing import Any

from ducky.utils import get_facts_conn

logger = logging.getLogger("aiduMEM.SkillCrystallizer")

_CRYSTAL_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS skill_crystals (
    crystal_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name   TEXT NOT NULL UNIQUE,
    trigger_rule TEXT NOT NULL,
    procedure    TEXT NOT NULL,
    sample_facts TEXT DEFAULT '',
    hit_count    INTEGER DEFAULT 1,
    status       TEXT DEFAULT 'candidate', -- candidate | approved | archived
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


def init_crystallizer_schema() -> None:
    """初始化 skill_crystals 表"""
    conn = get_facts_conn()
    try:
        conn.executescript(_CRYSTAL_SCHEMA_DDL)
        conn.commit()
    except Exception as e:
        logger.error("🐙 [SkillCrystallizer] DDL 初始化失败: %s", e)
    finally:
        conn.close()


def detect_and_crystallize_patterns() -> list[dict[str, Any]]:
    """
    扫描 facts 数据库中的高频操作/踩坑记忆，聚类生成技能结晶候选项。
    在 24h 后台 consolidator 定时任务中调用。
    """
    init_crystallizer_schema()
    conn = get_facts_conn()
    crystals_added = []
    try:
        # 查找出现频次 ≥ 2 的高频分类或操作
        rows = conn.execute(
            """
            SELECT category, COUNT(*) as cnt, GROUP_CONCAT(fact_key || ': ' || fact_value, ' | ') as facts_summary
            FROM facts
            WHERE category NOT IN ('general', 'uncategorized') AND archived = 0
            GROUP BY category
            HAVING cnt >= 2
            """
        ).fetchall()

        for category, cnt, summary in rows:
            skill_name = f"crystallized-{category.lower().replace(' ', '-')}"
            trigger_rule = f"当出现与 {category} 相关的场景或连续需求时触发"
            procedure = f"自动整合自 {cnt} 条记忆片段:\n{summary}"

            # 插入或更新
            conn.execute(
                """
                INSERT INTO skill_crystals (skill_name, trigger_rule, procedure, sample_facts, hit_count)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(skill_name) DO UPDATE SET
                    hit_count = hit_count + 1,
                    procedure = excluded.procedure,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (skill_name, trigger_rule, procedure, summary[:500]),
            )
            crystals_added.append({
                "skill_name": skill_name,
                "count": cnt,
                "category": category,
            })
            logger.info("🐙 [SkillCrystallizer] 结晶感知: 生成技能候选项 %s (样本数=%d)", skill_name, cnt)

        conn.commit()
    except Exception as e:
        logger.error("🐙 [SkillCrystallizer] detect_and_crystallize_patterns 失败: %s", e)
    finally:
        conn.close()

    return crystals_added


def list_crystals(status: str = "candidate") -> list[dict[str, Any]]:
    """查询已沉淀的技能结晶"""
    init_crystallizer_schema()
    conn = get_facts_conn()
    try:
        rows = conn.execute(
            """
            SELECT crystal_id, skill_name, trigger_rule, procedure, hit_count, status, created_at
            FROM skill_crystals
            WHERE status = ? OR ? = 'all'
            ORDER BY hit_count DESC
            """,
            (status, status),
        ).fetchall()
        return [
            {
                "crystal_id": r[0],
                "skill_name": r[1],
                "trigger_rule": r[2],
                "procedure": r[3],
                "hit_count": r[4],
                "status": r[5],
                "created_at": r[6],
            }
            for r in rows
        ]
    except Exception as e:
        logger.error("🐙 [SkillCrystallizer] list_crystals 失败: %s", e)
        return []
    finally:
        conn.close()
