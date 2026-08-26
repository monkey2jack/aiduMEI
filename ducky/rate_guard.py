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
    0 = 关闭该路限流；非法值**回退默认并点名出声**（warning 一次 +
    /health 探针常驻）——v20.2.1 外审 R1 同款：限流站在写路径入口，
    在这里 raise 等于每次 /add 直接 500。不静默的纪律不变，出声方式
    从「炸」改成「可观测」。
  - 生效值进 /health 探针，运维面可查。
  - 单进程内存实现是有意的：本服务是单进程部署形态；跨实例限流属于
    多实例路线图（v21），不在此处装样子。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Dict, Optional, Tuple

logger = logging.getLogger("aiduMEM.rate_guard")

_ADD_ENV = "AIDUMEI_RATE_ADD_PER_MIN"
_DELETE_ALL_ENV = "AIDUMEI_RATE_DELETE_ALL_PER_MIN"
_DEFAULT_ADD_PER_MIN = 120
_DEFAULT_DELETE_ALL_PER_MIN = 3

_WINDOWS: Dict[Tuple[str, str], Tuple[int, int]] = {}
_LOCK = threading.Lock()


_config_errors: dict = {}


def _limit_from_env(env_name: str, default: int) -> int:
    """v20.2.1（外审 R1 同款）：限流站在写路径入口，非法 env 在这里 raise
    会让每次 /add 直接 500 —— 回退默认 + 出声，不炸业务路径。"""
    raw = os.environ.get(env_name)
    if raw is None:
        _config_errors.pop(env_name, None)
        return default
    try:
        v = int(raw.strip())
        if v < 0:
            raise ValueError
        _config_errors.pop(env_name, None)
        return v
    except ValueError:
        msg = f"{env_name} 非法值 {raw!r}，已回退默认 {default}（0=关闭该路限流）"
        if _config_errors.get(env_name) != msg:
            logger.warning("🚧 %s", msg)
        _config_errors[env_name] = msg
        return default


def rate_config_errors() -> dict:
    return dict(_config_errors)


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
