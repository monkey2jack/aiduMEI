#!/usr/bin/env python3
"""
aiduMEM Tool Envelope — 统一 MCP 工具返回契约
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Osaurus Plugin v6 ToolEnvelope 灵感：
- 成功: {result, warnings}  
- 失败: {kind, message, retryable}

本模块提供装饰器和包装函数，统一 aiduMEM 所有 MCP 工具返回格式。
"""

import json, logging, traceback
from typing import Any

logger = logging.getLogger("aiduMEM.envelope")

# ── 标准错误类型（对齐 Osaurus Plugin v6） ──
ERROR_KINDS = {
    "invalid_args":       {"retryable": False, "desc": "参数格式错误"},
    "rejected":           {"retryable": False, "desc": "请求被策略拒绝"},
    "user_denied":        {"retryable": False, "desc": "用户拒绝操作"},
    "timeout":            {"retryable": True,  "desc": "操作超时"},
    "execution_error":    {"retryable": True,  "desc": "执行异常"},
    "unavailable":        {"retryable": True,  "desc": "服务不可用"},
    "tool_not_found":     {"retryable": False, "desc": "工具不存在"},
    "not_found":          {"retryable": False, "desc": "资源不存在"},
    "rate_limit_exceeded": {"retryable": True, "desc": "限流"},
}


def success(result: Any = None, warnings: Any = None) -> dict:
    """构建成功 Envelope（result 嵌套形态，适合 MCP/工具层）"""
    env: dict = {"status": "ok"}
    if result is not None:
        env["result"] = result
    if warnings:
        env["warnings"] = warnings
    return env


def ok(**fields: Any) -> dict:
    """扁平成功 Envelope：{status: ok, ...fields}

    REST 主链路（/health、session、facts…）优先用这个，避免把已有字段塞进 result。
    """
    env: dict = {"status": "ok"}
    env.update(fields)
    return env


def error(kind: str, message: str, detail: Any = None, **extra: Any) -> dict:
    """构建失败 Envelope"""
    if kind not in ERROR_KINDS:
        logger.warning(f"未知错误类型: {kind}，回退到 execution_error")
        kind = "execution_error"

    env = {
        "status": "error",
        "kind": kind,
        "message": message,
        "retryable": ERROR_KINDS[kind]["retryable"],
    }
    if detail is not None:
        env["detail"] = detail
    if extra:
        env.update(extra)
    return env


def wrap(func):
    """装饰器：自动包装异常为标准 Envelope"""
    def wrapper(*args, **kwargs):
        try:
            result = func(*args, **kwargs)
            # 如果已经是 Envelope 格式，直接返回
            if isinstance(result, dict) and "status" in result:
                return result
            return success(result)
        except ValueError as e:
            return error("invalid_args", str(e))
        except TimeoutError as e:
            return error("timeout", str(e))
        except Exception as e:
            logger.error(f"工具执行异常 [{func.__name__}]: {e}\n{traceback.format_exc()}")
            return error("execution_error", str(e))
    wrapper.__name__ = func.__name__
    wrapper.__doc__ = func.__doc__
    return wrapper


def format_response(env: dict) -> str:
    """将 Envelope 转为 JSON 字符串"""
    return json.dumps(env, ensure_ascii=False, default=str)
