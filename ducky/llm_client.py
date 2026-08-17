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


def _extract_content(data: dict) -> Optional[str]:
    """从一个 chat/completions 响应对象里提取 assistant 文本。

    兼容非流式（choices[0].message.content）与流式块（choices[0].delta.content）。
    """
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return None
    choice = choices[0]
    message = choice.get("message") or choice.get("delta") or {}
    content = message.get("content") if isinstance(message, dict) else None
    if content:
        return str(content).strip()
    return None


def _parse_completion_body(text: str) -> Optional[str]:
    """解析 chat/completions 响应体（v19.4.0 · 生产审计 🔴-B）。

    上游网关实测会返回 Content-Type: text/event-stream，body 却是
    「完整 JSON + data: [DONE]」拼接体，r.json() 直接抛异常——
    v19.4.0 评估器因此永远记 evaluator_unavailable。兜底策略：

      1. 标准 JSON → 直接提取
      2. 拼接体 / 真 SSE 流 → 逐行剥 `data: ` 前缀、跳过 [DONE]：
         · 出现完整响应对象 → 取 message.content
         · 全是 delta 块    → 拼接 delta.content
    解析不出内容返回 None（调用方降级），绝不抛异常。
    """
    text = (text or "").strip()
    if not text:
        return None
    # 1) 标准 JSON 直通
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return _extract_content(data)
    except Exception:
        pass
    # 2) SSE / 拼接体逐行兜底
    full_parts: list[str] = []
    delta_chunks: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("data:"):
            line = line[len("data:"):].strip()
        if not line or line == "[DONE]":
            continue
        try:
            data = json.loads(line)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        content = _extract_content(data)
        if content:
            choices = data.get("choices") or [{}]
            is_delta = isinstance(choices[0], dict) and "delta" in choices[0]
            (delta_chunks if is_delta else full_parts).append(content)
    if full_parts:
        return "\n".join(full_parts).strip() or None
    if delta_chunks:
        return "".join(delta_chunks).strip() or None
    return None


def _post_completion(endpoint: str, api_key: str, model: str, messages: list,
                     max_tokens: int, temperature: float, timeout: int) -> tuple[Optional[str], bool]:
    """发一次 chat/completions，返回 (content, 推理截断标志)。

    推理截断 = HTTP 200 但 content 为空、finish_reason=length 且响应带
    reasoning_content——推理模型把全部预算耗在思考上，没来得及输出正文。
    """
    r = requests.post(
        endpoint,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            # 🔴-B：显式要求非流式。部分网关无视该字段仍回 SSE，
            # 响应体交给 _parse_completion_body 兜底解析。
            "stream": False,
        },
        timeout=timeout,
    )
    if r.status_code != 200:
        logger.warning(f"LLM 直接调用失败: HTTP {r.status_code} {r.text[:200]}")
        return None, False
    content = _parse_completion_body(r.text)
    if content:
        return content, False
    # 探测「推理截断」：content 空 + finish_reason=length + 有 reasoning_content
    try:
        data = json.loads(r.text.strip().splitlines()[0].removeprefix("data:").strip())
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        if choice.get("finish_reason") == "length" and msg.get("reasoning_content"):
            return None, True
    except Exception:
        pass
    logger.warning("LLM 直接调用: HTTP 200 但响应体解析不出内容: %s", r.text[:200])
    return None, False


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

    🔴-B 根治（v19.4.0 生产实测补强）：上游网关的推理模型，
    请求级 reasoning_effort/enable_thinking 均被网关无视；小预算下思考耗尽
    预算 → content 空 + finish_reason=length。检测到该形态自动放大预算重试一次。
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
        content, reasoning_truncated = _post_completion(
            endpoint, cfg["api_key"], cfg["model"], messages,
            max_tokens, temperature, timeout)
        if content:
            return content
        if reasoning_truncated:
            retry_budget = min(max_tokens * 4, 4096)
            logger.info("LLM 推理截断（思考耗尽预算），放大预算重试: %d → %d",
                        max_tokens, retry_budget)
            content, _ = _post_completion(
                endpoint, cfg["api_key"], cfg["model"], messages,
                retry_budget, temperature, timeout)
            if content:
                return content
        return None
    except Exception as e:
        logger.warning(f"LLM 直接调用异常: {e}")
        return None
