"""ducky.failure_ledger — 特性级失败计数（v20 · P1-8）

外部审计 M7 ／ 第三方审计低-6：宽捕获遍地。AST 普查实测（射程 `ducky/` + 三个服务
入口）：**489 处**宽捕获，其中重抛 53、纯 pass 20、有动作但零日志 79、**只有 debug
152**、有 warning/error 184。也就是说 251 处在生产默认日志级别下**等于无声**。

整改口径不是「全改」—— 那会用噪声淹掉真信号，而且多数无声是正当的（并发建表、
向前兼容、时间戳解析兜底、`health.py` 里把错误写进响应字段的那批）。口径是**只改
特性级入口**：挂在写入／读取主链路上、失败后有**持久的用户可见后果**的那些。

判据来自审计点名的那个前科（`ducky/self_edit.py:293` 自己写着）：

    每次 /add 都稳定抛 TypeError，又被调用方的 except Exception 收进一条
    logger.debug。结果是 P0-2 的 LLM 语义级去重在 v20 里**从未执行过一次**，
    日志上却什么都看不出来。

每个改动点都要能回答铁律 8 那句「如果这里真失败了，谁会知道？」。这个模块就是那个
「谁」：一条 warning（看得见）+ 一个计数器（`/health` 端得出来，事后查得到）。

刻意的设计取舍：
· **不改控制流。** 原来的 `logger.debug` 一行不动（细节还在那儿），只在它前面插一条
  记账。降级路径的行为逐字节不变 —— 观测器不许改变被观测者。
· **同一特性的 warning 有限流。** 前 3 次打 warning，之后每 100 次打一条汇总。
  一个每秒失败的特性不该把日志刷满，但也不许彻底沉默。
· **计数只增不减、进程内、不落盘。** 目标是「事故当时看得见」，不是「事后审计」。
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger("aiduMEM.failure")

_LOUD_FIRST = 3        # 前几次一律 warning
_SUMMARY_EVERY = 100   # 之后每多少次打一条汇总

_counts: dict[str, int] = {}
_lock = threading.Lock()


def feature_failed(feature: str, exc: BaseException | None = None,
                   detail: str = "") -> None:
    """记一次特性级失败：计数 +1，并按限流打 warning。

    `feature` 是**特性名**，不是函数名 —— 它会出现在 `/health` 里给运维看，
    所以用「index_memory」这种能对上业务的名字，别用内部私有函数名。
    """
    try:
        with _lock:
            _counts[feature] = _counts.get(feature, 0) + 1
            n = _counts[feature]
        if n <= _LOUD_FIRST or n % _SUMMARY_EVERY == 0:
            logger.warning(
                "特性 %s 第 %d 次失败：%s%s —— 主链路已降级继续，但这件事没有发生",
                feature, n, type(exc).__name__ if exc else "未知",
                f"（{exc}）" if exc else "",
            )
            if detail:
                logger.warning("  ↳ %s", detail)
    except Exception:
        # 记账器挂在降级路径上，绝不许把降级路径再带崩一层
        pass


def snapshot() -> dict:
    """供 `/health` 使用：`{"total": N, "by_feature": {...}}`。

    空字典时 `total` 为 0 —— 这里 0 是有意义的（进程启动以来没有特性级失败），
    与 `http_error_rate_5m` 那个「无流量」的情形不同，不需要 None。
    """
    with _lock:
        by = dict(_counts)
    return {"total": sum(by.values()), "by_feature": by}


def reset() -> None:
    """只给测试用。"""
    with _lock:
        _counts.clear()
