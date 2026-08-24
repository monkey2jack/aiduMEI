"""ducky.http_metrics — 进程内 HTTP 结局计数（v20 · P1-9）

外部审计 M12 ／ 用户视角审计四：`health.py` 里搜 `5xx|error_rate|alert` 只命中一行注释。
`/health` 只探活，不探错误率 —— 于是「195 次 500、持续 13 分钟」那次事故**没有留下
任何可复现的监控路径**：服务全程 active、`/health` 全程 ok，而三分之一的请求在报错。

这个模块只做一件事：把最近一段时间的响应状态码记在内存里，让 `/health` 能回答
「现在错得多不多」。

三条刻意的设计取舍：

· **进程内、不落盘、不引依赖。** 目标是「事故当时能看见」，不是「事后能查账」。
  落盘会把一个观测器变成一个新的故障源（磁盘满、锁竞争、权限），得不偿失。
· **窗口内没有任何请求时，错误率报 `None` 而不是 `0.0`。**
  `0.0` 的意思是「有流量且没出错」，`None` 的意思是「没有流量，无从判断」。
  把后者渲染成前者，就是拿一个绿灯掩盖一次「服务其实没人用」——而那恰恰是
  事故的常见形态之一（上游全挂，本服务闲着，一切"正常"）。
· **计数器本身不许抛异常。** 它挂在每个请求的路径上；一个观测器把主链路带崩，
  比没有观测器糟得多。所以记录函数整体 try/except，失败只丢这一条样本。
"""
from __future__ import annotations

import threading
import time
from collections import deque

#: 滑动窗口长度（秒）。和 `/health` 字段名 `http_error_rate_5m` 是同一个真相源 ——
#: 改这里就必须改字段名，否则字段名会开始说谎（宣称即承诺）。
WINDOW_S = 300

#: 样本上限。5 分钟内 20000 个请求已经是本服务量级的十几倍，够用且封顶内存。
#: 满了之后丢最老的 —— 丢老样本会让错误率略微偏向近期，这在观测上是想要的方向。
MAX_SAMPLES = 20000

_samples: deque[tuple[float, int]] = deque(maxlen=MAX_SAMPLES)
_lock = threading.Lock()


def record(status_code: int, *, now: float | None = None) -> None:
    """记一次响应结局。挂在请求路径上，所以整体不抛。"""
    try:
        t = time.time() if now is None else now
        with _lock:
            _samples.append((t, int(status_code)))
    except Exception:
        pass


def _prune(cutoff: float) -> None:
    while _samples and _samples[0][0] < cutoff:
        _samples.popleft()


def snapshot(*, now: float | None = None) -> dict:
    """窗口内的结局分布。

    返回 `{"window_s", "total", "server_errors", "client_errors", "error_rate_5m"}`；
    `error_rate_5m` 在窗口内无样本时为 `None`（见模块 docstring 第二条取舍）。
    """
    t = time.time() if now is None else now
    with _lock:
        _prune(t - WINDOW_S)
        rows = list(_samples)
    total = len(rows)
    server = sum(1 for _, c in rows if 500 <= c <= 599)
    client = sum(1 for _, c in rows if 400 <= c <= 499)
    return {
        "window_s": WINDOW_S,
        "total": total,
        "server_errors": server,
        "client_errors": client,
        # 只把 5xx 计入「错误率」：4xx 多半是调用方姿势不对（含门禁 401），
        # 混进来会让这条指标失去「服务端是否在出错」的含义。4xx 单独暴露。
        "error_rate_5m": round(server / total, 4) if total else None,
    }


def reset() -> None:
    """清空样本 —— 只给测试用。"""
    with _lock:
        _samples.clear()
