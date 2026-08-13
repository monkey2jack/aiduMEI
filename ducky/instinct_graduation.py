#!/usr/bin/env python3
"""
aiduMEM Instinct Graduation: Instinct→Skill 自动毕业模块
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Aion Memory 设计哲学：
- 同域 ≥3 条 instinct 自动升格为 skill
- LLM 蒸馏 → 删除原始 → 保留精炼版本
- 元数据追踪 source_ids 溯源链
"""

import json, logging, os, base64, time
from typing import Optional
import requests

from ducky.utils import BASE_DIR as _BASE_DIR

logger = logging.getLogger("aiduMEM.graduation")

# ── 配置 ──
MIN_GROUP_SIZE = 3          # 触发毕业的最小同组条数
LLM_CONFIG_FILE = os.path.join(_BASE_DIR, "mem0_config_local.json")
GRADUATION_PROMPT = """你是一个记忆蒸馏专家。以下是一组关于「{category}」的记忆碎片：

{memories}

请将这些记忆归纳为一条精炼的知识，要求：
1. 保留所有关键事实和信息
2. 消除冗余和重复
3. 用简洁的中文表达
4. 只输出归纳后的文本，不要加任何前缀或说明"""


def _get_llm_config() -> dict:
    """读取 LLM 配置"""
    try:
        with open(LLM_CONFIG_FILE) as f:
            cfg = json.load(f)
        llm_cfg = cfg.get("llm", {}).get("config", {})
        api_key = llm_cfg.get("api_key", "")
        if api_key == "__SF_KEY__" or not api_key:
            for kf in [os.path.join(_BASE_DIR, ".llm_key"),
                       os.path.join(_BASE_DIR, ".sensenova_key")]:
                if os.path.exists(kf):
                    with open(kf) as f:
                        api_key = f.read().strip()
                        break
        return {
            "model": llm_cfg.get("model", "flash-lite"),
            "base_url": llm_cfg.get("openai_base_url", "https://provider.example.cn/v1"),
            "api_key": api_key,
        }
    except Exception as e:
        logger.error(f"LLM配置读取失败: {e}")
        return {}


def _call_llm(prompt: str, max_tokens: int = 512) -> Optional[str]:
    """调用 LLM"""
    cfg = _get_llm_config()
    if not cfg.get("api_key"):
        logger.error("LLM API key 不可用")
        return None
    try:
        r = requests.post(
            f"{cfg['base_url'].rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
            json={
                "model": cfg["model"],
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0.3,
            },
            timeout=30
        )
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"].strip()
        logger.warning(f"LLM 调用失败: HTTP {r.status_code}")
        return None
    except Exception as e:
        logger.warning(f"LLM 调用异常: {e}")
        return None


def _extract_category(memory: dict) -> str:
    """从记忆元数据中提取分类"""
    meta = memory.get("metadata") or {}
    return meta.get("category", meta.get("source", "general"))


def scan_instincts(memory, user_id: str) -> list[dict]:
    """扫描可毕业的记忆组"""
    try:
        all_mem = memory.get_all(filters={"user_id": user_id}, limit=10000)
        results = all_mem.get("results", all_mem) if isinstance(all_mem, dict) else all_mem
        if not isinstance(results, list):
            return []

        # 按 category 分组
        groups = {}
        for item in results:
            if not isinstance(item, dict):
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


def graduate_to_skill(memory, user_id: str, group: dict) -> Optional[str]:
    """将一组记忆蒸馏为 skill"""
    try:
        # 获取完整记忆
        all_mem = memory.get_all(filters={"user_id": user_id}, limit=10000)
        results = all_mem.get("results", all_mem) if isinstance(all_mem, dict) else all_mem
        if not isinstance(results, list):
            return None

        cat = group["category"]
        cat_memories = [it for it in results if _extract_category(it) == cat]
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
            return None

        # 写入新记忆
        messages = [{"role": "assistant", "content": distilled}]
        metadata = {
            "level": "skill",
            "category": cat,
            "source": "instinct_graduation",
            "source_ids": source_ids,
            "graduated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        memory.add(messages, user_id=user_id, metadata=metadata)

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


def auto_graduate(memory, user_id: str, min_group_size: int = MIN_GROUP_SIZE) -> dict:
    """自动扫描并毕业所有符合条件的记忆组"""
    groups = scan_instincts(memory, user_id)
    graduated = []
    deleted_total = 0

    for group in groups:
        if group["count"] < min_group_size:
            continue
        skill = graduate_to_skill(memory, user_id, group)
        if skill:
            graduated.append({"category": group["category"], "preview": skill[:80]})
            deleted_total += group["count"]

    return {
        "graduated_groups": len(graduated),
        "new_skills": graduated,
        "deleted": deleted_total,
    }
