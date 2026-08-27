"""ducky.env_config — env 数值解析的单一真相源（v20.2.3 · 外审 M-2）

**保命纪律**：非法 env 值一律**回退默认 + 出声一次 + 探针可查**，绝不 raise。

这条纪律 v20.2.1 已在挡位切换器与限流护栏上立过（外审 R1「配置雷」），
但当时只拆了那两处的雷 —— auth / scoring / injection_guard / api_server
里的裸 `int(os.environ.get(...))` 一直埋着，且多数炸在 **import 期**：
一个配置笔误让整个服务起不来，比 R1 原案更狠。外审 M-2 点名了其中两处，
自查普查出六处，本模块把它们全部收编。

为什么是叶子模块（只 import os / logging，绝不 import 任何 ducky 子模块）：
配置解析被 auth 这类底层安全模块在 import 期调用，任何内部依赖都可能
织出循环导入 —— 而循环导入的症状恰恰又是「服务起不来」，等于用新的
启动阻断换掉旧的启动阻断。

「非法值不静默」的老纪律没有丢：它从「炸」改成了「可观测」——
warning 打一次（同值不刷屏），错误常驻 config_errors()，进 /health 探针。
"""
from __future__ import annotations

import logging
import math
import os
from typing import Callable, Optional

logger = logging.getLogger("aiduMEM.env_config")

_errors: dict[str, str] = {}


def _resolve(name: str, default, caster: Callable, valid: Callable,
             *, raw: Optional[str] = None, expects: str = "") -> object:
    if raw is None:
        raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        _errors.pop(name, None)
        return default
    try:
        v = caster(str(raw).strip())
        # v20.2.3（外审 A-1）：**先拦非有限值**。NaN 与任何数比较恒为 False，
        # 于是 `not (v < min or v > max)` 对 NaN 恒真 —— NaN 被判「合法」，
        # 静默通过、不进 _errors、不打 warning、探针零痕迹。这正是本模块
        # 头注自己定义的「假绿灯」形态，而且它埋在**专门用来拆配置雷的
        # 模块**里。下游是真有杀伤的：RERANK_WEIGHT=nan 会让融合分
        # `old*(1-W) + rr*W` 整体变 NaN，排序彻底失效而全系统报健康。
        # `1e999` 解析成 inf，无上限的参数同样拦不住 —— 一并由此拦下。
        # int 路径天然免疫（int("nan") 直接 ValueError），但判据放在共用
        # 层是刻意的：将来新增任何 caster 都自动受保护，不靠下一个人记得。
        if not math.isfinite(v):
            raise ValueError
        if not valid(v):
            raise ValueError
        _errors.pop(name, None)
        return v
    except (ValueError, TypeError):
        msg = (f"{name} 非法值 {raw!r}"
               + (f"（需{expects}）" if expects else "")
               + f"，已回退默认 {default}")
        if _errors.get(name) != msg:
            logger.warning("⚙️ %s", msg)
        _errors[name] = msg
        return default


def int_env(name: str, default: int, *, minimum: Optional[int] = None,
            maximum: Optional[int] = None, raw: Optional[str] = None) -> int:
    """整数 env。越界与不可解析同等处理：回退默认 + 出声。"""
    def _valid(v: int) -> bool:
        return not ((minimum is not None and v < minimum)
                    or (maximum is not None and v > maximum))
    bounds = []
    if minimum is not None:
        bounds.append(f">={minimum}")
    if maximum is not None:
        bounds.append(f"<={maximum}")
    return int(_resolve(name, default, int, _valid, raw=raw,
                        expects="有限整数" + (" " + " 且 ".join(bounds) if bounds else "")))


def float_env(name: str, default: float, *, minimum: Optional[float] = None,
              maximum: Optional[float] = None,
              exclusive_minimum: Optional[float] = None,
              raw: Optional[str] = None) -> float:
    """浮点 env。语义同 int_env，另有 exclusive_minimum（严格大于）。

    v20.2.3（外审 A-2）：加 exclusive_minimum 是为了让调用方能表达**真正的
    `v > 0`**。此前 ducky/gear.py 拿 `minimum=0.000001` 近似它，于是区间
    (0, 1e-6) 的合法旧值被拒 —— 收编时宣称的「公开行为逐字不变」就不逐字了。
    影响接近于零（没人给熔断器配亚微秒冷却），但**旗立到「逐字」，实况就
    该经得起逐字**，所以补的是能力而不是措辞。
    （不设 exclusive_maximum：当下无调用方需要，需要时再加——不预造抽象。）
    """
    def _valid(v: float) -> bool:
        if exclusive_minimum is not None and v <= exclusive_minimum:
            return False
        return not ((minimum is not None and v < minimum)
                    or (maximum is not None and v > maximum))
    bounds = []
    if exclusive_minimum is not None:
        bounds.append(f">{exclusive_minimum}")
    if minimum is not None:
        bounds.append(f">={minimum}")
    if maximum is not None:
        bounds.append(f"<={maximum}")
    return float(_resolve(name, default, float, _valid, raw=raw,
                          expects="有限数值" + (" " + " 且 ".join(bounds) if bounds else "")))


def config_errors(*names: str) -> dict[str, str]:
    """当前生效的配置错误。不传参=全部；传名字=只看这几个（各模块的
    探针只对自己那几个 env 负责，不越界替别人报警）。"""
    if not names:
        return dict(_errors)
    return {k: v for k, v in _errors.items() if k in names}


def clear_config_errors_for_tests() -> None:
    _errors.clear()
