"""ducky.gear — 挡位切换器（v20.2 WP-G · 智慧引擎自动挡）

标准熔断器三态机。v20.2.2 起管**两条外部腿**，一腿一实例、状态互相独立：
    · 云嵌入腿（v20.2）——降挡进 lite，本地备胎索引接管语义召回；
    · LLM 蒸馏腿（v20.2.2）——降挡后写入跳过蒸馏，确定性直写秒回
      （实弹取证 2026-08-26：LLM 网关 521 + 传输层盲重试把单次 /add
      同步挂 4.5 分钟——嵌入活着时这不该发生）。

    closed（full 挡）--连续 N 次失败--> open（lite 挡）
    open --冷却 T 秒--> half-open（放探测）
    half-open --连续 M 次成功--> closed；任一失败 --> open（重新冷却）

防抖是灵魂：**假恢复（单次侥幸成功）不许过早升挡** —— half-open 需要
连续 M 次成功才回 full；反方向，偶发一次超时也不该立刻降挡（N 次连续
失败才降）。参数默认 N=3 / M=2 / T=60s：工程惯例值，**显式标注待生产
故障分布校准**（与召回阈值 0.46 的校准流程同款 —— 先诚实默认，
拿到真实分布再定，绝不冒充「算出来的」）。

纪律：
  - 升降挡是**事件**，进事件账本（actor=gear_shifter；嵌入腿
    target_id=engine_mode，LLM 腿 target_id=llm_leg）——「哪段时间
    跑在备胎上/没在蒸馏」必须审计可查，这是挡位诚实化（WP-H）的地基。
  - **两腿状态互相独立**：LLM 断供不碰嵌入挡位，反之亦然——信号纯净
    是 v20.2.1 外审 Y2 教训的结构化（装配 bug 误记云失败会把半开探测
    成功倒打成失败）。
  - 状态存进程内存：重启回 closed（full）重新试探是合理语义 ——
    重启本身就是一次「重新认识世界」。
  - 本模块只做判定，不做调用：失败/成功信号由真正调用外部服务的位点
    上报（嵌入腿：engine 向量腿；LLM 腿：写路径 layer1，只认 LLMError
    形态——非 LLM 异常不许污染信号）。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Callable, Optional

from ducky.env_config import config_errors, float_env as env_float, int_env as env_int

logger = logging.getLogger("aiduMEM.gear")

_FAIL_ENV = "AIDUMEI_GEAR_TRIP_FAILURES"      # 嵌入腿 closed→open 连续失败数
_RECOVER_ENV = "AIDUMEI_GEAR_RECOVER_SUCCESSES"  # 嵌入腿 half-open→closed 连续成功数
_COOLDOWN_ENV = "AIDUMEI_GEAR_COOLDOWN_SEC"   # 嵌入腿 open→half-open 冷却秒
_LLM_FAIL_ENV = "AIDUMEI_LLM_GEAR_TRIP_FAILURES"
_LLM_RECOVER_ENV = "AIDUMEI_LLM_GEAR_RECOVER_SUCCESSES"
_LLM_COOLDOWN_ENV = "AIDUMEI_LLM_GEAR_COOLDOWN_SEC"
_DEFAULT_TRIP = 3
_DEFAULT_RECOVER = 2
_DEFAULT_COOLDOWN = 60.0


# v20.2.1（外审 R1 · 配置雷）：阈值站在熔断主路径上（should_try_* 在
# 请求 try 之前）——非法 env 若在这里 raise，一个配置笔误就把
# 「断供保命机制」反转成「腿全灭」。保命路径的纪律修正为：
# **回退默认 + 出声（warning 一次 + 探针常驻）**，绝不抛。
# 「非法值不静默」的老纪律没有丢——它从「炸」改成了「可观测」。
# v20.2.3（外审 M-2）：本模块 v20.2.1 自己拆过一次雷，实现留在了本地；
# 外审在 auth/scoring/injection_guard 里发现同款雷之后，实现收编进
# ducky/env_config（单一真相源），这里只保留「本腿关心哪几个 env」。
# 公开行为逐字不变：非法值回退默认、warning 一次、status 里可查。
_GEAR_ENVS = (_FAIL_ENV, _RECOVER_ENV, _COOLDOWN_ENV,
              _LLM_FAIL_ENV, _LLM_RECOVER_ENV, _LLM_COOLDOWN_ENV)


def _int_env(name: str, default: int) -> int:
    return env_int(name, default, minimum=1)


def _float_env(name: str, default: float) -> float:
    # v20.2.3（外审 A-2）：v20.2.1 的旧判据是 `v > 0`，收编时用
    # minimum=0.000001 近似，于是 (0, 1e-6) 的合法旧值被拒 ——「逐字不变」
    # 就不逐字了。改用 exclusive_minimum 表达**真正的严格大于零**，
    # 边界由 tests 钉死，宣称与实况重新对齐。
    return env_float(name, default, exclusive_minimum=0.0)


def _gear_config_errors() -> dict:
    return config_errors(*_GEAR_ENVS)


def _ledger_shift(direction: str, reason: str, target_id: str) -> None:
    """升降挡进事件账本；账本失败不拖垮换挡（换挡是保命动作）。"""
    try:
        from ducky.event_ledger import ensure_ledger_schema, record_event
        from ducky.utils import get_facts_conn
        ensure_ledger_schema()
        conn = get_facts_conn()
        try:
            record_event(conn, actor="gear_shifter", action=direction,
                         target_id=target_id, reason=reason[:200])
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("换挡事件记账失败（不拖垮换挡）: %s", exc)


def _spawn_replay() -> None:
    """嵌入腿升挡后台重放欠账（lite 挡期间的蒸馏与本地补写）。守护线程，
    失败留账下轮——重放绝不阻塞升挡本身。v20.2.1（外审 R2）后重放有
    两个触发点（升挡事件 + 启动对账），线程逻辑收编进
    dual_index.spawn_replay_daemon 共用，绝不各养一套。"""
    try:
        from ducky.dual_index import spawn_replay_daemon
        spawn_replay_daemon(source="upshift")
    except Exception as exc:
        logger.warning("升挡欠账重放挂起失败（留账）: %s", exc)


class _Breaker:
    """一条外部腿的熔断器。所有可变状态自带锁，两腿互不共享、互不牵连。"""

    def __init__(self, leg: str, noun: str, target_id: str,
                 trip_env: str, recover_env: str, cooldown_env: str,
                 on_upshift: Optional[Callable[[], None]] = None):
        self.leg = leg                 # 探针/日志里的腿名
        self._noun = noun              # 换挡原因里的口语名（云嵌入 / LLM 蒸馏）
        self._target_id = target_id    # 事件账本 target_id
        self._trip_env = trip_env
        self._recover_env = recover_env
        self._cooldown_env = cooldown_env
        self._on_upshift = on_upshift
        self._lock = threading.Lock()
        self._state = "closed"         # closed | open | half_open
        self._consec_failures = 0
        self._consec_successes = 0
        self._opened_at: Optional[float] = None
        self._last_reason = ""
        self._shift_count = 0

    # ── 参数（每次读 env：回退语义见 _env_or_default）──
    def trip_threshold(self) -> int:
        return _int_env(self._trip_env, _DEFAULT_TRIP)

    def recover_threshold(self) -> int:
        return _int_env(self._recover_env, _DEFAULT_RECOVER)

    def cooldown_sec(self) -> float:
        return _float_env(self._cooldown_env, _DEFAULT_COOLDOWN)

    # ── 信号 ──
    def record_failure(self, reason: str = "", *, now: Optional[float] = None) -> None:
        """失败信号。closed 态连续 N 次 → 降挡；half-open 一次失败 → 回 open。"""
        t = time.time() if now is None else now
        with self._lock:
            self._consec_successes = 0
            if self._state == "half_open":
                self._state = "open"
                self._opened_at = t
                self._last_reason = f"半开探测失败：{reason[:120]}"
                self._shift_count += 1
                logger.warning("⚙️ %s腿：半开探测失败，回到 lite（%s）",
                               self._noun, reason[:120])
                _ledger_shift("downshift", self._last_reason, self._target_id)
                return
            if self._state == "open":
                return
            self._consec_failures += 1
            if self._consec_failures >= self.trip_threshold():
                self._state = "open"
                self._opened_at = t
                self._last_reason = (f"{self._noun}连续 {self._consec_failures} "
                                     f"次失败：{reason[:120]}")
                self._shift_count += 1
                logger.warning("⚙️ %s腿：降挡 full→lite（%s）",
                               self._noun, self._last_reason)
                _ledger_shift("downshift", self._last_reason, self._target_id)

    def record_success(self, *, now: Optional[float] = None) -> None:
        """成功信号。half-open 连续 M 次 → 升挡；closed 态清失败计数。"""
        with self._lock:
            self._consec_failures = 0
            if self._state == "closed":
                return
            if self._state == "half_open":
                self._consec_successes += 1
                if self._consec_successes >= self.recover_threshold():
                    self._state = "closed"
                    self._consec_successes = 0
                    self._last_reason = f"半开探测连续成功，{self._noun}恢复"
                    self._shift_count += 1
                    logger.info("⚙️ %s腿：升挡 lite→full（%s）",
                                self._noun, self._last_reason)
                    _ledger_shift("upshift", self._last_reason, self._target_id)
                    if self._on_upshift is not None:
                        self._on_upshift()

    # ── 判定 ──
    def should_try(self, *, now: Optional[float] = None) -> bool:
        """本次请求该不该试这条腿：closed 当然试；**half-open 也试** ——
        半开的语义就是拿真实流量当探针（不试探，成功信号永远不来，
        系统会卡死在备胎挡：断供演练首跑抓出的死锁，切换逻辑的命门）。
        open 态不试（冷却中，别去撞还没好的服务）。"""
        t = time.time() if now is None else now
        with self._lock:
            self._maybe_half_open(t)
            return self._state in ("closed", "half_open")

    def mode(self, *, now: Optional[float] = None) -> str:
        """当前挡位：'full' | 'lite'。open 态冷却到点自动转 half-open
        （half-open 仍报 lite —— 探测成功之前不许对外宣称恢复）。"""
        t = time.time() if now is None else now
        with self._lock:
            self._maybe_half_open(t)
            return "full" if self._state == "closed" else "lite"

    def _maybe_half_open(self, t: float) -> None:
        # 调用方必须已持锁
        if (self._state == "open" and self._opened_at is not None
                and t - self._opened_at >= self.cooldown_sec()):
            self._state = "half_open"
            logger.info("⚙️ %s腿：冷却结束，进入半开探测", self._noun)

    def policy_disabled(self) -> bool:
        """本腿是否被**部署配置**整条关闭（不是熔断，是选择）。"""
        try:
            from ducky.engine_mode import cloud_leg_enabled
            return not cloud_leg_enabled()
        except Exception:
            return False

    def status(self, *, now: Optional[float] = None) -> dict:
        """/health 探针：挡位、熔断器内态、参数生效值、最近换挡原因。

        v20.2.3（外审 A-4）：本腿被配置关闭时，mode 报 `disabled_by_policy`
        而不是 `full` —— 本地档下云腿一次都不会被尝试，报「full/closed」
        等于告诉值班人「云端腿正在服役且健康」，那是一句活生生的假话。
        **只改探针面，不改判定面**：current_mode() 保持 full|lite 二值语义
        （ducky/hot/add.py 拿它分流 lite 分支，混进第三个值会走错路），
        breaker 也保持熔断器的真实内态（它没被抹掉，只是没在服役）。
        """
        m = self.mode(now=now)
        disabled = self.policy_disabled()
        with self._lock:
            return {
                "leg": self.leg,
                "mode": "disabled_by_policy" if disabled else m,
                "policy_disabled": disabled,
                "breaker_mode_if_serving": m if disabled else None,
                "breaker": self._state,
                "consecutive_failures": self._consec_failures,
                "trip_threshold": self.trip_threshold(),
                "recover_threshold": self.recover_threshold(),
                "cooldown_sec": self.cooldown_sec(),
                "last_shift_reason": self._last_reason or None,
                "shift_count": self._shift_count,
                "config_errors": _gear_config_errors() or None,
            }

    def reset(self) -> None:
        with self._lock:
            self._state = "closed"
            self._consec_failures = 0
            self._consec_successes = 0
            self._opened_at = None
            self._last_reason = ""
            self._shift_count = 0


_EMBED = _Breaker("cloud_embed", "云嵌入", "engine_mode",
                  _FAIL_ENV, _RECOVER_ENV, _COOLDOWN_ENV,
                  on_upshift=_spawn_replay)
_LLM = _Breaker("llm", "LLM 蒸馏", "llm_leg",
                _LLM_FAIL_ENV, _LLM_RECOVER_ENV, _LLM_COOLDOWN_ENV)
# LLM 腿升挡无重放回调：挡内写入已确定性落库（原文/硬事实/云向量齐全），
# 欠的只是蒸馏精修——「补蒸馏债」需要 replace 语义（重放会产生第二份记忆），
# 显式后置，不在这里装样子。


# ── 嵌入腿公开 API（v20.2 起的既有契约，签名与行为逐字不变）──────────

def trip_threshold() -> int:
    return _EMBED.trip_threshold()


def recover_threshold() -> int:
    return _EMBED.recover_threshold()


def cooldown_sec() -> float:
    return _EMBED.cooldown_sec()


def record_cloud_failure(reason: str = "", *, now: Optional[float] = None) -> None:
    """云嵌入腿失败信号。closed 态连续 N 次 → 降挡；half-open 一次失败 → 回 open。"""
    _EMBED.record_failure(reason, now=now)


def record_cloud_success(*, now: Optional[float] = None) -> None:
    """云嵌入腿成功信号。half-open 连续 M 次 → 升挡；closed 态清失败计数。"""
    _EMBED.record_success(now=now)


def should_try_cloud(*, now: Optional[float] = None) -> bool:
    """v20.2.3：本地档（部署方明确选择零外部依赖）永不试云腿 ——
    档位是**配置**，熔断器是**判定**，两件事分开：这里先问配置。"""
    from ducky.engine_mode import cloud_leg_enabled
    if not cloud_leg_enabled():
        return False
    return _EMBED.should_try(now=now)


def current_mode(*, now: Optional[float] = None) -> str:
    return _EMBED.mode(now=now)


def gear_status(*, now: Optional[float] = None) -> dict:
    return _EMBED.status(now=now)


# ── LLM 腿公开 API（v20.2.2）─────────────────────────────────────────

def llm_trip_threshold() -> int:
    return _LLM.trip_threshold()


def record_llm_failure(reason: str = "", *, now: Optional[float] = None) -> None:
    """LLM 蒸馏腿失败信号。**只许由 LLMError 形态的失败调用**（信号纯净：
    FTS 崩、salience 崩等非 LLM 异常不许污染本腿计数——Y2 教训）。"""
    _LLM.record_failure(reason, now=now)


def record_llm_success(*, now: Optional[float] = None) -> None:
    _LLM.record_success(now=now)


def should_try_llm(*, now: Optional[float] = None) -> bool:
    """写入管线该不该走 LLM 蒸馏：open 态直接确定性直写（秒回），
    closed/half-open 走真实蒸馏（半开拿真实写入当探针，同命门教训）。
    v20.2.3：本地档零 token —— LLM 蒸馏整条不走。"""
    from ducky.engine_mode import cloud_leg_enabled
    if not cloud_leg_enabled():
        return False
    return _LLM.should_try(now=now)


def llm_current_mode(*, now: Optional[float] = None) -> str:
    return _LLM.mode(now=now)


def llm_gear_status(*, now: Optional[float] = None) -> dict:
    return _LLM.status(now=now)


def reset_gear_for_tests() -> None:
    from ducky.env_config import clear_config_errors_for_tests
    clear_config_errors_for_tests()
    _EMBED.reset()
    _LLM.reset()
