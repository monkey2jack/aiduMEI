"""对外错误正文的**唯一真相源**（参赛前自查 WP-C）。

🔴 由来：元守卫 `test_no_route_hands_a_bare_exception_to_the_caller` 一次扫出
**29 处** `HTTPException(500, str(e))` —— 六个文件里，写法一模一样。
调用方拿到的是 Python 异常的 `str()`，也就是内部实现细节：

    {"detail": "Using SOCKS proxy, but the 'socksio' package is not installed…"}

这句话是真的，但它回答的是「内部哪里断了」，而不是「拿到这个响应的人该做什么」。
对一个准备拿出去给人用的项目，这是**面子问题也是里子问题**：
面子上它像没做完，里子上它把实现细节暴露给了任何一个能调接口的人。

**为什么做成一个函数而不是在 29 个地方各写一句**：这个仓在 v20.2.5 记过
「契约抄两遍就一定会改一遍漏一遍」，也记过原则 P1「单一真相源，不靠调用点自觉」。
29 个调用点各自措辞，下一次要改口径就得改 29 遍，漏掉的那处不会有人发现。

**保留原始错误**是刻意的：运维要靠它定位。改的是「它不再是正文的全部」。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("aiduMEM.api_errors")

_MAX_RAW = 160

def _expose_raw_errors() -> bool:
    """f0.6 W-32（muse L-1 / cue §5）：原始异常正文默认不出网。

    前 160 字符可能含文件路径 / SQL 片段 / 上游端点地址——对运维有价值，
    对「能调接口的任何人」是内部信息。诊断开关 AIDUMEI_EXPOSE_RAW_ERRORS=1
    保留旧行为（自托管调试用），默认响应只留类别与指引，原始错误进日志。
    """
    return os.environ.get("AIDUMEI_EXPOSE_RAW_ERRORS", "0").strip().lower() in ("1", "true", "yes")


_SAFE_ERROR_CLASSES = {
    # 业务校验类：message 是服务端自己写的诊断语（含 bank_id 等调用方需要
    # 的字段信息），不含路径/SQL/端点——默认保留原文。
    "BankScopeError", "BankScopeValidationError", "ValueError", "KeyError",
}

_INTERNAL_ERROR_MARKERS = (
    # 库层异常特征：默认脱敏（路径/SQL/连接串风险）
    "sqlite3", "no such column", "no such table", "sql:", "Traceback",
    "File \"", "/data/", "/home/", "/root/", ".py", "http://", "https://",
)

def _raw_is_sensitive(exc: Exception, raw: str) -> bool:
    """f0.6 W-32 折衷（muse L-1 / cue §5 与既有「错误带病因」用例的交集）：
    业务校验类错误保留原文（调用方要靠它知道哪个字段错）；库层异常
    （可能带路径/SQL/端点）默认脱敏，只给类别。AIDUMEI_EXPOSE_RAW_ERRORS=1
    恢复全量原文（自托管调试）。"""
    if exc.__class__.__name__ in _SAFE_ERROR_CLASSES:
        return False
    # 锁库/busy：诊断关键（运维要知道是锁不是逻辑错）且不含路径——不脱敏
    low = raw.lower()
    if ("locked" in low or "busy" in low) and "no such" not in low:
        return False
    return any(m in raw for m in _INTERNAL_ERROR_MARKERS) or         exc.__class__.__module__.startswith(("sqlite3", "requests", "httpx", "socket"))

def api_error_detail(exc: Exception, *, hint: str = "") -> str:
    """把服务端异常翻译成对外正文：先说这是什么性质的错，再附原始错误。

    `hint` 给调用点补一句更具体的指引（可选）。不给也能用 —— 通用那句
    至少告诉了调用方两件事：**不是你的参数写错了**，以及**去哪儿看状态**。
    """
    raw = str(exc).strip() or exc.__class__.__name__
    if _expose_raw_errors() or not _raw_is_sensitive(exc, raw):
        tail = f"原始错误：{raw[:_MAX_RAW]}"
    else:
        logger.warning("对外错误脱敏：原始异常留日志（AIDUMEI_EXPOSE_RAW_ERRORS=1 可恢复旧行为）: %s",
                       raw[:_MAX_RAW])
        tail = f"错误类别：{exc.__class__.__name__}（详情见服务端日志）"
    guide = hint.strip() or "可用 GET /health 查看 degraded 与 degraded_details 确认组件状态"
    return f"服务端处理失败（非调用参数问题）。{guide}。{tail}"


# v20.3.2 正式版（P2-34 · Codex F-11 部分采纳）：搜索路径的 200+status=error 信封
# **不改状态码**（老客户端按 200 解析），但补两个机器可读字段：
#   error_code —— 异常类名（不含消息，不泄内部路径）；
#   retryable  —— 调用方能否原样重试（超时 / 连接 / 忙锁 / 限流 → True；参数、配置类 → False）。
_RETRYABLE_MARKERS = ("timeout", "timed out", "connection", "temporarily", "unavailable",
                      "rate limit", "ratelimit", "429", "503", "database is locked", "busy")


def is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    text = (str(exc) + " " + exc.__class__.__name__).lower()
    return any(m in text for m in _RETRYABLE_MARKERS)


def error_envelope(exc: Exception, *, hint: str = "") -> dict:
    """{"detail": 对外正文, "error_code": 异常类名, "retryable": bool} —— 搜索类 200 信封用。"""
    return {
        "detail": api_error_detail(exc, hint=hint),
        "error_code": exc.__class__.__name__,
        "retryable": is_retryable(exc),
    }
