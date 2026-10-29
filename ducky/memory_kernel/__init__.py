"""ducky.memory_kernel — 记忆内核可换层(f0.6+ · WP1)。

历史背景:aiduMEI 自 v18 起以 mem0ai 为存储内核构建;至 f0.6.3,自研层
49315 行已远超内核本体,但 64 个调用触点仍直接持有 mem0 Memory 对象
的形状。本包把「内核」从隐式依赖变成显式契约:

    调用方 ──> get_memory()(签名不变) ──> memory_kernel ──> 实现注册表
                                                    ├─ native (f0.7 起默认:单库 memory.db)
                                                    └─ mem0   (可选旧内核,pip install ".[mem0]")

纪律:
  - **行为逐字不变**:f0.6+ 只立接口不换任何语义,mem0 路径与既往单例
    完全同锁同补丁同 journal;
  - **调用方零改动**:get_memory() 签名与返回形状保持——返回对象仍是
    "长得像 Memory"的内核句柄;
  - **实现选择经 AIDUMEI_MEMORY_KERNEL**(f0.7 起默认 native;老 mem0 部署未迁移时自动留在 mem0),未知值 fail-fast;
  - ducky/ 内禁止绕过本包直接 import mem0(白名单:mem0_runtime.py 与
    impl_mem0.py——守卫 tests/test_kernel_contract.py 盲扫)。
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("aiduMEM.memory_kernel")

_ENV = "AIDUMEI_MEMORY_KERNEL"
_DEFAULT = "native"
_LEGACY_WARNED = False

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
    if raw:
        return raw
    # f0.7 P3（实验机从零安装实测）：本地档 = 没有 API key 的用户。mem0 内核没 key 写入即 503
    # （placeholder_credentials），native 内核只存原话、不调 LLM，本地档零外呼照样能写能召回。
    # 故未显式选内核时，本地档默认 native；显式设了 AIDUMEI_MEMORY_KERNEL 一律以它为准。
    from ducky.engine_mode import configured_mode
    if configured_mode() == "local":
        return "native"
    # f0.7 WP3b：默认内核改 native。唯一例外 = 老 mem0 部署升级上来、还没迁移（没有 memory.db、
    # mem0 历史库里有数据）：自动留在 mem0，否则升级后记忆「凭空消失」。迁移见 scripts/migrate_to_native.py。
    if _legacy_mem0_unmigrated():
        global _LEGACY_WARNED
        if not _LEGACY_WARNED:
            _LEGACY_WARNED = True
            logger.warning("检测到未迁移的 mem0 旧数据，本次仍用 mem0 内核。迁移到默认 native 内核："
                           "python scripts/migrate_to_native.py（见 README「从 mem0 迁移」）；"
                           "或显式设置 AIDUMEI_MEMORY_KERNEL。")
        return "mem0"
    return _DEFAULT


def _legacy_mem0_unmigrated() -> bool:
    """没有 native 的 memory.db，但 mem0 历史库存在且有记录 → 老部署未迁移。"""
    from ducky.utils import DATA_DIR
    if os.path.exists(os.path.join(DATA_DIR, "memory.db")):
        return False
    try:
        from ducky.mem0_runtime import get_history_db_path
        path = get_history_db_path()
    except (ImportError, OSError, ValueError, TypeError, KeyError):
        return False
    if not path or not os.path.isfile(path):
        return False
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            return conn.execute("SELECT 1 FROM history LIMIT 1").fetchone() is not None
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def get_memory():
    """内核句柄入口(全仓唯一;签名与历史 get_memory 逐字兼容)。

    实际句柄由注册表给出。mem0 实现内部保留与旧 mem0_runtime.get_memory
    逐字相同的单例/锁/补丁/journal 语义。
    """
    name = configured_kernel()
    impl = get_kernel_implementation(name)
    return impl()


def scope_kwargs(mem, user_id, bank_id) -> dict:
    """f0.7 P0-2:内核支持按租户定位(native)时,按 id 的 get/update/delete 一律带上已核验的租户;
    mem0 Memory 的 SDK 签名没有这两个参数,返回空 dict 保持原调用。"""
    if getattr(mem, "supports_scope", False) is True and user_id:
        return {"user_id": str(user_id), "bank_id": str(bank_id or "default")}
    return {}
