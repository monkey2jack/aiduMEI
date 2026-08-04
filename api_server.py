"""aiduMEM 应用组装层。业务逻辑位于 ducky/ 各模块。"""
from __future__ import annotations

# ── posthog stub（省 ~23M RSS）──────────────────────────
# mem0 SDK 顶层 import posthog 做遥测，但我们不需要。
# 在 mem0 之前注入一个空壳模块，避免加载真正的 posthog 包。
# 不改 mem0 源码，升级安全。
import types as _types, os as _os
_os.environ.setdefault("MEM0_TELEMETRY", "false")
_stub = _types.ModuleType("posthog")
class _NoopPosthog:
    """Lightweight posthog stub — all calls are silent no-ops."""
    def __init__(self, *a, **kw): pass
    def capture(self, *a, **kw): pass
    def shutdown(self, *a, **kw): pass
    def evaluate_flags(self, *a, **kw): return {}
    def feature_enabled(self, *a, **kw): return False
_stub.Posthog = _NoopPosthog
import sys
sys.modules["posthog"] = _stub
del _stub, _NoopPosthog, _types
# ── end posthog stub ──────────────────────────────────

import logging
import os
import threading

import uvicorn
from fastapi import FastAPI

from ducky.autodream import autodream_background_loop
from ducky.core_memory import init_core_memory
from ducky.extended import _auto_expire_loop, auto_memory_background_loop
from ducky.extended.routes import register_extended_routes
from ducky.hot.health import set_version_info
from ducky.hot.legacy import (
    _background_consolidation_loop,
    _background_scene_cluster_loop,
    _extract_entities,
    _extract_key_facts,
    _get_db,
    _init_text_fts,
)
from ducky.mem0_runtime import get_memory
from ducky.routes_registry import register_all_routes
from ducky.schema_bootstrap import ensure_core_schema
from ducky.utils import LOG_DIR
from ducky.version import SERVICE_VERSION, CODENAME, CODENAME_ZH, DISPLAY_NAME

_os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(_os.path.join(LOG_DIR, "api_server.log")),
        logging.StreamHandler(sys.stderr),
    ],
)
logger = logging.getLogger(f"aiduMEM-v{SERVICE_VERSION}")

app = FastAPI(
    title=f"aiduMEM API v{SERVICE_VERSION} — {CODENAME}",
    version=f"{SERVICE_VERSION}-{CODENAME.lower()}",
)

# 兼容旧模块仍从 api_server 导入这些符号。
__all__ = [
    "app",
    "get_memory",
    "_extract_entities",
    "_extract_key_facts",
    "_get_db",
]

# 注册所有路由（统一入口）
register_all_routes(app, get_memory, _get_db, _extract_entities)

# 注入版本信息到 health 端点（唯一真相源）
set_version_info(SERVICE_VERSION, CODENAME, CODENAME_ZH)

_background_started = False
_background_lock = threading.Lock()
_BACKGROUND_LOOPS = {
    "consolidation": _background_consolidation_loop,
    "scene_cluster": _background_scene_cluster_loop,
    "auto_memory": auto_memory_background_loop,
    "auto_expire": _auto_expire_loop,
    "autodream": autodream_background_loop,
}


def _start_background() -> None:
    """幂等启动后台循环并初始化存储。"""
    global _background_started
    with _background_lock:
        if _background_started:
            return
        _background_started = True

    # 核心表建表必须最先做：facts/entities 是所有功能的地基，
    # 全新克隆时它们还不存在（v14 Aegis 起由代码建，不再依赖手工部署）。
    ensure_core_schema()

    try:
        get_memory()
        logger.info("🧠 mem0 单例预热完成")
    except Exception as exc:
        logger.warning(f"⚠️ mem0 预热失败（主服务仍会启动）: {exc}")

    _init_text_fts()
    init_core_memory()

    # 启动自检：实体词表漏配是「静默故障」——闸门会把涉及自定义人名/
    # 项目代号的查询判成 no_signal 而零召回，不报错也不留痕。v15 起
    # 在启动日志里显式告警，别再让部署方自己去猜为什么查不到。
    try:
        from ducky.pipeline.memory_gate import entity_keywords_status
        _ek = entity_keywords_status()
        if _ek["configured"]:
            logger.info("🎯 相关性闸门实体词表已加载：%d 个词条", _ek["count"])
        else:
            logger.warning(
                "⚠️ %s 未配置 —— 涉及自定义人名/项目代号的查询会被闸门判为"
                " no_signal 并静默零召回。请参考 .env.example 配置后重启服务。",
                _ek["env_var"],
            )
    except Exception as exc:
        logger.warning("⚠️ 实体词表自检失败: %s", exc)

    for name, loop_fn in _BACKGROUND_LOOPS.items():
        thread = threading.Thread(
            target=loop_fn,
            daemon=True,
            name=f"aiduMEM-{name}",
        )
        thread.start()
        logger.info(f"▶ {name} 后台线程已启动")

    logger.info(
        "✅ aiduMEM v%s %s 后台线程已启动 (%s 个)",
        SERVICE_VERSION,
        CODENAME,
        len(_BACKGROUND_LOOPS),
    )


def main():
    _start_background()
    host = os.environ.get("AIDUMEM_HOST", "127.0.0.1")
    port = int(os.environ.get("AIDUMEM_API_PORT") or os.environ.get("MEM0_API_PORT") or 8767)
    if host != "127.0.0.1":
        logger.warning(
            "⚠️ 监听地址为 %s（非回环）。aiduMEM 自身不做鉴权，"
            "请确保前置反向代理已配置认证与 TLS。", host
        )
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
