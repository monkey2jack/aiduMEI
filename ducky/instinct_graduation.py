#!/usr/bin/env python3
"""
aiduMEM Instinct Graduation: Instinct→Skill 自动毕业模块
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Aion Memory 设计哲学：
- 同域 ≥3 条 instinct 自动升格为 skill
- LLM 蒸馏 → 删除原始 → 保留精炼版本
- 元数据追踪 source_ids 溯源链
"""

import logging, time
from typing import Optional

from ducky.bank_contract import (
    DEFAULT_BANK_ID,
    make_scope,
    stamp_bank_metadata,
    vector_item_in_bank,
    vector_scope_filters,
)

logger = logging.getLogger("aiduMEM.graduation")

# ── 配置 ──
MIN_GROUP_SIZE = 3          # 触发毕业的最小同组条数
GRADUATION_PROMPT = """你是一个记忆蒸馏专家。以下是一组关于「{category}」的记忆碎片：

{memories}

请将这些记忆归纳为一条精炼的知识，要求：
1. 保留所有关键事实和信息
2. 消除冗余和重复
3. 用简洁的中文表达
4. 只输出归纳后的文本，不要加任何前缀或说明"""


def _call_llm(prompt: str, max_tokens: int | None = None) -> Optional[str]:
    """蒸馏用的 LLM 调用 —— 转交 `ducky.llm_client.call_llm`（v20 · P1-5 根因整改）。

    这里原先自己手搓了一份 `requests.post` 直发 OpenAI 兼容补全端点，
    连带一份自己的配置读取和一个写死的 512 预算。它和 `llm_client` 读的是**同一份**
    `mem0_config_local.json`、**同一条**密钥回退链（`__SF_KEY__` → `.llm_key`
    → `.sensenova_key`），唯一的实质差别是：**它没有推理截断重试**。

    于是推理模型下就成了这样一条无声链路：思考吃光 512 预算 → `content` 回空串
    → `.strip()` 还是空串 → HTTP 仍是 200，所以一行日志都不打 → 上游
    `if not distilled: return None`（本文件 :174 附近）当成「没什么可毕业的」
    → 整条毕业静默不发生。这不是「调用失败」，是**假绿灯**：绿灯亮着，活没干。

    转交之后，截断检测和 ×4 放大重试直接继承（见 `llm_client._post_completion`
    的 `reasoning_truncated`），预算也不再由本文件私藏 —— 统一取
    `COGNITIVE_MAX_TOKENS`。

    函数名和签名故意保持不变：`tests/test_v20_graduation_persona_bank_scope.py`
    有三处 `monkeypatch.setattr(graduation, "_call_llm", …)` 挂在这个名字上，
    改名会让那三条用例挂到一个空气上还照样变绿。
    """
    from ducky.llm_client import COGNITIVE_MAX_TOKENS, call_llm
    return call_llm(prompt, max_tokens=max_tokens or COGNITIVE_MAX_TOKENS)


def _extract_category(memory: dict) -> str:
    """从记忆元数据中提取分类"""
    meta = memory.get("metadata") or {}
    return meta.get("category", meta.get("source", "general"))


def scan_instincts(memory, user_id: str, bank_id: str = DEFAULT_BANK_ID) -> list[dict]:
    """扫描可毕业的记忆组（v20 P0-2：按 (user_id, bank_id) 圈定，毕业不跨库）"""
    # 非法作用域在取数前就抛，不许静默降级成全库扫描
    scope = make_scope(user_id, bank_id)
    try:
        all_mem = memory.get_all(
            filters=vector_scope_filters(scope.user_id, scope.bank_id), limit=10000
        )
        results = all_mem.get("results", all_mem) if isinstance(all_mem, dict) else all_mem
        if not isinstance(results, list):
            return []

        # 按 category 分组
        groups = {}
        for item in results:
            if not isinstance(item, dict):
                continue
            # 复筛：默认域不下推 bank_id，具名域的点不许漏进毕业候选
            if not vector_item_in_bank(item, scope.bank_id):
                continue
            cat = _extract_category(item)
            if cat not in groups:
                groups[cat] = []
            groups[cat].append(item)

        # 筛选 ≥MIN_GROUP_SIZE 的组
        candidates = []
        for cat, items in groups.items():
            if len(items) >= MIN_GROUP_SIZE:
                candidates.append({
                    "category": cat,
                    "count": len(items),
                    "sample_ids": [it["id"][:16] for it in items[:5]],
                })
        return candidates
    except Exception as e:
        logger.warning(f"扫描 instinct 失败: {e}")
        return []


def graduate_to_skill(memory, user_id: str, group: dict,
                      bank_id: str = DEFAULT_BANK_ID) -> Optional[str]:
    """将一组记忆蒸馏为 skill（v20 P0-2：蒸馏与删除都锁死在本域内）"""
    scope = make_scope(user_id, bank_id)
    try:
        # 获取完整记忆
        all_mem = memory.get_all(
            filters=vector_scope_filters(scope.user_id, scope.bank_id), limit=10000
        )
        results = all_mem.get("results", all_mem) if isinstance(all_mem, dict) else all_mem
        if not isinstance(results, list):
            return None

        cat = group["category"]
        cat_memories = [
            it for it in results
            if isinstance(it, dict)
            and vector_item_in_bank(it, scope.bank_id)
            and _extract_category(it) == cat
        ]
        if len(cat_memories) < MIN_GROUP_SIZE:
            return None

        # 构建 prompt
        memory_texts = []
        source_ids = []
        for item in cat_memories[:10]:  # 最多10条
            text = item.get("memory", "")
            memory_texts.append(f"- {text}")
            source_ids.append(item["id"])

        prompt = GRADUATION_PROMPT.format(
            category=cat,
            memories="\n".join(memory_texts)
        )

        # LLM 蒸馏
        distilled = _call_llm(prompt)
        if not distilled:
            # P1-5：这里过去是一句光秃秃的 return None —— 「LLM 回了空」和
            # 「本来就没什么可毕业」在日志里长得一模一样。铁律 8 那句问话
            # 「如果这里真失败了，谁会知道？」在这条路径上原本没有答案。
            logger.warning(
                "Instinct 毕业中止：蒸馏返回空（category=%s, 待毕业 %d 条）。"
                "推理模型预算被思考吃光时就是这个形态，看 llm_client 的截断重试日志",
                cat, len(source_ids))
            return None

        # 写入新记忆
        messages = [{"role": "assistant", "content": distilled}]
        metadata = stamp_bank_metadata({
            "level": "skill",
            "category": cat,
            "source": "instinct_graduation",
            "source_ids": source_ids,
            "graduated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, scope.bank_id)
        from ducky.gear import should_try_llm
        memory.add(messages, user_id=scope.user_id, metadata=metadata, infer=should_try_llm())

        # 删除原始记忆
        deleted = 0
        for sid in source_ids:
            try:
                memory.delete(sid)
                deleted += 1
            except Exception as e:
                logger.debug(f"删除原始记忆失败 {sid[:8]}: {e}")

        logger.info(f"Instinct 毕业: {cat} ({len(source_ids)}→1 skill, 删除{deleted})")
        return distilled[:100]
    except Exception as e:
        logger.warning(f"毕业失败: {e}")
        return None


def auto_graduate(memory, user_id: str, min_group_size: int = MIN_GROUP_SIZE,
                  bank_id: str = DEFAULT_BANK_ID) -> dict:
    """自动扫描并毕业所有符合条件的记忆组（v20 P0-2：整条链锁在本域）"""
    scope = make_scope(user_id, bank_id)
    groups = scan_instincts(memory, scope.user_id, scope.bank_id)
    graduated = []
    deleted_total = 0

    for group in groups:
        if group["count"] < min_group_size:
            continue
        skill = graduate_to_skill(memory, scope.user_id, group, scope.bank_id)
        if skill:
            graduated.append({"category": group["category"], "preview": skill[:80]})
            deleted_total += group["count"]

    return {
        "graduated_groups": len(graduated),
        "new_skills": graduated,
        "deleted": deleted_total,
    }
