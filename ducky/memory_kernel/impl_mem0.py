"""mem0 内核实现——f0.6+ 行为保持的唯一现行实现。

关键裁定:**不再复制一份 get_memory 逻辑**,而是直接转调
ducky.mem0_runtime.get_memory()。那边 180 行的初始化(配置校验/沙箱
断言/锁清理/usage 补丁/speed 补丁/journal 安装/sys 单例钉)是 f0.6.3
生产正在跑的代码——本包若复制,两份逻辑必然漂移,「行为逐字不变」就
是句空话。转调=同一份代码、同一个单例、同一个锁,零语义分叉。

本文件唯一的增量价值是「作为 kernel 注册表的一个实现存在」,让
AIDUMEI_MEMORY_KERNEL=mem0 显式可写、未来 native 有对立面。
"""
from __future__ import annotations

import logging

logger = logging.getLogger("aiduMEM.memory_kernel")


def create_mem0_kernel():
    """零参工厂:返回 mem0 单例句柄(与历史 get_memory 语义逐字一致)。"""
    from ducky.mem0_runtime import get_memory as _legacy_get_memory
    return _legacy_get_memory()
