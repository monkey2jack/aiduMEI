"""
ducky.extended — 扩展路由 + 自动记忆（v9.1 一次到位）
"""
from ducky.extended.auto_memory import (
    AUTO_MEMORY_STATE,
    _auto_expire_loop,
    _run_auto_memory,
    _wrapper_auto_memory,
    auto_memory_background_loop,
    bind_runtime,
)
from ducky.extended.routes import register_extended_routes

__all__ = [
    "register_extended_routes",
    "auto_memory_background_loop",
    "_auto_expire_loop",
    "_run_auto_memory",
    "_wrapper_auto_memory",
    "AUTO_MEMORY_STATE",
    "bind_runtime",
]
