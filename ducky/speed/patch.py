"""aiduMEM speed · LLM 请求补丁"""
from __future__ import annotations

import json
import logging

from ducky.speed.config import _CFG_PATH, load_speed_cfg

logger = logging.getLogger("aiduMEM.speed")


def patch_llm_for_speed(mem_instance) -> None:
    """
    给 mem0 的 OpenAI client 打补丁：
    - reasoning 模型路径也强制带 max_tokens（SDK 默认会丢掉）
    - 若部署方**显式**配置了 `_speed.force_reasoning_effort`，按配置注入该字段
    只打一次。

    v20 · P1-4：这里原先无条件注入 `reasoning_effort="none"`，且注入代码是
    `if force_effort and "reasoning_effort" not in kwargs: … elif force_effort: …`
    —— 两个分支做的是同一件事，是一段谁都没读懂的死分支。更要紧的是那个默认值：
    上游网关**无视**请求级 `reasoning_effort`（v19.4.0 生产实测，见
    `ducky/llm_client.py` 的 🔴-B 注释），于是它塞了、没生效、日志还打 ✅。
    现在：没配就不注入、日志也不提；配了就注入，日志只敢说「已按配置发送」，
    不敢说「已生效」—— 请求侧压根判定不了上游采不采纳。
    """
    if getattr(mem_instance, "_aidumem_speed_patched", False):
        return
    try:
        from openai import OpenAI

        # mem0 OpenAI LLM client 通常在 mem_instance.llm.client
        llm = getattr(mem_instance, "llm", None)
        client = getattr(llm, "client", None) if llm is not None else None
        if client is None:
            client = getattr(mem_instance, "client", None)
        if client is None or not hasattr(client, "chat"):
            logger.warning("speed patch: no chat client found")
            return

        speed = load_speed_cfg()
        # 默认 None = 不注入。绝不在这里兜一个 "none" 回来（那正是 P1-4 的形态）
        force_effort = speed.get("force_reasoning_effort") or None
        force_max = bool(speed.get("force_max_tokens_on_reasoning", True))

        # 从 config 读 max_tokens
        max_tokens = 2048
        try:
            with open(_CFG_PATH) as f:
                cfg = json.load(f)
            max_tokens = int(cfg.get("llm", {}).get("config", {}).get("max_tokens", 2048))
        except Exception as e:
            logger.debug(f"patch_llm_for_speed: suppressed exception: {e}")

        _orig = client.chat.completions.create

        def _wrapped(*args, **kwargs):
            # 只有部署方显式配了才注入。原先的 if/elif 两个分支做的是同一件事，
            # 等价于这一行 —— 死分支删掉，别让下一个人以为那里有什么讲究。
            if force_effort:
                kwargs["reasoning_effort"] = force_effort
            # reasoning 路径 SDK 可能不带 max_tokens；这里强制补上
            if force_max and "max_tokens" not in kwargs and "max_completion_tokens" not in kwargs:
                kwargs["max_tokens"] = max_tokens
            return _orig(*args, **kwargs)

        client.chat.completions.create = _wrapped
        setattr(mem_instance, "_aidumem_speed_patched", True)
        # 日志只报**确实做了的事**。effort 没配就不提它 —— 报一个没发生的动作，
        # 和报一个没生效的动作，是同一种「宣称即承诺」违规。
        if force_effort:
            logger.info(
                "✅ speed LLM patch: max_tokens=%s，已按配置发送 reasoning_effort=%s"
                "（上游是否采纳无法从请求侧判定；我们的网关实测忽略该字段）",
                max_tokens, force_effort)
        else:
            logger.info("✅ speed LLM patch: max_tokens=%s（未配置 reasoning_effort，不注入）",
                        max_tokens)
    except Exception as e:
        logger.warning(f"speed LLM patch skip: {e}")
