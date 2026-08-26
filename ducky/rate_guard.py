"""ducky.rate_guard — 进程内固定窗口限流（v20.1.1 · N-1，外审建议采纳）

护的不是「恶意攻击者」（默认 127.0.0.1 + Bearer 鉴权下攻击者先得过门），
护的是**失控的自动化**：上层 Agent 陷入循环时每秒几十次写入 / 反复
delete_all，在配额烧穿或数据清空之前被 429 拦停。

默认值不拍脑袋，来自生产 14 天日志实测（2026-08-26）：
  - 写路径（/add 系）分钟峰值 35 次 → 默认 120/min（3.4 倍余量，
    正常流量永不触发；>2/s 的持续写入只有失控循环做得到）。
  - /delete_all 14 天共 7 次（全为维护/冒烟）→ 默认 3/min
    （单会话人工或 Agent 正常操作的充分余量，挡住循环删除）。

契约：
  - 超限返回建议的 Retry-After 秒数（到下一窗口的剩余秒），不丢数据、
    不静默——429 是显式信号，与三态判语同一哲学。
  - 计数按 (route, user_id) 分桶：一个租户超限不牵连别人。
  - env 覆盖：AIDUMEI_RATE_ADD_PER_MIN / AIDUMEI_RATE_DELETE_ALL_PER_MIN；
    0 = 关闭该路限流；非法值**报错点名**不静默回退（配置生效三查纪律）。
  - 生效值进 /health 探针，运维面可查。
  - 单进程内存实现是有意的：本服务是单进程部署形态；跨实例限流属于
    多实例路线图（v21），不在此处装样子。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Dict, Optional, Tuple

_ADD_ENV = "AIDUMEI_RATE_ADD_PER_MIN"
_DELETE_ALL_ENV = "AIDUMEI_RATE_DELETE_ALL_PER_MIN"
_DEFAULT_ADD_PER_MIN = 120
_DEFAULT_DELETE_ALL_PER_MIN = 3

_WINDOWS: Dict[Tuple[str, str], Tuple[int, int]] = {}
_LOCK = threading.Lock()


def _limit_from_env(env_name: str, default: int) -> int:
    raw = os.environ.get(env_name)
    if raw is None:
        return default
    try:
        v = int(raw.strip())
        if v < 0:
            raise ValueError
        return v
    except ValueError:
        raise ValueError(
            f"{env_name} 非法值 {raw!r}：需 >=0 的整数（0=关闭该路限流）"
        )


def add_rate_limit() -> int:
    return _limit_from_env(_ADD_ENV, _DEFAULT_ADD_PER_MIN)


def delete_all_rate_limit() -> int:
    return _limit_from_env(_DELETE_ALL_ENV, _DEFAULT_DELETE_ALL_PER_MIN)


def check_rate(route: str, user_id: str, *, limit: int,
               now: Optional[float] = None) -> Optional[int]:
    """未超限：计数并返回 None。超限：返回建议 Retry-After 秒数（不计数）。

    limit<=0 视为关闭，恒放行。窗口是自然分钟（固定窗口）：实现最简、
    语义可测；边界突刺（窗口交界最多 2×limit）对「拦失控循环」这个
    目标无碍——失控循环是持续的，不是恰好卡在边界上的两发。
    """
    if limit <= 0:
        return None
    t = time.time() if now is None else now
    win = int(t // 60)
    key = (route, str(user_id))
    with _LOCK:
        w, c = _WINDOWS.get(key, (win, 0))
        if w != win:
            w, c = win, 0
        if c >= limit:
            return max(1, int((w + 1) * 60 - t))
        _WINDOWS[key] = (w, c + 1)
    return None


def reset_rate_windows() -> None:
    """测试与运维用：清空全部计数窗口。"""
    with _LOCK:
        _WINDOWS.clear()
