"""
ducky.llm_client — 轻量共享 LLM 调用助手（v19.0）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Reflect / 记忆去重自编辑 等认知层模块需要一个「不进 mem0 抽取管道」的
直接 LLM 调用通道：读 mem0_config_local.json 里的 llm 配置，复用
与 mem0_runtime 完全相同的密钥解析规则（__SF_KEY__ / __LLM_KEY__ /
key 文件回退），通过 requests 直发 OpenAI 兼容 chat/completions。

铁律：密钥永远从占位符文件解析，不在源码里硬编码。
失败一律返回 None，由调用方降级，不阻断主链路。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Optional

import requests

from ducky.utils import BASE_DIR

logger = logging.getLogger("aiduMEM.llm_client")

MEM0_CONFIG = os.path.join(BASE_DIR, "mem0_config_local.json")

# 密钥占位符 → 对应 key 文件（顺序即回退顺序）
_KEY_FALLBACKS = {
    "llm": [os.path.join(BASE_DIR, ".llm_key"), os.path.join(BASE_DIR, ".sensenova_key")],
    "embedding": [os.path.join(BASE_DIR, ".sf_key")],
}

_config_cache: Optional[dict] = None
_config_lock = threading.Lock()


def get_llm_config() -> dict:
    """读取 mem0_config 的 llm 段，解析密钥占位符。结果缓存，进程内只读一次。

    失败不缓存：配置文件缺失/损坏时返回空配置，但不会把空配置写进缓存，
    下一次调用会重新尝试读取（配置修复后无需重启进程即可恢复）。
    """
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    with _config_lock:
        # 双检：等锁期间可能已被并发调用方填充
        if _config_cache is not None:
            return _config_cache

        cfg: dict = {"model": "", "base_url": "", "api_key": ""}
        try:
            if not os.path.exists(MEM0_CONFIG):
                logger.warning("mem0_config_local.json 不存在，LLM 配置为空")
                return cfg

            with open(MEM0_CONFIG, encoding="utf-8") as f:
                raw = json.load(f)

            llm_cfg = raw.get("llm", {}).get("config", {}) or {}
            cfg["model"] = llm_cfg.get("model", "")
            cfg["base_url"] = llm_cfg.get("openai_base_url", "")

            api_key = llm_cfg.get("api_key", "")
            cfg["api_key"] = _resolve_key(api_key, "llm")
            _config_cache = cfg
        except Exception as e:
            logger.warning(f"读取 LLM 配置失败（下次调用重试）: {e}")
        return cfg


def _resolve_key(api_key: str, purpose: str) -> str:
    """把占位符解析成真实密钥；已是真实 key 则原样返回。"""
    placeholders = {"__SF_KEY__", "__LLM_KEY__", "__EMBED_KEY__"}
    if api_key and api_key not in placeholders:
        return api_key

    for key_file in _KEY_FALLBACKS.get(purpose, []):
        if os.path.exists(key_file):
            try:
                with open(key_file, encoding="utf-8") as f:
                    resolved = f.read().strip()
                if resolved:
                    return resolved
            except OSError:
                continue
    return api_key if api_key not in placeholders else ""


def call_llm(
    prompt: str,
    *,
    system: str = "",
    max_tokens: int = 1024,
    temperature: float = 0.3,
    timeout: int = 45,
) -> Optional[str]:
    """
    直接调用配置好的 LLM，返回 assistant 文本；失败返回 None（调用方降级）。

    Args:
        prompt: 用户消息内容
        system: 可选 system 提示
        max_tokens: 输出上限
        temperature: 采样温度（认知类任务用低温度求稳定）
        timeout: 请求超时秒数
    """
    cfg = get_llm_config()
    if not cfg.get("api_key") or not cfg.get("model"):
        logger.debug("LLM 未配置，跳过直接调用")
        return None

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    base = (cfg.get("base_url") or "").rstrip("/")
    if not base:
        return None

    # 兼容三种配置形态：纯域名、带 /v1 前缀、已指向 /chat/completions
    if base.endswith("/chat/completions"):
        endpoint = base
    else:
        endpoint = f"{base}/chat/completions"

    try:
        r = requests.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {cfg['api_key']}",
                "Content-Type": "application/json",
            },
            json={
                "model": cfg["model"],
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
            },
            timeout=timeout,
        )
        if r.status_code == 200:
            data = r.json()
            choices = data.get("choices") or []
            if choices:
                return (choices[0].get("message") or {}).get("content", "").strip()
            return None
        logger.warning(f"LLM 直接调用失败: HTTP {r.status_code} {r.text[:200]}")
        return None
    except Exception as e:
        logger.warning(f"LLM 直接调用异常: {e}")
        return None
