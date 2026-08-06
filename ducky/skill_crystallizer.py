"""
ducky.skill_crystallizer — 记忆向技能结晶器 (v17.0 · 借鉴 Mímir 联邦记忆系统)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
自动感知与归纳高频重复事实 / 操作流程，将其"结晶"(Crystallize) 为结构化 Skill 候选项。

借鉴来源: Mímir v9.1 联邦记忆系统 + MemOS SkillCrystallizer
  - 高频操作模式自动结晶为 Agent 技能候选项
  - 与 Hermes skill_manage 工作流对齐（候选项需人工审核后才能落地）
  - 借鉴 Mímir "LLM 只能建议，不能直接 commit" 的治理铁律

v17.0 修复:
  - detect_and_crystallize_patterns 改为提取 fact_key 模式，不再 GROUP_CONCAT 完整内容
  - 结晶的 procedure 聚焦"操作步骤摘要"而非原始记忆拼接
  - 增加 source_categories 字段记录结晶来源分类
  - 增加 candidate_count 字段限制过度生成（分类 < 3 条不结晶）
"""
from __future__ import annotations

import logging
from typing import Any

from ducky.utils import get_facts_conn

logger = logging.getLogger("aiduMEM.SkillCrystallizer")

_CRYSTAL_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS skill_crystals (
    crystal_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_name        TEXT NOT NULL UNIQUE,
    trigger_rule      TEXT NOT NULL,
    procedure         TEXT NOT NULL,
    source_categories TEXT DEFAULT '',
    sample_keys       TEXT DEFAULT '',
    hit_count         INTEGER DEFAULT 1,
    candidate_count   INTEGER DEFAULT 0,
    status            TEXT DEFAULT 'candidate', -- candidate | approved | archived
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# 结晶最小事实数阈值：分类下至少 3 条不同 fact_key 才触发结晶（避免噪声过度生成）
_MIN_FACTS_FOR_CRYSTAL = 3

# 排除的噪声分类（这些分类事实过于碎片化，不适合结晶为技能）
_EXCLUDED_CATEGORIES = frozenset({
    "general", "uncategorized", "Experience", "emotion",
    "session", "temp", "draft",
})


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

    策略（v17 优化）:
    - 按 category 分组，统计有效 fact 数量（archived=0, valid_to=NULL 或未过期）
    - 阈值 >= _MIN_FACTS_FOR_CRYSTAL 才结晶，过滤噪声
    - procedure 提取 fact_key 列表，不塞完整内容（避免超长无意义拼接）
    - 结晶遵循 Mímir 铁律：只是候选项，需人工审核后才能 approved 落地

    在 24h 后台 consolidator 定时任务中调用。
    """
    init_crystallizer_schema()
    conn = get_facts_conn()
    crystals_added: list[dict[str, Any]] = []
    now_placeholder = "2099-12-31"  # valid_to 比较用

    try:
        # 查找有效状态的高频分类（精确过滤噪声分类）
        rows = conn.execute(
            f"""
            SELECT
                category,
                COUNT(*) AS cnt,
                GROUP_CONCAT(DISTINCT fact_key, ' | ') AS key_summary
            FROM facts
            WHERE
                archived = 0
                AND category NOT IN ({','.join('?' * len(_EXCLUDED_CATEGORIES))})
                AND (valid_to IS NULL OR valid_to > CURRENT_TIMESTAMP)
            GROUP BY category
            HAVING cnt >= ?
            ORDER BY cnt DESC
            LIMIT 20
            """,
            (*_EXCLUDED_CATEGORIES, _MIN_FACTS_FOR_CRYSTAL),
        ).fetchall()

        for category, cnt, key_summary in rows:
            skill_name = f"crystallized-{category.lower().replace(' ', '-')[:40]}"
            trigger_rule = f"当出现与「{category}」相关的连续需求或重复操作时触发"
            # procedure 只记录 fact_key 摘要，不塞完整内容
            keys_preview = (key_summary or "")[:300]
            procedure = (
                f"分类「{category}」下共有 {cnt} 条记忆事实，\n"
                f"高频操作键：{keys_preview}\n"
                f"（需人工审核后，将此候选项转化为正式 SKILL.md）"
            )

            conn.execute(
                """
                INSERT INTO skill_crystals
                    (skill_name, trigger_rule, procedure, source_categories, sample_keys, hit_count, candidate_count)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(skill_name) DO UPDATE SET
                    hit_count      = hit_count + 1,
                    candidate_count= excluded.candidate_count,
                    procedure      = excluded.procedure,
                    sample_keys    = excluded.sample_keys,
                    updated_at     = CURRENT_TIMESTAMP
                """,
                (skill_name, trigger_rule, procedure, category, keys_preview[:200], cnt),
            )
            crystals_added.append({
                "skill_name": skill_name,
                "count": cnt,
                "category": category,
            })
            logger.info(
                "🐙 [SkillCrystallizer] 结晶感知: 生成/更新候选项 '%s' (事实数=%d)",
                skill_name, cnt,
            )

        conn.commit()
    except Exception as e:
        logger.error("🐙 [SkillCrystallizer] detect_and_crystallize_patterns 失败: %s", e)
    finally:
        conn.close()

    return crystals_added


def list_crystals(status: str = "candidate") -> list[dict[str, Any]]:
    """查询已沉淀的技能结晶（按 hit_count 排序，命中越多越值得关注）"""
    init_crystallizer_schema()
    conn = get_facts_conn()
    try:
        rows = conn.execute(
            """
            SELECT crystal_id, skill_name, trigger_rule, procedure,
                   source_categories, sample_keys, hit_count, candidate_count,
                   status, created_at, updated_at
            FROM skill_crystals
            WHERE status = ? OR ? = 'all'
            ORDER BY hit_count DESC, candidate_count DESC
            """,
            (status, status),
        ).fetchall()
        return [
            {
                "crystal_id": r[0],
                "skill_name": r[1],
                "trigger_rule": r[2],
                "procedure": r[3],
                "source_categories": r[4],
                "sample_keys": r[5],
                "hit_count": r[6],
                "candidate_count": r[7],
                "status": r[8],
                "created_at": r[9],
                "updated_at": r[10],
            }
            for r in rows
        ]
    except Exception as e:
        logger.error("🐙 [SkillCrystallizer] list_crystals 失败: %s", e)
        return []
    finally:
        conn.close()


def approve_crystal(crystal_id: int) -> dict[str, Any]:
    """
    人工审核通过某个结晶候选项（状态 candidate -> approved）。
    遵循 Mímir 铁律：只有人工审核才能 approve，不可自动批准。
    """
    conn = get_facts_conn()
    try:
        conn.execute(
            "UPDATE skill_crystals SET status = 'approved', updated_at = CURRENT_TIMESTAMP WHERE crystal_id = ?",
            (crystal_id,),
        )
        conn.commit()
        logger.info("🐙 [SkillCrystallizer] 人工审核通过: crystal_id=%d", crystal_id)
        return {"status": "ok", "crystal_id": crystal_id, "new_status": "approved"}
    except Exception as e:
        logger.error("🐙 [SkillCrystallizer] approve_crystal 失败: %s", e)
        return {"status": "error", "message": str(e)}
    finally:
        conn.close()
