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
    # /docs 的封面标题，是 API 侧唯一的「品牌门面」。
    # 注意只改这里的展示文案：logger 名、/health 的 service 字段、
    # 各模块 docstring 里的 aiduMEM 都是机器契约或历史内部名，
    # 生产侧的日志采集与监控按它们匹配，v19.4.2 一律不动（决策 D2）。
    title=f"aiduMEI API v{SERVICE_VERSION} — {CODENAME}",
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
_api_alias = FastAPI(title="aiduMEI /api alias")
register_all_routes(_api_alias, get_memory, _get_db, _extract_entities)
app.mount("/api", _api_alias)

# ── 统一鉴权门禁（🔴P0-1 v19.4.1 鉴权贯通）──────────────────────────────
#
# v19.4.0 的问题：UI 口令与 API token 是两套互不相通的凭据，实测两种组合
# 都不可用 ——
#   · 只设 AIDUMEM_UI_PASSWORD：中间件因 token 为空整段放行，未登录直接
#     GET /api/facts → 200，全部记忆裸奔（UI 登录只是前端 sessionStorage 障眼法）；
#   · 只设 AIDUMEM_API_TOKEN：/api/login 返回 200，但前端从不发 Authorization 头，
#     登录后所有面板 → 401，控制台报废。
#
# v19.4.1 的解法：**一道门禁，两把钥匙**。
#   钥匙 A：HttpOnly session cookie（/login 成功后服务端签发，浏览器自动携带）
#   钥匙 B：Authorization: Bearer <AIDUMEM_API_TOKEN>（脚本 / MCP / CI 用）
# 任一有效即放行。门禁在「配置了 token **或** 配置了 UI 口令」时启用 ——
# 于是只设 UI 口令的部署方也真的被保护，而不是自以为被保护。
# 两者都没配时保持旧行为（本机回环零配置可用）。
#
# 注意：token 与口令状态都在**请求时**实时读取，不在模块加载时定格。
# 理由有二：① `/config/password` 可在运行时首次设置口令，门禁必须立刻生效；
# ② 模块级常量会迫使测试用 sys.modules 清洗来切换部署形态，那会连带
# 抹掉其它测试文件在 import 期做的 DB 重定向（本版施工中真实踩到）。
def _api_token() -> str:
    return os.environ.get("AIDUMEM_API_TOKEN", "").strip()


# 交互式 API 文档（/docs /redoc /openapi.json）是否免凭据。
#
# 🟡（v19.4.1 用户审计）：门禁启用后这三个路径仍返回 200，等于把 135 个
#     端点的完整清单（含参数与请求体结构）交给未授权访问者。对自托管
#     记忆库而言，这是一份现成的攻击面地图。
#     但它同时也是开发调试与接入排障的主要入口，直接锁死会明显劣化体验。
#     取舍：**门禁启用时默认一并保护**，需要公开时显式设
#     AIDUMEM_PUBLIC_DOCS=1（例如本机开发、或已在反代层另加保护）。
#     门禁未启用时（本机零配置）行为完全不变。
def _public_docs_enabled() -> bool:
    return os.environ.get("AIDUMEM_PUBLIC_DOCS", "0").strip().lower() in {"1", "true", "yes"}


_DOC_PATHS = frozenset({
    "/docs", "/api/docs",
    "/redoc", "/api/redoc",
    "/openapi.json", "/api/openapi.json",
})

# 登录与健康检查必须永久免凭据：前者是拿到凭据的唯一入口，
# 后者是监控探针的依赖，锁死会让服务「看起来挂了」。
_ALWAYS_PUBLIC_PATHS = frozenset({
    "/", "/ui",
    "/login", "/api/login",
    "/login/hint", "/api/login/hint",
    "/logout", "/api/logout",
    "/health", "/api/health",
})


def _is_public_path(path: str) -> bool:
    """无需凭据即可访问的路径（登录、健康检查、静态 UI，以及可选的文档）。"""
    if path in _ALWAYS_PUBLIC_PATHS:
        return True
    if path in _DOC_PATHS:
        return _public_docs_enabled()
    if path.startswith("/ui/"):
        return True
    return False


def _auth_enabled() -> bool:
    """门禁是否启用：配了 API token 或 UI 口令任一即启用。

    每次请求实时判定 —— `/config/password` 可在运行时首次设置口令，
    门禁必须立刻生效，不能等重启。
    """
    if _api_token():
        return True
    try:
        from ducky.security.auth import ui_password_configured
        return ui_password_configured()
    except Exception as exc:
        # 判定失败时**保守启用**门禁：宁可要求凭据，也不裸奔。
        logger.debug("门禁启用判定失败，保守启用: %s", exc)
        return True


def _request_authorized(request: Request) -> bool:
    """钥匙 A（session cookie）∨ 钥匙 B（Bearer token），任一有效即放行。"""
    from ducky.security.auth import SESSION_COOKIE_NAME, validate_session

    session_token = request.cookies.get(SESSION_COOKIE_NAME, "")
    if session_token and validate_session(session_token):
        return True

    token = _api_token()
    if token:
        supplied = request.headers.get("Authorization", "")
        if supplied and hmac.compare_digest(supplied, f"Bearer {token}"):
            return True
        # 兼容以 X-API-Token 头传递的调用方
        alt = request.headers.get("X-API-Token", "")
        if alt and hmac.compare_digest(alt, token):
            return True
    return False


@app.middleware("http")
async def _require_credentials(request: Request, call_next):
    if not _auth_enabled():
        return await call_next(request)
    if _is_public_path(request.url.path):
        return await call_next(request)
    if _request_authorized(request):
        return await call_next(request)
    return JSONResponse(
        {
            "error": "unauthorized",
            "detail": "Missing or invalid credentials: log in to the console "
                      "or send Authorization: Bearer <AIDUMEM_API_TOKEN>",
        },
        status_code=401,
    )

# ── UI 登录与会话（🔴P0-1 v19.4.1）───────────────────────────────────────
# /login 校验口令后**服务端签发会话**（HttpOnly cookie），不再只在浏览器
# sessionStorage 打个标记 —— 后者对 REST 接口毫无约束力。
# 口令统一走 ducky.security.auth：PBKDF2 存储、旧格式自动升级（P2-2）。


def _ensure_ui_password() -> None:
    """初始化控制台口令：既无环境变量又无哈希文件时，自动生成强随机口令。

    v19.4.1：哈希改用 PBKDF2-HMAC-SHA256（200k 轮），文件权限收紧 0600。
    """
    from ducky.security.auth import (
        hash_password,
        password_hash_path,
        write_password_hash,
    )
    import secrets

    env_pwd = os.environ.get("AIDUMEM_UI_PASSWORD", "").strip()
    hash_file = password_hash_path()
    if not env_pwd and not os.path.exists(hash_file):
        gen_pwd = secrets.token_urlsafe(12)
        # source="auto"：自动生成的口令只守 UI 登录，不启用 API 门禁 ——
        # 否则存量部署（hermes 插件 / MCP / cron 全走回环不带凭据）
        # 会在升级瞬间集体 401。详见 ducky/security/auth.py 的 provenance 说明。
        if write_password_hash(hash_password(gen_pwd), source="auto"):
            logger.warning(
                "🔐 [安全加固] 未配置 AIDUMEM_UI_PASSWORD，已自动生成随机控制台初始口令: %s "
                "(PBKDF2 哈希持久化至 %s，权限 0600，请妥善保存或通过控制台修改)",
                gen_pwd,
                hash_file,
            )


_ensure_ui_password()


def _register_login(route_app: FastAPI) -> None:
    @route_app.post("/login", include_in_schema=False)
    async def ui_login(request: Request):
        from ducky.security.auth import (
            SESSION_COOKIE_NAME,
            check_ui_password,
            create_session,
        )

        try:
            payload = await request.json()
        except Exception:
            payload = {}
        given = payload.get("password")
        if not isinstance(given, str) or not given:
            return JSONResponse(
                {"success": False, "message": "密码不能为空或格式错误"}, status_code=401
            )

        if not check_ui_password(given):
            logger.warning("🚪 UI 登录失败（密码错误）")
            return JSONResponse(
                {"success": False, "message": "访问密码错误 / Wrong password"},
                status_code=401,
            )

        # 🔴P0-1：签发服务端会话，浏览器后续请求靠它自证身份。
        # HttpOnly 阻断 JS 读取（防 XSS 窃取）；SameSite=Lax 防基础 CSRF。
        # secure 标志按部署形态决定：HTTPS 反代后设 AIDUMEM_COOKIE_SECURE=1。
        token, ttl = create_session()
        secure_flag = os.environ.get("AIDUMEM_COOKIE_SECURE", "0").strip().lower() in {
            "1", "true", "yes",
        }
        resp = JSONResponse({"success": True, "expires_in": ttl})
        resp.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=token,
            max_age=ttl,
            httponly=True,
            samesite="lax",
            secure=secure_flag,
            path="/",
        )
        logger.info("🚪 UI 登录成功（已签发会话，有效期 %d 秒）", ttl)
        return resp

    @route_app.post("/logout", include_in_schema=False)
    async def ui_logout(request: Request):
        """登出：撤销服务端会话并清 cookie（会话可撤销是 cookie 方案的要点）。"""
        from ducky.security.auth import SESSION_COOKIE_NAME, revoke_session

        token = request.cookies.get(SESSION_COOKIE_NAME, "")
        revoked = revoke_session(token) if token else False
        resp = JSONResponse({"success": True, "revoked": revoked})
        resp.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
        return resp

    @route_app.get("/login/hint", include_in_schema=False)
    async def ui_login_hint():
        # 安全修复：任何情况下都不把密码明文吐给前端。
        # 未设置 AIDUMEM_UI_PASSWORD 时启动日志会提醒部署方自行设置。
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
    # 📼 v19.4.0 明镜工程 Phase 1: Verbatim Vault 原文保真层建表（幂等，失败降级）
    try:
        from ducky.verbatim_vault import ensure_verbatim_schema
        ensure_verbatim_schema()
    except Exception as _vs:
        logger.warning(f"📼 Verbatim Vault 建表跳过（主服务仍会启动）: {_vs}")
    init_core_memory()
    # 启动 WAL 对账与自愈（v19.2.0 P0-DATA）
    try:
        from ducky.wal_engine import reconcile_startup
        _rec_report = reconcile_startup()
        if _rec_report.get("recovered", 0) > 0:
            logger.info("🔧 [WAL Reconcile] 成功自愈恢复 %d 条挂起事务", _rec_report["recovered"])
    except Exception as _re:
        logger.warning(f"⚠️ WAL 启动对账异常: {_re}")

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
    if not _api_token():
        logger.warning(
            "⚠️ 未设置 AIDUMEM_API_TOKEN：REST 接口无鉴权。"
            "本机/回环使用可接受；对外部署请务必设置 token。"
        )
    env_pwd = os.environ.get("AIDUMEM_UI_PASSWORD", "").strip()
    if not env_pwd:
        logger.info("🔐 UI 登录使用 data/.ui_password_hash 安全凭据（或通过环境变量 AIDUMEM_UI_PASSWORD 配置）")
    if host not in ("127.0.0.1", "localhost") and not _api_token():
        allow_insecure = os.environ.get("AIDUMEM_ALLOW_INSECURE_PUBLIC", "0").lower() in {"1", "true", "yes"}
        if not allow_insecure:
            logger.critical(
                "🛑 [Security Fatal] 拒绝启动：监听地址为公网/非回环 '%s' 且未配置 AIDUMEM_API_TOKEN。"
                "为防止未授权公网裸奔，请在 .env 中设置 AIDUMEM_API_TOKEN 或显式设置 AIDUMEM_ALLOW_INSECURE_PUBLIC=1 后重试。",
                host
            )
            raise RuntimeError(f"Fatal Security Policy: Binding to '{host}' without AIDUMEM_API_TOKEN is prohibited.")
        else:
            logger.warning("⚠️ 已开启 AIDUMEM_ALLOW_INSECURE_PUBLIC：以不安全模式监听公网 %s", host)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
