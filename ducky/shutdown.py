"""ducky.shutdown — 后台循环的优雅停机信号（v20.3.2 正式版 · P2-20 · Gemini P1-4）。

七个常驻循环此前都是 `while True: ... time.sleep(N)`：SIGTERM 到来时 uvicorn 结束
lifespan，守护线程被进程退出硬切 —— 正在写的一轮巩固/反思可能留下半截。
这里给一个进程级事件：循环用 `sleep()` 代替 `time.sleep()`，被叫停时立刻醒来并
返回 False，循环据此收尾退出；lifespan 在 yield 之后 `request_shutdown()`。
"""
from __future__ import annotations

import threading

SHUTDOWN = threading.Event()


def request_shutdown() -> None:
    SHUTDOWN.set()


def stopping() -> bool:
    return SHUTDOWN.is_set()


def sleep(seconds: float) -> bool:
    """可中断的睡眠：正常醒来返回 True；停机请求到来立刻返回 False。"""
    return not SHUTDOWN.wait(max(0.0, float(seconds)))
