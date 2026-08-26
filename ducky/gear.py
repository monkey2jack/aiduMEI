"""ducky.gear — 挡位切换器（v20.2 WP-G · 智慧引擎自动挡）

标准熔断器三态机，管「云嵌入腿」这一个信号源：
    closed（full 挡）--连续 N 次失败--> open（lite 挡）
    open --冷却 T 秒--> half-open（放探测）
    half-open --连续 M 次成功--> closed；任一失败 --> open（重新冷却）

防抖是灵魂：**假恢复（单次侥幸成功）不许过早升挡** —— half-open 需要
连续 M 次成功才回 full；反方向，偶发一次超时也不该立刻降挡（N 次连续
失败才降）。参数默认 N=3 / M=2 / T=60s：工程惯例值，**显式标注待生产
故障分布校准**（与召回阈值 0.46 的校准流程同款 —— 先诚实默认，
拿到真实分布再定，绝不冒充「算出来的」）。

纪律：
  - 升降挡是**事件**，进事件账本（actor=gear_shifter）——「哪段时间
    跑在备胎上」必须审计可查，这是挡位诚实化（WP-H）的地基。
  - 状态存进程内存：重启回 closed（full）重新试探是合理语义 ——
    重启本身就是一次「重新认识世界」。
  - 本模块只做判定，不做嵌入：失败/成功信号由真正调用云嵌入的位点
    上报（record_cloud_failure / record_cloud_success）。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger("aiduMEM.gear")

_FAIL_ENV = "AIDUMEI_GEAR_TRIP_FAILURES"      # closed→open 连续失败数
_RECOVER_ENV = "AIDUMEI_GEAR_RECOVER_SUCCESSES"  # half-open→closed 连续成功数
_COOLDOWN_ENV = "AIDUMEI_GEAR_COOLDOWN_SEC"   # open→half-open 冷却秒
_DEFAULT_TRIP = 3
_DEFAULT_RECOVER = 2
_DEFAULT_COOLDOWN = 60.0

_LOCK = threading.Lock()
_state = "closed"            # closed | open | half_open
_consec_failures = 0
_consec_successes = 0
_opened_at: Optional[float] = None
_last_reason = ""
_shift_count = 0


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = int(raw.strip())
        if v < 1:
            raise ValueError
        return v
    except ValueError:
        raise ValueError(f"{name} 非法值 {raw!r}：需 >=1 的整数")


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = float(raw.strip())
        if v <= 0:
            raise ValueError
        return v
    except ValueError:
        raise ValueError(f"{name} 非法值 {raw!r}：需 >0 的秒数")


def trip_threshold() -> int:
    return _int_env(_FAIL_ENV, _DEFAULT_TRIP)


def recover_threshold() -> int:
    return _int_env(_RECOVER_ENV, _DEFAULT_RECOVER)


def cooldown_sec() -> float:
    return _float_env(_COOLDOWN_ENV, _DEFAULT_COOLDOWN)


def _ledger_shift(direction: str, reason: str) -> None:
    """升降挡进事件账本；账本失败不拖垮换挡（换挡是保命动作）。"""
    try:
        from ducky.event_ledger import ensure_ledger_schema, record_event
        from ducky.utils import get_facts_conn
        ensure_ledger_schema()
        conn = get_facts_conn()
        try:
            record_event(conn, actor="gear_shifter", action=direction,
                         target_id="engine_mode", reason=reason[:200])
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("换挡事件记账失败（不拖垮换挡）: %s", exc)


def record_cloud_failure(reason: str = "", *, now: Optional[float] = None) -> None:
    """云嵌入腿失败信号。closed 态连续 N 次 → 降挡；half-open 一次失败 → 回 open。"""
    global _state, _consec_failures, _consec_successes, _opened_at, _last_reason, _shift_count
    t = time.time() if now is None else now
    with _LOCK:
        _consec_successes = 0
        if _state == "half_open":
            _state = "open"
            _opened_at = t
            _last_reason = f"半开探测失败：{reason[:120]}"
            _shift_count += 1
            logger.warning("⚙️ 挡位：半开探测失败，回到 lite（%s）", reason[:120])
            _ledger_shift("downshift", _last_reason)
            return
        if _state == "open":
            return
        _consec_failures += 1
        if _consec_failures >= trip_threshold():
            _state = "open"
            _opened_at = t
            _last_reason = f"云嵌入连续 {_consec_failures} 次失败：{reason[:120]}"
            _shift_count += 1
            logger.warning("⚙️ 挡位：降挡 full→lite（%s）", _last_reason)
            _ledger_shift("downshift", _last_reason)


def record_cloud_success(*, now: Optional[float] = None) -> None:
    """云嵌入腿成功信号。half-open 连续 M 次 → 升挡；closed 态清失败计数。"""
    global _state, _consec_failures, _consec_successes, _last_reason, _shift_count
    with _LOCK:
        _consec_failures = 0
        if _state == "closed":
            return
        if _state == "half_open":
            _consec_successes += 1
            if _consec_successes >= recover_threshold():
                _state = "closed"
                _consec_successes = 0
                _last_reason = "半开探测连续成功，云嵌入恢复"
                _shift_count += 1
                logger.info("⚙️ 挡位：升挡 lite→full（%s）", _last_reason)
                _ledger_shift("upshift", _last_reason)
                _spawn_replay()


def _spawn_replay() -> None:
    """升挡后台重放欠账（lite 挡期间的蒸馏与本地补写）。守护线程，
    失败留账下轮——重放绝不阻塞升挡本身。"""
    def _run():
        try:
            from ducky.dual_index import replay_pending
            report = replay_pending(apply=True)
            logger.info("⚙️ 升挡欠账重放：%s", report)
        except Exception as exc:
            logger.warning("升挡欠账重放失败（留账）: %s", exc)
    threading.Thread(target=_run, name="gear-replay", daemon=True).start()


def should_try_cloud(*, now: Optional[float] = None) -> bool:
    """本次请求该不该试云腿：closed 当然试；**half-open 也试** ——
    半开的语义就是拿真实流量当探针（不试探，成功信号永远不来，
    系统会卡死在备胎挡：断供演练首跑抓出的死锁，切换逻辑的命门）。
    open 态不试（冷却中，别去撞还没好的服务）。"""
    global _state
    t = time.time() if now is None else now
    with _LOCK:
        if _state == "open" and _opened_at is not None and t - _opened_at >= cooldown_sec():
            _state = "half_open"
            logger.info("⚙️ 挡位：冷却结束，进入半开探测")
        return _state in ("closed", "half_open")


def current_mode(*, now: Optional[float] = None) -> str:
    """当前挡位：'full' | 'lite'。open 态冷却到点自动转 half-open
    （half-open 仍报 lite —— 探测成功之前不许对外宣称恢复）。"""
    global _state
    t = time.time() if now is None else now
    with _LOCK:
        if _state == "open" and _opened_at is not None and t - _opened_at >= cooldown_sec():
            _state = "half_open"
            logger.info("⚙️ 挡位：冷却结束，进入半开探测")
        return "full" if _state == "closed" else "lite"


def gear_status(*, now: Optional[float] = None) -> dict:
    """/health 探针：挡位、熔断器内态、参数生效值、最近换挡原因。"""
    mode = current_mode(now=now)
    with _LOCK:
        return {
            "mode": mode,
            "breaker": _state,
            "consecutive_failures": _consec_failures,
            "trip_threshold": trip_threshold(),
            "recover_threshold": recover_threshold(),
            "cooldown_sec": cooldown_sec(),
            "last_shift_reason": _last_reason or None,
            "shift_count": _shift_count,
        }


def reset_gear_for_tests() -> None:
    global _state, _consec_failures, _consec_successes, _opened_at, _last_reason, _shift_count
    with _LOCK:
        _state = "closed"
        _consec_failures = 0
        _consec_successes = 0
        _opened_at = None
        _last_reason = ""
        _shift_count = 0
