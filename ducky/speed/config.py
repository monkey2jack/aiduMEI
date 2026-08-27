"""aiduMEM speed · 配置与文本工具（Mnemosyne）"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

from ducky.utils import BASE_DIR as _BASE_DIR

logger = logging.getLogger("aiduMEM.speed")

from ducky.utils import mem0_config_path as _mcp
_CFG_PATH = _mcp()   # v20.2.4 F-22

_DEFAULT_SPEED = {
    "extract_cache_ttl_sec": 3600,
    "extract_cache_max": 256,
    "async_default": False,
    "coalesce_enabled": True,
    "coalesce_window_sec": 12,      # default：首条起最长等待
    "coalesce_idle_sec": 4,         # default：空闲这么久就冲刷
    "coalesce_max_parts": 8,
    "coalesce_max_chars": 2000,
    "coalesce_max_single_chars": 500,
    "coalesce_flush_tick_sec": 0.5,
    # 双策略（日记/亲密 vs 技术碎句）— 可被 _speed.coalesce_profiles 覆盖
    "coalesce_default_profile": "default",
    "coalesce_profiles": {
        "default": {
            "window_sec": 12,
            "idle_sec": 4,
            "max_parts": 8,
            "max_chars": 2000,
            "max_single_chars": 500,
        },
        "tech": {
            "window_sec": 8,
            "idle_sec": 2.5,
            "max_parts": 6,
            "max_chars": 1500,
            "max_single_chars": 400,
        },
        "intimate": {
            "window_sec": 20,
            "idle_sec": 8,
            "max_parts": 12,
            "max_chars": 3000,
            "max_single_chars": 800,
        },
    },
    "coalesce_profile_by_source": {
        "treasure": "intimate",
        "diary": "intimate",
        "goodnight": "intimate",
        "intimacy": "intimate",
        "intimate": "intimate",
        "love": "intimate",
        "affection": "intimate",
        "romance": "intimate",
        "personal": "intimate",
        "hermes": "tech",
        "hermes_memory": "tech",
        "mem0_sync": "tech",
        "auto_memory": "tech",
        "memory_md": "tech",
        "code": "tech",
        "tech": "tech",
        "debug": "tech",
        "deploy": "tech",
        "speed_test": "tech",
        "chat": "default",
    },
    "coalesce_profile_by_category": {
        "diary": "intimate",
        "日记": "intimate",
        "treasure": "intimate",
        "intimacy": "intimate",
        "intimate": "intimate",
        "love": "intimate",
        "goodnight": "intimate",
        "hermes_memory": "tech",
        "tech": "tech",
        "code": "tech",
        "speed_test": "tech",
        "debug": "tech",
    },
    "capacity_merge_async": True,
    "fastpath_enabled": True,
    "long_text_chars": 2500,
    "force_max_tokens_on_reasoning": True,
    # v20 · P1-4：默认**不设**。原先默认 "none"，于是每个部署都在无声地往请求里
    # 塞一个 reasoning_effort=none —— 而 v19.4.0 生产实测已经写明（见
    # ducky/llm_client.py 的 🔴-B 注释）：**上游网关无视请求级
    # reasoning_effort/enable_thinking**。塞进去不生效，日志却打 ✅，这就是
    # 「设了没用但报成功」的第三态。
    #
    # 为什么不是直接把这个键删掉：开源用户可能把 base_url 指向**别的**供应商
    # （OpenAI o 系列就认这个字段）。所以保留能力、去掉默认：显式设了才发，
    # 没设就一个字都不提 —— 不替上游承诺任何效果，也不替用户做决定。
    "force_reasoning_effort": None,
}

_speed_cfg_cache: Optional[dict] = None
_speed_cfg_mtime: float = 0.0


def load_speed_cfg() -> dict:
    """读取 _speed 配置（带 mtime 缓存）。"""
    global _speed_cfg_cache, _speed_cfg_mtime
    try:
        mtime = os.path.getmtime(_CFG_PATH)
        if _speed_cfg_cache is not None and mtime == _speed_cfg_mtime:
            return _speed_cfg_cache
        with open(_CFG_PATH) as f:
            cfg = json.load(f)
        speed = dict(_DEFAULT_SPEED)
        speed.update(cfg.get("_speed") or {})
        _speed_cfg_cache = speed
        _speed_cfg_mtime = mtime
        return speed
    except Exception as e:
        logger.debug(f"speed cfg load skip: {e}")
        return dict(_DEFAULT_SPEED)


def messages_to_text(messages_json) -> str:
    if isinstance(messages_json, list):
        return " ".join(
            str(m.get("content", "")) for m in messages_json if isinstance(m, dict)
        ).strip()
    if isinstance(messages_json, dict):
        return str(messages_json.get("content", messages_json)).strip()
    return str(messages_json or "").strip()
