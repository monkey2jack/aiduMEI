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
    status            TEXT DEFAULT 'candidate', -- candidate | approved | archived | draft
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    use_count         INTEGER DEFAULT 0,     -- v19.0 P1-2：复用总次数
    success_count     INTEGER DEFAULT 0,     -- v19.0 P1-2：复用成功次数
    fail_count        INTEGER DEFAULT 0      -- v19.0 P1-2：复用失败次数
);
"""

# v19.0 P1-2 技能精炼：连续观察至少这么多轮复用，才允许自动判定效用
_MIN_USES_FOR_UTILITY = 3
# 成功率低于此阈值 → 自动标记为 archived（待淘汰），需人工复核
_LOW_UTILITY_SUCCESS_RATE = 0.34


def _migrate_crystal_columns(conn) -> None:
    """v19.0 P1-2 迁移：给旧 skill_crystals 表补全代码引用的列（幂等）。

    兼容两代历史 schema：
      - 最老版（生产）：sample_facts，无 source_categories/sample_keys/candidate_count
      - v17 版：有 source_categories/sample_keys/candidate_count，无 use/success/fail
    新代码（skill_growth / record_skill_use / prune_low_utility_skills）需要的列：
      source_categories TEXT, sample_keys TEXT, candidate_count INT,
      use_count INT, success_count INT, fail_count INT
    """
    _COLUMN_DDL = {
        "source_categories": "TEXT DEFAULT ''",
        "sample_keys": "TEXT DEFAULT ''",
        "candidate_count": "INTEGER DEFAULT 0",
        "use_count": "INTEGER DEFAULT 0",
        "success_count": "INTEGER DEFAULT 0",
        "fail_count": "INTEGER DEFAULT 0",
    }
    existing = {r[1] for r in conn.execute("PRAGMA table_info(skill_crystals)").fetchall()}
    for col, ddl in _COLUMN_DDL.items():
        if col in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE skill_crystals ADD COLUMN {col} {ddl}")
        except Exception:
            # 并发建表等极端情况——忽略，下轮再迁移
            pass

# 结晶最小事实数阈值：分类下至少 3 条不同 fact_key 才触发结晶（避免噪声过度生成）
_MIN_FACTS_FOR_CRYSTAL = 3

# 排除的噪声分类（这些分类事实过于碎片化，不适合结晶为技能）
_EXCLUDED_CATEGORIES = frozenset({
    "general", "uncategorized", "Experience", "emotion",
    "session", "temp", "draft",
})


def init_crystallizer_schema() -> None:
    """初始化 skill_crystals 表（含 v19.0 P1-2 精炼字段迁移）"""
    conn = get_facts_conn()
    try:
        conn.executescript(_CRYSTAL_SCHEMA_DDL)
        _migrate_crystal_columns(conn)
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
                   status, created_at, updated_at,
                   use_count, success_count, fail_count
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
                "use_count": r[11],
                "success_count": r[12],
                "fail_count": r[13],
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


def record_skill_use(skill_name: str, success: bool) -> dict[str, Any]:
    """v19.0 P1-2 技能精炼：记录一次技能复用成功/失败。

    - 成功后优化描述（hit_count 微升，复用是技能有效的信号）
    - 失败后标注陷阱（fail_count 累计，供 prune_low_utility_skills 判定）
    - 返回 {status, skill_name, use_count, success_count, fail_count, low_utility}
    """
    init_crystallizer_schema()
    conn = get_facts_conn()
    try:
        row = conn.execute(
            "SELECT crystal_id, status FROM skill_crystals WHERE skill_name=?", (skill_name,)
        ).fetchone()
        if not row:
            return {"status": "error", "message": f"技能 '{skill_name}' 不存在"}
        conn.execute(
            """
            UPDATE skill_crystals SET
                use_count     = use_count + 1,
                success_count = success_count + ?,
                fail_count    = fail_count + ?,
                hit_count     = CASE WHEN ? THEN hit_count + 1 ELSE hit_count END,
                updated_at    = CURRENT_TIMESTAMP
            WHERE skill_name = ?
            """,
            (1 if success else 0, 0 if success else 1, 1 if success else 0, skill_name),
        )
        conn.commit()
        stats = conn.execute(
            "SELECT use_count, success_count, fail_count FROM skill_crystals WHERE skill_name=?",
            (skill_name,),
        ).fetchone()
        use_count, success_count, fail_count = stats[0], stats[1], stats[2]
        low_utility = (
            use_count >= _MIN_USES_FOR_UTILITY
            and success_count / max(use_count, 1) < _LOW_UTILITY_SUCCESS_RATE
        )
        logger.info(
            "🐙 [SkillCrystallizer] 技能复用记录: '%s' %s (use=%d ok=%d fail=%d)",
            skill_name, "成功" if success else "失败", use_count, success_count, fail_count,
        )
        return {
            "status": "ok",
            "skill_name": skill_name,
            "use_count": use_count,
            "success_count": success_count,
            "fail_count": fail_count,
            "low_utility": low_utility,
        }
    except Exception as e:
        logger.error("🐙 [SkillCrystallizer] record_skill_use 失败: %s", e)
        return {"status": "error", "message": str(e)}
    finally:
        conn.close()


def prune_low_utility_skills() -> list[dict[str, Any]]:
    """v19.0 P1-2 技能精炼淘汰：低效用技能自动标记为 archived（待淘汰）。

    判定：use_count >= _MIN_USES_FOR_UTILITY 且成功率 < _LOW_UTILITY_SUCCESS_RATE。
    只降级 approved/candidate 技能；draft 草稿由人工决定去留，本函数不动。
    不物理删除数据，仅改 status='archived'，可人工复核后恢复。
    """
    init_crystallizer_schema()
    conn = get_facts_conn()
    archived: list[dict[str, Any]] = []
    try:
        rows = conn.execute(
            f"""
            SELECT skill_name, use_count, success_count, fail_count
            FROM skill_crystals
            WHERE status IN ('approved', 'candidate')
              AND use_count >= {_MIN_USES_FOR_UTILITY}
              AND success_count * 1.0 / use_count < {_LOW_UTILITY_SUCCESS_RATE}
            """
        ).fetchall()
        for skill_name, use_count, success_count, fail_count in rows:
            conn.execute(
                "UPDATE skill_crystals SET status='archived', updated_at=CURRENT_TIMESTAMP WHERE skill_name=?",
                (skill_name,),
            )
            archived.append({
                "skill_name": skill_name,
                "use_count": use_count,
                "success_count": success_count,
                "fail_count": fail_count,
                "success_rate": round(success_count / max(use_count, 1), 3),
            })
            logger.info(
                "🐙 [SkillCrystallizer] 低效用技能标记待淘汰: '%s' (成功率 %.0f%%)",
                skill_name, 100 * success_count / max(use_count, 1),
            )
        conn.commit()
    except Exception as e:
        logger.error("🐙 [SkillCrystallizer] prune_low_utility_skills 失败: %s", e)
    finally:
        conn.close()
    return archived
