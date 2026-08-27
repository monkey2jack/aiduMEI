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
                        expects="整数" + (" " + " 且 ".join(bounds) if bounds else "")))


def float_env(name: str, default: float, *, minimum: Optional[float] = None,
              maximum: Optional[float] = None, raw: Optional[str] = None) -> float:
    """浮点 env。语义同 int_env。"""
    def _valid(v: float) -> bool:
        return not ((minimum is not None and v < minimum)
                    or (maximum is not None and v > maximum))
    bounds = []
    if minimum is not None:
        bounds.append(f">={minimum}")
    if maximum is not None:
        bounds.append(f"<={maximum}")
    return float(_resolve(name, default, float, _valid, raw=raw,
                          expects="数值" + (" " + " 且 ".join(bounds) if bounds else "")))


def config_errors(*names: str) -> dict[str, str]:
    """当前生效的配置错误。不传参=全部；传名字=只看这几个（各模块的
    探针只对自己那几个 env 负责，不越界替别人报警）。"""
    if not names:
        return dict(_errors)
    return {k: v for k, v in _errors.items() if k in names}


def clear_config_errors_for_tests() -> None:
    _errors.clear()
