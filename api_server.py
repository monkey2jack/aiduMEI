"""aiduMEM 应用组装层。业务逻辑位于 ducky/ 各模块。"""
from __future__ import annotations

# ── posthog stub（省 ~23M RSS）──────────────────────────
# mem0 SDK 顶层 import posthog 做遥测，但我们不需要。
# 在 mem0 之前注入一个空壳模块，避免加载真正的 posthog 包。
# 不改 mem0 源码，升级安全。
import types as _types, os as _os

# ── .env 早期加载（v20.2.4 · 外审 F-22）────────────────────────────
#
# `ducky.utils.load_env_file()` 一直存在，但**从没有任何生产入口调用过它**
# （全仓只有测试在调）。于是照 README 复制一份 .env 再 `python api_server.py`，
# token / UI 口令可能根本不生效 —— 文档教的那条路和代码走的那条路不是同一条。
#
# 为什么内联在这里、而不是调 ducky.utils.load_env_file：**顺序契约**。
# `ducky.utils` 的 BASE_DIR / DATA_DIR 等一批常量是 **import 期求值**的，
# 一旦 `import ducky.*` 发生，再注入环境变量就晚了。所以这段必须跑在第一次
# import ducky 之前，而它又不能依赖 ducky —— 只好就地写这十几行。
# 解析规则与 ducky.utils.parse_env_file 保持一致（不覆盖已存在的变量）。
def _bootstrap_env_file() -> int:
    _here = _os.path.dirname(_os.path.abspath(__file__))
    _path = _os.environ.get("AIDUMEM_ENV_FILE") or _os.path.join(_here, ".env")
    _n = 0
    try:
        with open(_path, "r", encoding="utf-8") as _fh:
            for _line in _fh:
                _line = _line.strip()
                if not _line or _line.startswith("#") or "=" not in _line:
                    continue
                _k, _v = _line.split("=", 1)
                _k, _v = _k.strip(), _v.strip().strip('"').strip("'")
                if _k and _k not in _os.environ:
                    _os.environ[_k] = _v
                    _n += 1
    except OSError:
        return 0          # 文件不存在 / 读不了：静默，绝不拖垮启动
    return _n


_ENV_FILE_INJECTED = _bootstrap_env_file()

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
from contextlib import asynccontextmanager as _asynccontextmanager
import os
import threading
import uuid as _uuid
import time
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


@_asynccontextmanager
async def _lifespan(_app: "FastAPI"):
    """把后台能力挂进 FastAPI 生命周期（v20 · P1-6）。

    整改前，全部后台能力（建表、mem0 预热、WAL 启动对账与自愈、FTS 初始化、
    核心记忆初始化、实体词表自检、以及 `_BACKGROUND_LOOPS` 里那一批常驻线程）
    只挂在 `main()` 里。`main()` 是 `python api_server.py` 和控制台入口点走的路 ——
    但**不是唯一的路**：

        uvicorn api_server:app        # 官方文档最常见的起法
        gunicorn -k uvicorn.workers.UvicornWorker api_server:app
        任何把 `app` 当 ASGI 对象导入的进程

    这些起法完全不经过 `main()`。于是服务照样监听、`/health` 照样返回 ok、
    读写接口照样能用 —— **而 WAL 对账没跑、后台线程一个没起**。这不是崩溃，
    是「服务看着是好的，一半的能力静默缺席」（静默失败铁律）。全文此前 0 处
    `lifespan` / `on_event`，也就是说这条路上没有任何东西会告诉你缺了什么。

    `_start_background()` 本身是幂等的（`_background_started` 双检锁），所以
    `main()` 那一次调用保持原样不动 —— 两条路各自都能起，重复调用无副作用。

    v20.2.4（外审 F-01）：安全门禁也挪到了这里，且放在 `_start_background()`
    **之前** —— 拒绝启动就该在建表、预热、起线程之前发生。
    """
    _enforce_public_binding_policy()
    # v20.3.2-beta（外审 P0-3）：单进程契约与公网熔断同级，**同样在起线程之前**。
    _enforce_single_process_policy()
    # v20.3.2 正式版（用户审计 H / GLM F-3）：设了本系统**不读**的 AIDUME?_ 变量，大概率是
    # 拼错了前缀（AIDUMEM_/AIDUMEI_ 双前缀 90+ 个，分界无规律）。此前静默按默认值跑 ——
    # 作者本人同一版本踩了两次。现在启动期扫 os.environ，未知名字 WARNING + 最接近的正确名。
    from ducky.env_registry import warn_unknown_env_vars
    warn_unknown_env_vars()
    # E1（v20.3.2 正式版）：部署方自定义互斥规则（DATA_DIR/conflict_rules.json）—— 逃生阀终于接线
    from ducky.conflict_resolver import load_custom_exclusion_patterns_from_file
    try:
        _n_rules = load_custom_exclusion_patterns_from_file()
        if _n_rules:
            logger.info("🐙 已装载 %d 条部署方自定义互斥规则", _n_rules)
    except Exception as _exc:
        logger.warning("自定义互斥规则装载失败（忽略，使用内置规则）：%s", _exc)
    _start_background()
    yield
    # P2-20（v20.3.2 正式版 · Gemini P1-4）：优雅停机 —— 叫停七个循环，最多等 3s 让
    # 正在跑的一轮收尾。此前守护线程被进程退出硬切，半截巩固/反思留在库里。
    from ducky.shutdown import request_shutdown
    request_shutdown()
    _deadline = time.monotonic() + 3.0
    for _t in list(_BACKGROUND_THREADS):
        _t.join(max(0.0, _deadline - time.monotonic()))
    _alive = [t.name for t in _BACKGROUND_THREADS if t.is_alive()]
    if _alive:
        logger.warning("停机：%d 个后台循环 3s 内未退出（守护线程随进程结束）：%s", len(_alive), _alive)
    else:
        logger.info("停机：后台循环全部收尾退出")


app = FastAPI(
    lifespan=_lifespan,
    # /docs 的封面标题，是 API 侧的「品牌门面」。
    # 注意只改展示文案：logger 名、/health 的 service 字段、线程名、
    # `AIDUMEM_*` 环境变量、各模块 docstring 里的 aiduMEM 都是机器契约或历史内部名，
    # 生产侧的日志采集与监控按它们匹配，v19.4.2 决策 D2 起一律不动。
    # v20 把这条分界写成了可执行的守卫，见 tests/test_v20_brand_policy.py：
    # 露脸的字（打印/注入/--help/systemctl status/用户会打开的文档与样例）必须是
    # 当前品牌名；机器认的键一个都不动；历史记录只新增、不改写。
    # v20 uses a two-part version and intentionally has no current codename.
    # Keep the OpenAPI version machine-readable instead of coupling clients to
    # a display label that changed every mythological generation.
    title=f"aiduMEI API v{SERVICE_VERSION}",
    version=SERVICE_VERSION,
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




def _client_is_loopback(request: "Request") -> bool:
    """请求是否来自回环。

    非 IP 形态的 `client.host`（ASGI 直连测试客户端报的是 "testclient"）视为本地：
    真实网络请求的 client.host 一定是 IP，所以这条在生产里不可能被利用；而把它
    判成「非回环」会让所有不配凭据的用例集体 503 —— 那是拿测试基座换一个假的
    严格。取舍写在这里，**别把它放宽成「任意字符串都放行」**。
    拿不到来源时 fail-closed（当作非回环）。
    """
    import ipaddress
    client = getattr(request, "client", None)
    host = (getattr(client, "host", "") or "").strip()
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in ("localhost", "testclient")


_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

# ── 反代痕迹探测（v20.3.2-beta · 外审 C-1）────────────────────────────────
#
# 🔴 C-1：`_client_is_loopback` 判的是 **TCP 对端 IP**，而同机 nginx 反代的
# 对端**恒为 127.0.0.1**。于是「无凭据 → 只服务回环」这道防线，在本项目
# **标准生产形态**（域名 + 同机反代）下等于全部放行 —— 而「无凭据」正是
# 默认安装的终点（自动生成口令 source=auto 刻意不启门禁）。
#
# 判据只看代理头**在不在**，绝不看它的**值**：
#   · 值可伪造（rate_guard.py 早就写明拒绝采信 XFF 的值，那是对的）；
#   · 但**存在性不可否认** —— 这个请求经过了一跳，是客户端无法抹掉的事实。
# 无凭据实例不该服务任何经过跳转的请求，所以「有痕迹 → 拒绝」是 fail-closed。
#
# 逃生阀是 AIDUMEI_TRUST_PROXY：部署方显式声明「我知道我在反代后面」才放行。
# 宁可让反代部署者多配一个变量，也不让默认安装裸奔。
_PROXY_HINT_HEADERS = (
    "x-forwarded-for", "x-real-ip", "forwarded",
    "x-forwarded-host", "x-forwarded-proto", "x-client-ip",
)


def _request_via_proxy(headers) -> bool:
    """请求是否带反代痕迹。**只判存在性，不读值。**"""
    try:
        for name in _PROXY_HINT_HEADERS:
            v = headers.get(name)
            if v is not None and str(v).strip():
                return True
    except Exception:
        # 取不到头时 fail-closed：当作经过了代理（更严的赢）。
        return True
    return False


# ── P1-12（v20.3.2 正式版 · Codex F-04）：无凭据模式的 Host 校验 ─────────────
# 无凭据实例只服务回环 —— 但「对端是回环」拦不住 DNS rebinding：攻击页把
# attacker.example 解析到 127.0.0.1，用户的浏览器就以回环对端、攻击者 Host 打本地 API，
# 无 CORS 预检的简单请求还能直接写。所以对端判据之外再加两道只看请求头的判据：
#   ① Host 必须是回环名 / AIDUMEM_HOST / AIDUMEI_TRUSTED_HOSTS 之一（否则 421）；
#   ② 浏览器发起的写（有 Origin 或 Sec-Fetch-Site: cross-site）Origin 必须同样受信（否则 403）。
# 无 Origin 的 CLI / MCP / cron 请求不受 ② 影响；有凭据的实例由鉴权守住写面，这两道不启用；
# 声明了 AIDUMEI_TRUST_PROXY=1 时 Host 校验交给反代（反代转发的是对外域名）。
_LOOPBACK_HOST_NAMES = frozenset({"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"})


def _trusted_host_names() -> frozenset:
    extra = {h.strip().lower() for h in os.getenv("AIDUMEI_TRUSTED_HOSTS", "").split(",") if h.strip()}
    bound = (os.getenv("AIDUMEM_HOST") or "").strip().lower()
    return frozenset(_LOOPBACK_HOST_NAMES | extra | ({bound} if bound else set()))


def _host_without_port(value: str) -> str:
    h = (value or "").strip().lower()
    if h.startswith("["):            # [::1]:8767
        return h.split("]", 1)[0] + "]"
    if h.count(":") == 1:            # 127.0.0.1:8767 / localhost:8767
        return h.rsplit(":", 1)[0]
    return h                          # 裸 IPv6 或无端口


def _host_header_allowed(request: "Request") -> bool:
    if _trust_proxy_enabled():
        return True
    client_host = (getattr(getattr(request, "client", None), "host", "") or "").strip()
    if client_host == "testclient":
        # ASGI 直连测试客户端：没有 TCP 对端、没有 DNS，rebinding 不成立。
        # 与 _client_is_loopback 同一取舍（真实网络请求的 client.host 一定是 IP）。
        return True
    return _host_without_port(request.headers.get("host", "")) in _trusted_host_names()


def _browser_cross_site_write(request: "Request") -> bool:
    if request.method.upper() not in ("POST", "PUT", "PATCH", "DELETE"):
        return False
    if (request.headers.get("sec-fetch-site") or "").strip().lower() == "cross-site":
        return True
    origin = (request.headers.get("origin") or "").strip().lower()
    if not origin:
        return False                  # 非浏览器客户端不带 Origin
    if origin == "null":
        return True                   # 不透明来源（file:// 等）
    from urllib.parse import urlsplit
    return _host_without_port(urlsplit(origin).netloc) not in _trusted_host_names()


def _trust_proxy_enabled() -> bool:
    """部署方是否显式声明「本实例在可信反代之后」。"""
    return os.environ.get("AIDUMEI_TRUST_PROXY", "").strip().lower() in {
        "1", "true", "yes", "on",
    }




def _enforce_single_process_policy() -> None:
    """单进程契约由机器守住（v20.3.2-beta · 外审 P0-3 / 跨进程风险面）。

    会话表（auth._sessions）、限流窗口（rate_guard._WINDOWS）、用量计数
    （mem0_runtime._llm_usage）全是**进程内**状态 —— 这是**显式设计决策**
    （注释与文档都写明单进程部署形态，跨实例属 v21 路线图）。

    问题不在这个决策，而在**没有任何东西阻止别人违反它**：
    本文件的 docstring 就把 `gunicorn -k uvicorn.workers.UvicornWorker` 列为起法之一，
    任何人顺手加 `--workers 4` 就会得到「登录时好时坏 / 限流失灵 / 后台线程 ×4」——
    而这类症状**最难排查**，且服务看起来完全正常。

    所以判据落在**启动期**、拒绝启动、并给出唯一合规路径：与既有公网熔断同一风格。
    两个来源都看（更严的赢）：WEB_CONCURRENCY 环境变量，以及 argv 里的
    `--workers/-w`（gunicorn 的 worker 数是它自己的命令行参数，环境变量看不见它
    —— 与 F-01 的 `--host` 同理）。
    """
    def _fail(source: str, value: str) -> None:
        raise RuntimeError(
            f"🛑 [Security] 拒绝以多进程形态启动（{source}={value}）。"
            "本服务是**单进程契约**：会话表、限流窗口、用量计数均为进程内状态，"
            "多 worker 下会表现为「登录时好时坏、限流失灵」这类难排查症状。"
            "唯一合规路径：把 worker 数设为 1（WEB_CONCURRENCY=1，或去掉 --workers），"
            "后台任务如需独立进程请用 aidumem-worker 形态。"
            "跨实例共享状态属 v21 多副本路线图。"
        )

    raw = os.environ.get("WEB_CONCURRENCY", "").strip()
    if raw:
        try:
            if int(raw) > 1:
                _fail("WEB_CONCURRENCY", raw)
        except ValueError:
            # 非法值不静默：配置面出错必须出声（与 env_config 同纪律）
            logger.warning("WEB_CONCURRENCY 值非法（%r），按单进程处理", raw)

    argv = list(sys.argv[1:])
    for i, tok in enumerate(argv):
        val = None
        if tok in ("--workers", "-w") and i + 1 < len(argv):
            val = argv[i + 1]
        elif tok.startswith("--workers="):
            val = tok.split("=", 1)[1]
        if val is None:
            continue
        try:
            if int(val) > 1:
                _fail("--workers", val)
        except ValueError:
            continue


def _detect_bind_host() -> str:
    """推断**实际**监听地址（v20.2.4 · 外审 F-01）。

    `AIDUMEM_HOST` 只管我们自己的入口。而外审门槛点名的那一种起法 ——
    `uvicorn api_server:app --host 0.0.0.0` —— 里的 host 是 **uvicorn 自己的
    命令行参数**，环境变量根本看不到它。所以这里也扫一眼 argv：土，但它是
    这条路上唯一能在「开始接收请求之前」拿到的事实，而门槛要的正是这个时点。

    两个来源里**非回环的那个优先**（更严的赢）。argv 也看不到的情况
    （编程式 `uvicorn.Server(config)`）由请求期那道兜底。
    """
    env_host = os.environ.get("AIDUMEM_HOST", "").strip()
    argv_host = ""
    argv = list(sys.argv or [])
    for i, a in enumerate(argv):
        if a in ("--host", "-b", "--bind") and i + 1 < len(argv):
            argv_host = argv[i + 1].rsplit(":", 1)[0].strip().strip("[]")
        elif a.startswith("--host="):
            argv_host = a.split("=", 1)[1].strip()
        elif a.startswith("--bind="):
            argv_host = a.split("=", 1)[1].rsplit(":", 1)[0].strip()
    for h in (argv_host, env_host):
        if h and h not in _LOOPBACK_HOSTS:
            return h
    return env_host or argv_host or "127.0.0.1"


def _enforce_public_binding_policy() -> None:
    """公网裸奔门禁 —— **必须在开始接收请求之前执行**（v20.2.4 · 外审 F-01）。

    此前这段只长在 `main()` 里。而上面 `_lifespan` 的 docstring 讲的正是这条路：
    `uvicorn api_server:app` / gunicorn 把 `app` 当 ASGI 对象导入，**完全不经过
    main()**。v20 的 P1-6 认识到这条路会让后台能力静默缺席、把它们挪进了
    lifespan，却漏了同一条路上的**安全门禁** —— 于是无 token + 非回环时整个
    路由表对公网敞开，而鉴权 middleware 按设计是「没配凭据就整体关闭」。

    判据用 `_auth_enabled()`（token 或 UI 口令任一）而不是原来的 `_api_token()`：
    只配了 UI 口令的部署，middleware 其实会鉴权，原判据会误报。
    """
    host = _detect_bind_host()
    if host in ("127.0.0.1", "localhost", "::1"):
        return
    if _auth_enabled():
        return
    if os.environ.get("AIDUMEM_ALLOW_INSECURE_PUBLIC", "0").lower() in {"1", "true", "yes"}:
        logger.warning("⚠️ 已开启 AIDUMEM_ALLOW_INSECURE_PUBLIC：以不安全模式监听公网 %s", host)
        return
    logger.critical(
        "🛑 [Security Fatal] 拒绝启动：监听地址为公网/非回环 '%s' 且未配置任何凭据"
        "（AIDUMEM_API_TOKEN 或 UI 口令）。请配置凭据，或显式设置 "
        "AIDUMEM_ALLOW_INSECURE_PUBLIC=1 后重试。", host,
    )
    raise RuntimeError(
        f"Fatal Security Policy: binding to '{host}' without any credential is prohibited."
    )




@app.middleware("http")
async def _require_credentials(request: Request, call_next):
    if not _auth_enabled():
        # v20.2.4（外审 F-01 第二道）：无凭据的实例**只服务回环**。
        #
        # 配置面的检查（_enforce_public_binding_policy）拦不住一种情况：
        # `uvicorn --host 0.0.0.0` 里的 host 是 **uvicorn 自己的命令行参数**，
        # 环境变量 AIDUMEM_HOST 根本看不到它。所以这一道把判据换成**运行时
        # 事实**——请求是谁发来的。不管服务监听在哪，没配凭据就只招待本机。
        _proxied = _request_via_proxy(request.headers) and not _trust_proxy_enabled()
        if not _client_is_loopback(request) or _proxied:
            logger.critical(
                "🛑 [Security] 拒绝非回环请求：本实例未配置任何凭据"
                "（AIDUMEM_API_TOKEN 或 UI 口令），只服务回环。来源=%s 路径=%s",
                getattr(getattr(request, "client", None), "host", "?"),
                request.url.path,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "detail": (
                        "This instance has no credential configured and therefore "
                        "serves direct loopback only. Set AIDUMEM_API_TOKEN (or a UI "
                        "password) before exposing it beyond localhost. "
                        "If this request arrived through a trusted reverse proxy on "
                        "the same host, the proxy hop is what was refused: configure "
                        "a credential, or set AIDUMEI_TRUST_PROXY=1 to declare the "
                        "proxy trusted."
                    ),
                    "code": "no_credential_public_access_denied",
                },
            )
        if not _host_header_allowed(request):
            logger.warning(
                "🛑 [Security] 拒绝 Host=%s：无凭据实例只接受回环/受信主机名（防 DNS rebinding）。"
                "反代部署请配置凭据或设 AIDUMEI_TRUST_PROXY=1；自定义域名加入 AIDUMEI_TRUSTED_HOSTS。",
                request.headers.get("host", "?"))
            return JSONResponse(
                status_code=421,
                content={
                    "detail": (
                        "Host header is not a trusted name for this credential-less instance "
                        "(DNS rebinding protection). Use 127.0.0.1/localhost, add the name to "
                        "AIDUMEI_TRUSTED_HOSTS, or configure a credential."
                    ),
                    "code": "host_not_allowed",
                },
            )
        if _browser_cross_site_write(request):
            logger.warning("🛑 [Security] 拒绝跨站浏览器写请求：Origin=%s Sec-Fetch-Site=%s 路径=%s",
                           request.headers.get("origin", "-"), request.headers.get("sec-fetch-site", "-"),
                           request.url.path)
            return JSONResponse(
                status_code=403,
                content={"detail": "Cross-site browser write refused on a credential-less instance.",
                         "code": "cross_site_write_refused"},
            )
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


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    """浏览器侧兜底（v20.3.2-beta · 外审 F-4 / DeepSeek P1-6）。

    本服务自带 Web 控制台且常被放在反代之后，而全仓此前**一个安全头都没有**
    —— 与它在鉴权、注入、交付硬化上的完成度极不相称，属于低垂果实。

    CSP 必须**容纳现状**：前端有 inline style（`style-src 'unsafe-inline'`）。
    先宽后紧 —— 一个把自家控制台打成白屏的 CSP 会被下一个人直接删掉，
    那比没有 CSP 更糟。收紧 style-src 登记为 v20.4 候选（需先把 inline style 抽出）。
    """
    response = await call_next(request)
    # 已存在的值不覆盖（反代可能已经加过，部署方的显式设置优先）
    for name, value in (
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "no-referrer"),
        ("Content-Security-Policy",
         "default-src 'self'; "
         "img-src 'self' data: https:; "
         "style-src 'self' 'unsafe-inline'; "
         "script-src 'self'; "
         "connect-src 'self'; "
         "frame-ancestors 'none'; "
         "base-uri 'self'"),
    ):
        if name not in response.headers:
            response.headers[name] = value
    return response


@app.middleware("http")
async def _record_http_outcome(request: Request, call_next):
    """把每个响应的状态码记进进程内计数器（v20 · P1-9）。

    放在鉴权 middleware **外面**是刻意的：被门禁挡掉的 401 也是一次真实结局，
    事故里「凭据链断了导致满屏 401」正是要看见的形态之一。
    未处理异常按 500 计入后再抛，否则「打挂了」这一类会从统计里凭空消失。

    🔴v20.3.2 正式版（外审 GLM F-2 / Codex F-08）：**这段注释曾与事实相反。**
    Starlette 是「后注册者在更外层」，而本函数原先注册在**最前**（第 289 行），
    鉴权注册在最后 —— 于是鉴权成了最外层，它直接 return 的 401/503 **从不经过**
    这里，也不经过安全头中间件：实测 401+200 两个请求只计了 1 个，401 响应无 CSP。
    我上一轮加的四个安全头，没盖到我上一轮加的拒绝分支。
    现在三个中间件按「鉴权 → 安全头 → 计数」的**源码顺序**注册，运行时层次即
    「计数（最外）→ 安全头 → 鉴权（最内）→ 路由」。tests/test_v20_3_2_release_real_asgi.py
    用真 uvicorn + 真 socket 钉行为，用 AST 钉顺序 —— 两条都要。
    """
    # P1-9（收口后修正）：给本次请求盖借用上下文戳。contextvars 随 anyio 线程池传播到同步路由的
    # 工作线程，utils 据此把「同一请求嵌套借连接」与「上一个请求留下悬挂事务」分开 —— 后者才计数。
    from ducky.utils import BORROW_CONTEXT as _BORROW_CONTEXT
    _BORROW_CONTEXT.set(_uuid.uuid4().hex)
    from ducky import http_metrics
    try:
        response = await call_next(request)
    except Exception:
        http_metrics.record(500)
        raise
    http_metrics.record(response.status_code)
    return response

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
        initial_password_path,
        password_hash_path,
        write_initial_password,
        write_password_hash,
    )
    import secrets

    env_pwd = os.environ.get("AIDUMEM_UI_PASSWORD", "").strip()
    hash_file = password_hash_path()
    if not env_pwd and not os.path.exists(hash_file):
        gen_pwd = secrets.token_urlsafe(12)

        # v20 · P1-7：明文口令**只落 0600 文件，不进日志**。
        # 原先这里是 logger.warning(… 初始口令: %s …, gen_pwd, …)，于是每次
        # 首启都把一条可直接登录的凭据写进 journald 和 logrotate 归档 ——
        # 日志的读者范围永远大于口令的读者范围。
        #
        # 顺序是「先落明文、再落哈希」，不是反过来：
        #   · 明文先落 → 若哈希落盘失败，最坏是留下一份没生效的口令文件（噪声）；
        #   · 哈希先落 → 若明文落盘失败，口令**已经生效但没人知道它是什么**，
        #     控制台当场锁死。两种失败都要处理，但只有后者会锁死用户。
        if not write_initial_password(gen_pwd):
            logger.error(
                "🔐 [安全加固] 自动生成了控制台初始口令，但写 %s 失败 —— "
                "本次**不落哈希**，下次启动重试（避免口令已生效却无人知晓而锁死控制台）。"
                "按纪律这里绝不把明文打进日志兜底：请显式设置 AIDUMEM_UI_PASSWORD 后重启。",
                initial_password_path(),
            )
            return

        # source="auto"：自动生成的口令只守 UI 登录，不启用 API 门禁 ——
        # 否则存量部署（hermes 插件 / MCP / cron 全走回环不带凭据）
        # 会在升级瞬间集体 401。详见 ducky/security/auth.py 的 provenance 说明。
        if write_password_hash(hash_password(gen_pwd), source="auto"):
            logger.warning(
                "🔐 [安全加固] 未配置 AIDUMEM_UI_PASSWORD，已自动生成随机控制台初始口令。"
                "明文只写在 %s（权限 0600，日志中不留明文），PBKDF2 哈希在 %s。"
                "请取用后通过控制台改密，并删除该明文文件。",
                initial_password_path(),
                hash_file,
            )
        else:
            logger.error(
                "🔐 [安全加固] 初始口令明文已落 %s，但哈希落盘失败 —— 该口令并未生效。"
                "请显式设置 AIDUMEM_UI_PASSWORD 后重启，并删除那个明文文件。",
                initial_password_path(),
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

        # 🚪 v20.2.3（外审 M-1）：爆破护栏。**先查后验** —— 超限直接 429，
        # 连 PBKDF2 都不跑（省 100ms，也不给攻击者旁路信号）。
        from ducky.rate_guard import login_locked, record_login_failure
        _ip = (request.client.host if request.client else "unknown")
        _retry = login_locked(_ip)
        if _retry is not None:
            logger.warning("🚪 登录失败次数超限，暂时拒绝（%ss 后重试）", _retry)
            return JSONResponse(
                {"success": False,
                 "message": f"登录失败次数过多，请 {_retry} 秒后重试 / "
                            f"Too many failed attempts, retry in {_retry}s"},
                status_code=429, headers={"Retry-After": str(_retry)},
            )

        try:
            payload = await request.json()
        except Exception:
            payload = {}
        given = payload.get("password")
        if not isinstance(given, str) or not given:
            # 空口令也计数：否则攻击者可以拿它当免费的「探测门是否开着」。
            record_login_failure(_ip)
            return JSONResponse(
                {"success": False, "message": "密码不能为空或格式错误"}, status_code=401
            )

        # v20.3.2（外审 Gemini P1-1，实测 15.4ms）：PBKDF2 200k 轮是纯 CPU，
        # 在 async def 里同步跑会冻住整个事件循环；丢进线程池。
        import asyncio as _asyncio
        if not await _asyncio.to_thread(check_ui_password, given):
            _n = record_login_failure(_ip)
            logger.warning("🚪 UI 登录失败（密码错误，本窗口第 %d 次）", _n)
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
        # v20.2.3（外审 L-2）：默认关是对的（回环部署走 HTTP，开了 cookie
        # 根本发不出去）。但**反代之后还没开**就是真漏洞：会话 cookie 会
        # 随一次降级到 HTTP 的请求裸奔出去。请求头里带着 HTTPS 痕迹却没开
        # secure —— 这种「配置与部署形态不匹配」必须出声，不许静默。
        if not secure_flag and request.headers.get("x-forwarded-proto", "").lower() == "https":
            logger.warning(
                "🔓 检测到 HTTPS 反代（X-Forwarded-Proto: https）但 "
                "AIDUMEM_COOKIE_SECURE 未开启 —— 会话 cookie 缺少 secure 标志，"
                "遇到降级到 HTTP 的请求会明文外泄。请设 AIDUMEM_COOKIE_SECURE=1。"
            )
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
_BACKGROUND_THREADS: list = []   # P2-20（v20.3.2 正式版）：已启动的循环线程，lifespan 停机时 join

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
    try:
        from ducky.gear import ensure_half_open_probe_daemon
        ensure_half_open_probe_daemon()
    except Exception as _gear_probe_exc:
        logger.warning("⚠️ Gear 半开主动探测启动跳过: %s", _gear_probe_exc)
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
        _BACKGROUND_THREADS.append(thread)  # P2-20：停机时逐个 join
        logger.info(f"▶ {name} 后台线程已启动")

    logger.info(
        "✅ aiduMEI v%s %s 后台线程已启动 (%s 个)",
        SERVICE_VERSION,
        DISPLAY_NAME,
        len(_BACKGROUND_LOOPS),
    )


def main():
    # 🔴v20.3.2 正式版（外审 Codex F-09）：**不在这里起后台。**
    # 原先第一行就是 _start_background()，而安全策略检查在 20 行之后 ——
    # 一个最终会因公网绑定/多 worker 被拒绝启动的进程，退出前已经建表、预热、
    # 起了线程。lifespan 是后台生命周期的唯一负责人（且有 _background_started
    # 幂等标志）；main() 只做纯配置校验，然后把进程交给 uvicorn。
    host = os.environ.get("AIDUMEM_HOST", "127.0.0.1")
    # v20.2.3（外审 M-2）：端口曾是裸 int() —— 写错一个字符服务直接起不来，
    # 且报错是 ValueError 而非「端口配置无效」。回退默认 + 点名出声。
    # or 链保留原语义（新键优先、空串落到旧键），但错误要点名到**真正
    # 提供了这个值的那个 env**，不许张冠李戴。
    from ducky.env_config import int_env as _int_env
    _port_name, _port_raw = "AIDUMEM_API_PORT", os.environ.get("AIDUMEM_API_PORT")
    if not _port_raw:
        _port_name, _port_raw = "MEM0_API_PORT", os.environ.get("MEM0_API_PORT")
    port = _int_env(_port_name, 8767, minimum=1, maximum=65535, raw=_port_raw)
    if not _api_token():
        logger.warning(
            "⚠️ 未设置 AIDUMEM_API_TOKEN：REST 接口无鉴权。"
            "本机/回环使用可接受；对外部署请务必设置 token。"
        )
    env_pwd = os.environ.get("AIDUMEM_UI_PASSWORD", "").strip()
    if not env_pwd:
        logger.info("🔐 UI 登录使用 data/.ui_password_hash 安全凭据（或通过环境变量 AIDUMEM_UI_PASSWORD 配置）")
    # v20.2.4（F-01）：与 lifespan 共用**同一个**门禁实现 —— 两份拷贝
    # 早晚会长歪一份，而长歪的那份恰好是没人跑的那条路。
    _enforce_public_binding_policy()
    _enforce_single_process_policy()
    # 🔴v20.3.2 正式版（外审 GLM F-1）：**proxy_headers=False。**
    # uvicorn 默认 True → ProxyHeadersMiddleware 用 X-Forwarded-For 的**值**改写
    # request.client.host。于是 _client_is_loopback 在生产路径上读到的是伪造 IP，
    # AIDUMEI_TRUST_PROXY=1 也恒 503 —— 而 TestClient 不经过这层，9 条守卫全绿。
    # 全仓「不采信 XFF 值」的哲学，被一个默认启动参数推翻了。现在 client.host
    # 保持 TCP 对端语义；反代痕迹只看头**在不在**（_request_via_proxy），不读值。
    uvicorn.run(app, host=host, port=port, log_level="info", proxy_headers=False)


if __name__ == "__main__":
    main()
