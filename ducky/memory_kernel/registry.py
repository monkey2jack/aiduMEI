"""内核实现注册表。

注册表是"名字 → 零参工厂"的映射。mem0 实现入场时**不触发初始化**
(工厂内部才决定何时建单例)——import 本包没有任何副作用,这是
「import 时间零行为」纪律的要求。
"""
from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger("aiduMEM.memory_kernel")

_IMPLS: dict[str, Callable[[], object]] = {}
_REGISTERED: set[str] = set()


def register(name: str, factory: Callable[[], object], *, complete: bool = True) -> None:
    """注册实现。complete=False 表示"已登记未完工"(选到即 fail-fast)。"""
    _IMPLS[name] = factory
    if complete:
        _REGISTERED.add(name)


def available_kernels() -> dict[str, str]:
    """{name: "ready" | "planned"} — /health 与诊断面用。"""
    return {n: ("ready" if n in _REGISTERED else "planned") for n in _IMPLS}


def get_kernel_implementation(name: str) -> Callable[[], object]:
    if name not in _IMPLS:
        raise ValueError(
            f"未知记忆内核 {name!r}(可用: {sorted(_IMPLS)});"
            f"检查 AIDUMEI_MEMORY_KERNEL 拼写——内核选错是部署事故,不静默回退"
        )
    if name not in _REGISTERED:
        from ducky.memory_kernel.base import KernelNotImplemented
        raise KernelNotImplemented(name)
    return _IMPLS[name]


def _register_defaults() -> None:
    """注册两个内建实现。放函数里避免 import 环(impl_mem0 反向依赖
    mem0_runtime,而 mem0_runtime 被 ducky 几乎所有模块 import)。"""
    if "mem0" in _IMPLS:
        return
    from ducky.memory_kernel.impl_mem0 import create_mem0_kernel
    register("mem0", create_mem0_kernel, complete=True)
    from ducky.memory_kernel.base import kernel_not_implemented
    register("native", kernel_not_implemented("native"), complete=False)


_register_defaults()
