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
    - 强制 reasoning_effort=none（防上游默认开思考）
    只打一次。
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
        force_effort = speed.get("force_reasoning_effort", "none")
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
            # 强制关思考
            if force_effort and "reasoning_effort" not in kwargs:
                kwargs["reasoning_effort"] = force_effort
            elif force_effort:
                kwargs["reasoning_effort"] = force_effort
            # reasoning 路径 SDK 可能不带 max_tokens；这里强制补上
            if force_max and "max_tokens" not in kwargs and "max_completion_tokens" not in kwargs:
                kwargs["max_tokens"] = max_tokens
            return _orig(*args, **kwargs)

        client.chat.completions.create = _wrapped
        setattr(mem_instance, "_aidumem_speed_patched", True)
        logger.info(f"✅ speed LLM patch: max_tokens={max_tokens} effort={force_effort}")
    except Exception as e:
        logger.warning(f"speed LLM patch skip: {e}")
