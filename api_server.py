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
import hmac
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from ducky.autodream import autodream_background_loop
from ducky.evolve_mem import evolve_background_loop
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
)
from ducky.mem0_runtime import get_memory
from ducky.reflect import reflect_background_loop
from ducky.routes_registry import register_all_routes
from ducky.schema_bootstrap import ensure_core_schema
from ducky.text_fts import _init_text_fts
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

# ── 前端 UI 托管（aiduMEM 自带面板）──────────────────────
# UI_DIR 指向 frontend/ 目录；未指定时取本文件同级的 frontend/。
# 访问 / 与 /ui/ 即可打开控制台，页面通过 /api/* 与本服务通信。
_UI_DIR = os.environ.get("UI_DIR", str(Path(__file__).resolve().parent / "frontend"))
if Path(_UI_DIR).is_dir():
    app.mount("/ui", StaticFiles(directory=_UI_DIR, html=True), name="ui")

    @app.get("/", include_in_schema=False)
    def _ui_root():
        return RedirectResponse("/ui/")

    logger.info("🖥️ 前端 UI 已挂载: %s → /ui/", _UI_DIR)
else:
    logger.warning("⚠️ 未找到前端目录 %s（仅 API 模式运行）", _UI_DIR)

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

# ── /api 前缀别名层 ──────────────────────────────────────
# aiduMEI 控制台前端以 /api/* 为调用根（API.base = '/api'）。
# 这里挂一个子应用，复用同一套路由，让 /api/stats、/api/config 等
# 直接命中扁平路由，无需改前端。
_api_alias = FastAPI(title="aiduMEM /api alias")
register_all_routes(_api_alias, get_memory, _get_db, _extract_entities)
app.mount("/api", _api_alias)

# ── UI 登录 ─────────────────────────────────────────────
# 控制台登录门禁：/api/login 校验访问密码（AIDUMEM_UI_PASSWORD）。
# 密码只来自环境变量，不入仓库；未配置时回退到开源默认密码 123456。
_DEFAULT_UI_PASSWORD = "123456"
_UI_PASSWORD = _os.environ.get("AIDUMEM_UI_PASSWORD") or _DEFAULT_UI_PASSWORD


def _register_login(route_app: FastAPI) -> None:
    @route_app.post("/login", include_in_schema=False)
    async def ui_login(request: Request):
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        given = payload.get("password")
        if isinstance(given, str) and hmac.compare_digest(given, _UI_PASSWORD):
            logger.info("🚪 UI 登录成功")
            return {"success": True}
        logger.warning("🚪 UI 登录失败（密码错误）")
        return JSONResponse(
            {"success": False, "message": "访问密码错误 / Wrong password"},
            status_code=401,
        )

    @route_app.get("/login/hint", include_in_schema=False)
    async def ui_login_hint():
        # 默认密码时前端提示 123456；部署方已自定义则不提示。
        if _UI_PASSWORD == _DEFAULT_UI_PASSWORD:
            return {"hint": "默认密码 / Default PIN: 123456"}
        return {"hint": None}


_register_login(app)
_register_login(_api_alias)

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
        "evolve_mem": evolve_background_loop,
    "reflect": reflect_background_loop,
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
