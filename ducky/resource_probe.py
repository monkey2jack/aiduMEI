"""ducky.resource_probe — 进程资源占用探针（v20 · 部署方指定的产品指标）

为什么它是产品指标而不是运维小工具
────────────────────────────────
aiduMEI 是一个**常驻**服务：它替用户记着东西，所以它必须一直活着。一个记忆引擎
如果内存单调上涨、文件描述符只增不减、线程越跑越多，那它不是「性能差」，是
**迟早会把用户的记忆一起带走**。所以「占多少资源」和「召回准不准」是同一级别的指标。

零新依赖是刻意的
────────────────
不引 `psutil`。这个探针挂在 `/health` 上，而 `/health` 是事故当时唯一还能问的东西；
给它加一个第三方依赖，等于给最后一道观测手段加一个新的失效点。Linux 上走 `/proc`，
其余平台退回标准库 `resource`。

语义诚实是这个模块的全部难点
──────────────────────────
· `rss_mb` 是**当前**常驻内存，只有 Linux 的 `/proc/self/status` 给得出来。
· `max_rss_mb` 是**历史峰值**，`resource.getrusage` 各平台都有 —— 但它**不是**
  当前占用。两者绝不能混成一个字段：把峰值当现值，会让一次早已结束的尖峰永远
  挂在监控上；把现值当峰值，会让真尖峰完全看不见。
· `cpu_seconds` 是进程启动以来的累计 CPU 时间。**不换算成百分比** —— 百分比需要
  两次采样和一个时间窗，单次调用给不出来。给一个假的百分比比不给更糟。
· 测不到的一律 `None`，绝不填 0。`0` 的意思是「测了，是零」；`None` 的意思是
  「这个平台测不出来」。
"""
from __future__ import annotations

import os
import sys
import threading

# v20.1 整改轮（R-12 · 社区审计）：`resource` 是 POSIX-only 标准库，
# Windows 上不存在。此前顶层裸 import 让整套测试在 Windows 收集阶段即崩
# （tests 顶层引本模块 → ModuleNotFoundError → 一个用例都跑不起来），
# /health 资源指标恒 None 还带着 ImportError。docstring 里「其余平台退回
# 标准库 resource」退回的是个不存在的模块。按本模块自己的哲学修：
# **测不到的一律 None，绝不填 0，更不许崩** —— Windows 上 resource=None，
# 依赖它的字段如实返回 None。
try:
    import resource  # POSIX-only
except ImportError:  # pragma: no cover - Windows
    resource = None

_KB = 1024.0


def _linux_status_kb(key: str) -> float | None:
    """从 /proc/self/status 取一个以 kB 为单位的字段。"""
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for ln in f:
                if ln.startswith(key + ":"):
                    return float(ln.split()[1])   # 单位固定 kB
    except (OSError, ValueError, IndexError):
        return None
    return None


def _open_fds() -> int | None:
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        pass
    try:  # 退路：部分平台有 fdopendir 之外的办法，没有就诚实报 None
        import subprocess
        out = subprocess.run(["lsof", "-p", str(os.getpid())],
                             capture_output=True, text=True, timeout=3)
        if out.returncode == 0:
            return max(0, len(out.stdout.splitlines()) - 1)
    except Exception:
        pass
    return None


def _max_rss_mb() -> float | None:
    """`ru_maxrss` 的单位随平台变：Linux 是 kB，macOS/BSD 是 **字节**。

    这一条不是洁癖 —— 直接按 kB 算，macOS 上会把 100MB 报成 100GB，
    而那个数字看着就像一次内存泄漏，会让人去查一个不存在的故障。
    """
    if resource is None:  # Windows：测不到 → None（R-12）
        return None
    try:
        raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (OSError, ValueError):
        return None
    if raw <= 0:
        return None
    if sys.platform == "darwin":
        return round(raw / _KB / _KB, 2)       # 字节 → MB
    return round(raw / _KB, 2)                # kB → MB


def snapshot() -> dict:
    """当前进程的资源画像。字段语义见模块 docstring —— 每个 None 都是「测不到」。"""
    try:
        if resource is None:  # Windows：测不到 → None（R-12）
            raise OSError("resource module unavailable on this platform")
        ru = resource.getrusage(resource.RUSAGE_SELF)
        cpu = round(ru.ru_utime + ru.ru_stime, 2)
    except (OSError, ValueError):
        cpu = None

    rss_kb = _linux_status_kb("VmRSS")
    return {
        # 当前常驻内存（仅 Linux 可得）
        "rss_mb": round(rss_kb / _KB, 2) if rss_kb is not None else None,
        # 历史峰值常驻内存（跨平台，**不是**当前值）
        "max_rss_mb": _max_rss_mb(),
        # 累计 CPU 秒（用户态 + 内核态），不是百分比
        "cpu_seconds": cpu,
        "threads": threading.active_count(),
        "open_fds": _open_fds(),
        "pid": os.getpid(),
    }
