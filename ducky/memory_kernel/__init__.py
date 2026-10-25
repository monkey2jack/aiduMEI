"""ducky.memory_kernel — 记忆内核可换层(f0.6+ · WP1)。

历史背景:aiduMEI 自 v18 起以 mem0ai 为存储内核构建;至 f0.6.3,自研层
49315 行已远超内核本体,但 64 个调用触点仍直接持有 mem0 Memory 对象
的形状。本包把「内核」从隐式依赖变成显式契约:

    调用方 ──> get_memory()(签名不变) ──> memory_kernel ──> 实现注册表
                                                    ├─ mem0   (现行,冻结 2.2.1)
                                                    └─ native (规划中,stub)

纪律:
  - **行为逐字不变**:f0.6+ 只立接口不换任何语义,mem0 路径与既往单例
    完全同锁同补丁同 journal;
  - **调用方零改动**:get_memory() 签名与返回形状保持——返回对象仍是
    "长得像 Memory"的内核句柄;
  - **实现选择经 AIDUMEI_MEMORY_KERNEL**(默认 mem0),未知值 fail-fast;
  - ducky/ 内禁止绕过本包直接 import mem0(白名单:mem0_runtime.py 与
    impl_mem0.py——守卫 tests/test_kernel_contract.py 盲扫)。
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("aiduMEM.memory_kernel")

_ENV = "AIDUMEI_MEMORY_KERNEL"
_DEFAULT = "mem0"

from ducky.memory_kernel.base import KernelContract, kernel_not_implemented
from ducky.memory_kernel.registry import available_kernels, get_kernel_implementation

__all__ = [
    "KernelContract",
    "kernel_not_implemented",
    "available_kernels",
    "get_kernel_implementation",
    "get_memory",
    "configured_kernel",
]


def configured_kernel() -> str:
    """部署选定的内核名。未知值一律 fail-fast——内核选错是部署事故,
    静默回退会让部署在毫不知情的情况下被换掉记忆引擎。"""
    raw = (os.environ.get(_ENV) or "").strip().lower()
    return raw or _DEFAULT


def get_memory():
    """内核句柄入口(全仓唯一;签名与历史 get_memory 逐字兼容)。

    实际句柄由注册表给出。mem0 实现内部保留与旧 mem0_runtime.get_memory
    逐字相同的单例/锁/补丁/journal 语义。
    """
    name = configured_kernel()
    impl = get_kernel_implementation(name)
    return impl()
