"""
ducky.skill_growth — 自动 Skill 生长（v19.0 · P1-2 · ReMe/MemU 借鉴）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
在 v17 skill_crystallizer 的基础上补上「从经验中自动生长技能」的
后半段链路：

    任务轨迹回放 → 步骤提取 → LLM 生成 SKILL.md 草稿 → 人工确认
    → 归档为 approved 技能。

治理铁律（沿用 Mímir）：**LLM 只能建议草稿，不能自动 commit**。
草稿永远落在 skill_crystals.status='draft'，必须由人工 approve。

与 v17 结晶器的关系：
    - v17 detect_and_crystallize_patterns 按 category 统计高频事实，
      产出 coarse 候选（procedure 只是 fact_key 摘要）
    - 本模块 grow_skill_from_trajectory 接收一次完整任务轨迹，
      产出可落地的 SKILL.md 草稿（frontmatter + 步骤 + 注意事项）
    两者共用 skill_crystals 表，status 多了一个 'draft' 态。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from typing import Any, Optional

from ducky.utils import get_facts_conn

logger = logging.getLogger("aiduMEM.skill_growth")

# 默认关闭自动 LLM 生成（保守：只在部署方明确开启后才消耗 LLM）
SKILL_GROWTH_ENABLED = os.environ.get("AIDUMEM_SKILL_GROWTH_ENABLED", "false").strip().lower() not in {
    "0", "false", "no", "off",
}

# 至少需要这么多有效步骤才值得生成技能草稿
_MIN_STEPS = 4

_SKILL_SYSTEM = (
    "你是 aiduMEI 的技能生长引擎。根据给定的任务轨迹，生成一份可复用的 "
    "SKILL.md 草稿。只输出 JSON，不要输出任何解释。"
)

_SKILL_USER_TEMPLATE = """任务轨迹：
{trajectory}

请提取可复用的操作步骤，输出 JSON：
{{
  "skill_name": "小写连字符命名，如 deploy-dashboard",
  "trigger": "一句话描述何时触发此技能",
  "steps": ["步骤1", "步骤2", "步骤3"],
  "cautions": ["注意事项1"],
  "confidence": 0.0-1.0
}}

要求：
1. steps 必须 ≥ {min_steps} 步，宁缺毋滥
2. 只保留可复用的通用步骤，去掉一次性细节和敏感信息
3. skill_name 用英文小写连字符
4. 只输出 JSON 对象"""


def _ensure_draft_status_supported() -> None:
    """确保 skill_crystals 表存在（复用 v17 结晶器 DDL）。"""
    from ducky.skill_crystallizer import init_crystallizer_schema
    init_crystallizer_schema()


def _parse_skill_json(raw: str) -> Optional[dict]:
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


def _format_skill_md(skill: dict) -> str:
    """把 skill 草稿格式化为 SKILL.md 文本（用于归档/推送）。"""
    name = str(skill.get("skill_name") or "new-skill").strip().lower()
    trigger = str(skill.get("trigger") or "").strip()
    steps = [str(s).strip() for s in (skill.get("steps") or []) if str(s).strip()]
    cautions = [str(c).strip() for c in (skill.get("cautions") or []) if str(c).strip()]

    lines = [
        "---",
        f"name: {name}",
        "description: >-",
        f"  {trigger or '从任务轨迹自动提取的技能草稿'}",
        "---",
        "",
        "# " + (trigger or name),
        "",
        "## 步骤",
    ]
    lines += [f"{i}. {s}" for i, s in enumerate(steps, 1)]
    if cautions:
        lines.append("")
        lines.append("## 注意事项")
        lines += [f"- {c}" for c in cautions]
    lines.append("")
    return "\n".join(lines)


def grow_skill_from_trajectory(
    trajectory: list[str] | str,
    *,
    task_name: str = "",
    use_llm: bool = True,
    source: str = "manual",
) -> dict:
    """从一次任务轨迹生成技能草稿（不自动 commit）。

    Args:
        trajectory: 任务步骤列表，或一个多行字符串（按行拆分）
        task_name: 可选任务名，会写入 source 备注
        use_llm: 是否调用 LLM 生成结构化草稿；False 时用规则摘要
        source: 触发来源（manual / session_end / cron）
    """
    _ensure_draft_status_supported()

    if isinstance(trajectory, str):
        steps = [s.strip() for s in trajectory.splitlines() if s.strip()]
    elif isinstance(trajectory, list):
        steps = [str(s).strip() for s in trajectory if str(s).strip()]
    else:
        steps = []

    if len(steps) < _MIN_STEPS:
        return {
            "status": "skipped",
            "reason": f"有效步骤不足（{len(steps)} < {_MIN_STEPS}），不值得生成技能",
            "steps": steps,
        }

    skill = None
    llm_used = False
    if use_llm and SKILL_GROWTH_ENABLED:
        try:
            from ducky.llm_client import call_llm
            raw = call_llm(
                _SKILL_USER_TEMPLATE.format(
                    trajectory="\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)),
                    min_steps=_MIN_STEPS,
                ),
                system=_SKILL_SYSTEM,
                max_tokens=600,
                temperature=0.3,
            )
            skill = _parse_skill_json(raw or "")
            llm_used = skill is not None
        except Exception as e:
            logger.debug(f"LLM 技能草稿生成失败（降级规则）: {e}")

    if skill is None:
        # 规则降级：任务名 + 前若干步骤
        skill = {
            "skill_name": (task_name or "task-skill").strip().lower()[:40] or "task-skill",
            "trigger": f"当需要处理「{task_name or '类似任务'}」时触发",
            "steps": steps[:_MIN_STEPS + 4],
            "cautions": ["本草稿由规则降级生成，需人工审核"],
            "confidence": 0.4,
        }

    raw_name = str(skill.get("skill_name") or "task-skill").strip()
    # 中文/非 ASCII 任务名：零依赖方案——保留 ASCII 片段，若无 ASCII
    # 字符则用 md5 前 8 位做稳定标识，避免不同中文名被 re.sub 全部
    # 坍缩成 '-' 而相互覆盖（ON CONFLICT(skill_name) 会静默吞掉草稿）。
    ascii_part = "".join(ch for ch in raw_name if ch.isascii() and (ch.isalnum() or ch == "-"))
    if re.search(r"[一-鿿]", raw_name) and not ascii_part.strip("-"):
        ascii_part = "skill-" + hashlib.md5(raw_name.encode()).hexdigest()[:8]
    skill_name = re.sub(r"[^a-z0-9-]+", "-", (ascii_part or raw_name).lower())[:48]
    if not re.search(r"[a-z0-9]", skill_name):
        skill_name = f"skill-{hashlib.md5(str(skill.get('skill_name') or 'task').encode()).hexdigest()[:8]}"
    skill_md = _format_skill_md({**skill, "skill_name": skill_name})
    confidence = 0.5
    try:
        confidence = max(0.0, min(1.0, float(skill.get("confidence", 0.5))))
    except (TypeError, ValueError):
        pass

    conn = get_facts_conn()
    try:
        # 治理铁律：LLM 只能建议 draft，不能覆盖人工已 approved/candidate 的技能。
        # 同名技能若已脱离 draft 态（approved/archived/candidate），本次草稿跳过。
        existing = conn.execute(
            "SELECT crystal_id, status FROM skill_crystals WHERE skill_name=?", (skill_name,)
        ).fetchone()
        if existing and str(existing["status"]) != "draft":
            conn.close()
            logger.warning(
                "🌱 skill_growth: 同名技能 '%s' 已处于 %s 态，跳过草稿覆盖（crystal_id=%s）",
                skill_name, existing["status"], existing["crystal_id"],
            )
            return {
                "status": "skipped",
                "reason": f"同名技能已存在且状态为 {existing['status']}，不覆盖（需人工处理）",
                "skill_name": skill_name,
                "existing_crystal_id": existing["crystal_id"],
            }

        conn.execute(
            """
            INSERT INTO skill_crystals
                (skill_name, trigger_rule, procedure, source_categories, sample_keys,
                 hit_count, candidate_count, status)
            VALUES (?, ?, ?, ?, ?, 1, ?, 'draft')
            ON CONFLICT(skill_name) DO UPDATE SET
                trigger_rule = excluded.trigger_rule,
                procedure    = excluded.procedure,
                sample_keys  = excluded.sample_keys,
                candidate_count = excluded.candidate_count,
                status       = 'draft',
                updated_at   = CURRENT_TIMESTAMP
            """,
            (
                skill_name,
                str(skill.get("trigger") or ""),
                skill_md,
                source,
                json.dumps(steps[:_MIN_STEPS], ensure_ascii=False),
                len(steps),
            ),
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"技能草稿落库失败: {e}")
        return {"status": "error", "detail": str(e)}
    finally:
        conn.close()

    logger.info("🌱 skill_growth: 生成草稿 '%s'（%d 步，llm=%s）", skill_name, len(steps), llm_used)
    return {
        "status": "ok",
        "skill_name": skill_name,
        "skill_md": skill_md,
        "steps_count": len(steps),
        "confidence": confidence,
        "llm_used": llm_used,
        "state": "draft",
    }


def list_skill_drafts(status: str = "draft") -> list[dict]:
    """列出技能草稿（复用 v17 list_crystals）。"""
    from ducky.skill_crystallizer import list_crystals
    return list_crystals(status=status)
