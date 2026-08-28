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

import os
import threading
import logging
import time
from typing import Dict, Optional, Tuple

from ducky.env_config import config_errors, int_env


_ADD_ENV = "AIDUMEI_RATE_ADD_PER_MIN"
_DELETE_ALL_ENV = "AIDUMEI_RATE_DELETE_ALL_PER_MIN"
_DEFAULT_ADD_PER_MIN = 120
_DEFAULT_DELETE_ALL_PER_MIN = 3

logger = logging.getLogger("aiduMEM.RateGuard")

_WINDOWS: Dict[Tuple[str, str], Tuple[int, int]] = {}
_LOCK = threading.Lock()

# v20.2.3（自查 S-1）：**这张表原先从不清理**，而 /login 是免鉴权公开
# 端点 —— 自查实测：5 万个不同源 IP 各打一次失败登录 = 5 万条常驻条目
# （约 12MB），100 万 IP 约 240MB，两小时前的死条目永不回收。
# 我为了堵爆破洞加的按 IP 分桶，恰好把一张有界的表（租户数有限）
# 变成了攻击者可撑爆的表（IPv6 一个 /64 就有 2^64 个地址）。
# 修法照抄同仓 ducky/security/auth.py 的 _purge_expired_locked ——
# **那个成熟模式一直在旁边，我加护栏时没抄它。**
# 过期条目丢弃是**语义无损**的：读取时若 w != win 本就按新窗口处理，
# 旧窗口的条目对判定不产生任何影响，删与不删结果逐字节相同。
_SWEEP_THRESHOLD = 4096   # 超过这个规模才扫，正常部署永不触发


def _sweep_stale_locked(win: int) -> int:
    """丢弃非当前窗口的死条目。调用方必须已持 _LOCK。"""
    dead = [k for k, (w, _) in _WINDOWS.items() if w != win]
    for k in dead:
        _WINDOWS.pop(k, None)
    return len(dead)


# v20.2.3（外审 M-2）：实现收编进 ducky/env_config（单一真相源），
# 公开行为逐字不变。见该模块头注与 gear.py 同款说明。
def _limit_from_env(env_name: str, default: int) -> int:
    """0 = 关闭该路限流；非法值回退默认并点名出声（不炸写路径）。"""
    return int_env(env_name, default, minimum=0)


def rate_config_errors() -> dict:
    return config_errors(_ADD_ENV, _DELETE_ALL_ENV, _LOGIN_ENV)


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
        if len(_WINDOWS) > _SWEEP_THRESHOLD:
            _sweep_stale_locked(win)
        w, c = _WINDOWS.get(key, (win, 0))
        if w != win:
            w, c = win, 0
        if c >= limit:
            return max(1, int((w + 1) * 60 - t))
        _WINDOWS[key] = (w, c + 1)
    return None


# ── 登录爆破护栏（v20.2.3 · 外审 M-1）─────────────────────────────────
# /login 是公网可达入口（按文档加反代对外后尤其如此），此前**无限流、
# 无失败锁定**。PBKDF2 200k 轮让单次校验 ~100ms，是减速带不是墙。
#
# 与写路径限流的两点不同，都是刻意的：
#   ① **只计失败**：成功登录不该被计数——正常用户换设备连登几次不该被锁；
#   ② **先查后验**：超限时直接 429，连口令校验都不做（既省 PBKDF2 的
#      100ms，也让攻击者拿不到「这次算不算数」的旁路信号）。
# 分桶键是客户端 IP。**反代之后 request.client.host 是反代的 IP**——
# 那种部署下本护栏退化为全局阈值（仍拦得住爆破，但会牵连同源用户）；
# X-Forwarded-For 可伪造，未经可信反代校验绝不拿来当分桶键，宁可退化
# 也不给攻击者一个「换个头就换个桶」的绕过口。
_LOGIN_ENV = "AIDUMEI_LOGIN_FAILURES_PER_MIN"
_DEFAULT_LOGIN_FAILURES_PER_MIN = 10


def login_failure_limit() -> int:
    """每分钟每 IP 允许的登录失败次数（0=关闭本护栏）。"""
    return int_env(_LOGIN_ENV, _DEFAULT_LOGIN_FAILURES_PER_MIN, minimum=0)


# ── 登录失败表（v20.2.4 · 外审 F-19 重写）──
#
# v20.2.3 的 S-1 给它加了清理，但清理只处理**过期窗口**的条目 —— 同一窗口内
# 的新 IP 一条都清不掉。外审实测：一个窗口写入 10,000 个不同 IP，得到 10,000
# 条常驻，且越过阈值后每次插入都遍历整张字典，规模上去是平方级。
# 「超过阈值才 sweep」限制不了当前窗口的增长，这是当时修法本身的盲区。
#
# 现在的结构：**每个窗口一个字典，跨窗口整体丢弃**（O(1)，没有扫描这回事），
# 加一条硬上限。表被撑满意味着正在被大规模爆破 —— 此时进入**本窗口的全局
# 节流**：既不让新 IP 免疫护栏（那是绕过），也不永久锁死所有人（那是 DoS），
# 下一分钟自动解除。真正的公网入口限流仍应放在反代/WAF 上。
_LOGIN_MAX_IPS_ENV = "AIDUMEI_LOGIN_MAX_TRACKED_IPS"
_DEFAULT_LOGIN_MAX_IPS = 4096
_login_win: int = -1
_login_fails: dict = {}
_login_throttle_win: int = -1


def login_max_tracked_ips() -> int:
    return int_env(_LOGIN_MAX_IPS_ENV, _DEFAULT_LOGIN_MAX_IPS, minimum=64)


def _roll_login_window_locked(win: int) -> None:
    """跨窗口就把整张表丢掉 —— 不遍历、不逐条判过期。"""
    global _login_win, _login_fails
    if win != _login_win:
        _login_win = win
        _login_fails = {}


def login_table_status() -> dict:
    """/health 与测试用：登录表规模、上限、是否处于全局节流。"""
    with _LOCK:
        return {
            "tracked_ips": len(_login_fails),
            "max_tracked_ips": login_max_tracked_ips(),
            # v20.2.5（用户实测 🟢）：-1 是「还没有过登录失败」的哨兵，直接显示给用户
            # 看不出这层意思。空闲态说人话。
            "window": _login_win if _login_win >= 0 else "idle",
            # 判据必须带 `_login_win >= 0`：两者的初始值都是 -1，于是**刚启动、
            # 表还是空的**时候相等 —— 探针会报「全局节流已激活」。
            # 探针自己撒的谎和缺陷一样害人，而且更难查（它看起来像证据）。
            "global_throttle_active": (_login_win >= 0
                                       and _login_throttle_win == _login_win),
        }


def login_locked(client_ip: str, *, now: Optional[float] = None) -> Optional[int]:
    """只查不计：该 IP 当前是否已超失败上限。超限返回建议 Retry-After 秒。"""
    limit = login_failure_limit()
    if limit <= 0:
        return None
    t = time.time() if now is None else now
    win = int(t // 60)
    with _LOCK:
        _roll_login_window_locked(win)
        # 表被撑满触发的全局节流：本窗口内所有 IP 一律按超限处理。
        if _login_throttle_win == win:
            return max(1, int((win + 1) * 60 - t))
        if _login_fails.get(str(client_ip), 0) < limit:
            return None
        return max(1, int((win + 1) * 60 - t))


def record_login_failure(client_ip: str, *, now: Optional[float] = None) -> int:
    """记一次登录失败，返回本窗口内的累计失败数。"""
    global _login_throttle_win
    t = time.time() if now is None else now
    win = int(t // 60)
    ip = str(client_ip)
    with _LOCK:
        _roll_login_window_locked(win)
        cur = _login_fails.get(ip)
        if cur is not None:
            _login_fails[ip] = cur + 1
            return cur + 1
        if len(_login_fails) >= login_max_tracked_ips():
            # 表满 = 正在被大规模爆破。进入本窗口全局节流，且**停止增长**。
            if _login_throttle_win != win:
                _login_throttle_win = win
                logger.warning(
                    "🛡️ [RateGuard] 登录失败表达到上限 %d，本窗口转入全局节流"
                    "（下一分钟自动解除）。公网入口限流请配在反代/WAF。",
                    login_max_tracked_ips(),
                )
            return max(login_failure_limit(), 1)
        _login_fails[ip] = 1
        return 1


def window_count() -> int:
    """/health 与测试用：当前计数窗口条目数（无界增长的可观测面）。"""
    with _LOCK:
        return len(_WINDOWS)


def reset_rate_windows() -> None:
    """测试与运维用：清空**全部**计数窗口。

    v20.2.4（外审 F-19）：登录失败表已搬出共享的 _WINDOWS，这里必须一起清 ——
    漏一个就是测试间状态泄漏，而症状是「另一条用例莫名其妙地红」。
    """
    global _login_win, _login_fails, _login_throttle_win
    with _LOCK:
        _WINDOWS.clear()
        _login_win = -1
        _login_fails = {}
        _login_throttle_win = -1