#!/usr/bin/env python3
"""
aiduMEM MCP Server — 版本随 ducky.version 真相源（见 SERVICE_VERSION）
==============================================
通过 stdio/SSE 模式暴露 aiduMEM 工具给宿主 Agent。

架构说明：
  MCP 工具统一通过 HTTP 调用本地 api_server（默认 127.0.0.1:8767），
  不再直接 import ducky 内部模块，实现完全解耦：
    - api_server 可独立重启/升级，MCP 无需重启
    - 减少 Qdrant 锁冲突风险（只有一个进程持有锁）
    - 工具接口与 REST API 保持一致，便于测试

后台自动记忆线程仍走宿主会话库直读（无需 HTTP），保持低延迟。

环境变量（均可选）：
    AIDUMEM_HOST_STATE_DB    宿主 Agent 的会话 SQLite 路径
    AIDUMEM_HOST_LAST_ID     增量游标文件路径
    AIDUMEM_API_BASE         api_server 地址（默认 http://127.0.0.1:8767）
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import argparse
import threading
import time

import httpx
from pathlib import Path
from typing import Any

# ── 路径 bootstrap（先于 ducky import）──
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ducky.utils import DATA_DIR, DEFAULT_USER_ID, LOG_DIR, api_auth_headers
# 🔴v20.3.2 正式版（外审 Gemini P0-3）：MCP 9 个记忆/事实工具原先 **0 个**收 bank_id ——
# v20 最核心的 (user_id, bank_id) 多域隔离，在 Claude Desktop / Cursor / Hermes 全走的
# MCP 生态里根本传不进去，所有 Agent 被锁死在 default 域。REST 契约有它，MCP 契约没有。
from ducky.bank_contract import DEFAULT_BANK_ID
# 🟡12：版本号统一从真相源导入，杜绝 mcp_server 自报 18.0.0 与 version.py 打架。
from ducky.version import SERVICE_VERSION

# ── 常量 ──
STATE_DB = os.environ.get("AIDUMEM_HOST_STATE_DB", "")
LAST_ID_FILE = os.environ.get(
    "AIDUMEM_HOST_LAST_ID",
    os.path.join(DATA_DIR, "auto_memory_last_id.txt"),
)
API_BASE = os.environ.get("AIDUMEM_API_BASE", "http://127.0.0.1:8767").rstrip("/")


def _mcp_principal() -> str:
    """Return the configured MCP caller identity, never the requested target.

    ``user_id`` in a tool call selects the memory hall being addressed.  It is
    not proof of who is making the call.  The host integration may explicitly
    set ``AIDUMEM_USER_ID``; the historical default remains the single-owner
    fallback for loopback deployments.
    """
    return (
        os.environ.get("AIDUMEM_USER_ID", "").strip()
        or DEFAULT_USER_ID
    )

os.makedirs(LOG_DIR, exist_ok=True)

# ── 日志 ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "mcp_server.log")),
        logging.StreamHandler(sys.stderr),
    ],
)
logger = logging.getLogger("aiduMEM-mcp")


# ═══════════════════════════════════════════════════════
# HTTP 客户端辅助（轻量，无第三方依赖）
# ═══════════════════════════════════════════════════════

def _api_headers(extra: dict | None = None) -> dict:
    """构造带 API token 的请求头（配置了 token 时自动携带）。

    v19.4.2：改为复用 ducky.utils.api_auth_headers()（单一真相源）。
    此前这里自带一份 `os.environ.get(...)` 快照，有两个坑：
      ① 无 .env 兜底 —— MCP 由 Claude Desktop / Hermes 启动，环境往往
         不含 AIDUMEM_API_TOKEN，门禁一开所有工具调用直接 401；
      ② 在 import 时就把 token 固化成模块常量，进程运行期间轮换凭据不生效。
    改用统一入口后，两个坑一起消失，且守卫测试能锁住它不再分叉。
    """
    headers = {"Accept": "application/json"}
    headers.update(api_auth_headers())
    if extra:
        headers.update(extra)
    return headers

def _insecure_public_confirmed(host: str | None) -> bool:
    """f0.3: the credential-less escape hatch needs the same explicit
    confirmation as the REST gate — AIDUMEM_ALLOW_INSECURE_PUBLIC=1 *and*
    AIDUMEI_I_CONFIRM_PUBLIC_NO_AUTH equal (verbatim) to the bind host."""
    if os.environ.get("AIDUMEM_ALLOW_INSECURE_PUBLIC", "0").lower() not in {"1", "true", "yes"}:
        return False
    confirm = os.environ.get("AIDUMEI_I_CONFIRM_PUBLIC_NO_AUTH", "").strip()
    return bool(host) and confirm == str(host).strip()


def _sse_authorization_allowed(host: str | None = None) -> bool:
    """SSE must not expose authenticated API tools without a transport credential."""
    if api_auth_headers().get("Authorization"):
        return True
    return _insecure_public_confirmed(host)


def _build_sse_app_with_auth(mcp_obj, *, loopback: bool, host: str | None = None):
    """v20.4.0（三方审计 P1-7 · Codex P1-03）：SSE 传输层逐请求认证。

    此前非回环启动只查「服务器配置了 token」，`sse_app` 与 uvicorn 之间
    没有任何逐请求 Authorization 校验 —— 而工具调用由服务端自动携带
    服务端 token 打 API，于是端口一旦被反代/端口映射暴露，连上 SSE 的
    远程方**不需要知道 token** 就继承全部 MCP 工具权限。「启动时已配置
    token」不等于「传输层请求已认证」。

    现在：非回环绑定时，外层 ASGI 中间件对每个 HTTP 请求校验
    `Authorization: Bearer <AIDUMEM_API_TOKEN>`（与 API 门禁同一把钥匙，
    constant-time 比较）；缺失/错误 → 401，不进 MCP 会话。回环绑定保持
    免认证（与 api_server 的回环信任模型同一口径）。显式
    AIDUMEM_ALLOW_INSECURE_PUBLIC=1 且 AIDUMEI_I_CONFIRM_PUBLIC_NO_AUTH 逐字
    等于监听地址时，按部署方声明放行（f0.3：与 REST 公网门禁同一确认口径）。
    """
    import hmac

    inner = mcp_obj.sse_app(mount_path="/sse")
    if loopback:
        return inner

    def _expected_token() -> str:
        auth = api_auth_headers().get("Authorization", "")
        return auth.removeprefix("Bearer ").strip()

    insecure_ok = _insecure_public_confirmed(host)

    async def _auth_wrapped(scope, receive, send):
        if scope.get("type") != "http" or insecure_ok:
            await inner(scope, receive, send)
            return
        expected = _expected_token()
        presented = ""
        for k, v in scope.get("headers") or []:
            if k == b"authorization":
                presented = v.decode("latin-1").removeprefix("Bearer ").strip()
                break
        if not expected or not presented or not hmac.compare_digest(presented, expected):
            body = (b'{"detail":"MCP SSE requires Authorization: Bearer <AIDUMEM_API_TOKEN>"}')
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"www-authenticate", b"Bearer")]})
            await send({"type": "http.response.body", "body": body})
            return
        await inner(scope, receive, send)

    return _auth_wrapped


def _api_get(path: str, params: dict | None = None, timeout: int = 20) -> dict:
    """GET 请求 api_server。返回解析后的 JSON dict 或 error dict。

    v22.0（雷霆审计 B6 · Step P1-8）：传输错误（连接失败/超时）抛异常，
    让 MCP 框架标 isError=True；业务错误（4xx）保留 error dict 形态。
    此前传输错误与业务错误同态，Agent 把「服务挂了」当成「查无此忆」。
    """
    # v20.4.1a(C 面整改):urllib → httpx,与全仓 HTTP 客户端统一
    # (连接池/超时语义一致,少一套排障分支)。query 编码交给 httpx。
    # v0.3++ A1：MCP 工具的 user_id 是目标殿；把同一 principal 明确带到
    # 服务端，避免 REST 已收紧而 MCP 仍靠空 caller 运行。
    params = dict(params or {})
    params["caller_user_id"] = _mcp_principal()
    resp = httpx.get(f"{API_BASE}{path}", params=params,
                     headers=_api_headers(), timeout=timeout)
    if resp.status_code >= 400:
        return {"error": f"HTTP {resp.status_code}", "detail": resp.text[:500]}
    return resp.json()


def _api_post(path: str, body: dict | None = None, timeout: int = 30,
              params: dict | None = None) -> dict:
    """POST 请求 api_server。返回解析后的 JSON dict 或 error dict。

    `params` 走 query string（v20.2.4 · 外审 F-21）：有些端点的入参是**散装标量**
    而不是 Pydantic 模型，FastAPI 会从 query 读它们，把同名键塞进 JSON body
    是**静默无效**的 —— 请求 200、参数全丢。哪个端点该用哪种，由
    tests/test_v20_2_4_mcp_contract.py 逐工具对表钉住。

    v22.0（雷霆审计 B6）：传输错误抛异常（isError=True），业务错误保留 dict。
    """
    body = dict(body or {})
    body["caller_user_id"] = _mcp_principal()
    params = dict(params or {})
    params["caller_user_id"] = _mcp_principal()
    clean_params = (
        {k: v for k, v in params.items() if v is not None and v != ""}
        if params else None
    )
    resp = httpx.post(f"{API_BASE}{path}", params=clean_params,
                      json=body or {},
                      headers=_api_headers({"Content-Type": "application/json"}),
                      timeout=timeout)
    if resp.status_code >= 400:
        return {"error": f"HTTP {resp.status_code}", "detail": resp.text[:500]}
    return resp.json()


def _ok(data: Any) -> str:
    """把 dict 序列化为 MCP 工具返回字符串。"""
    return json.dumps(data, ensure_ascii=False, indent=2)


def _err(msg: str) -> str:
    return json.dumps({"error": msg}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# FastMCP 初始化
# ═══════════════════════════════════════════════════════

from importlib.metadata import PackageNotFoundError, version as package_version

_MCP_INSTALL_HELP = (
    "aiduMEI MCP requires the official mcp SDK 1.x. "
    "Run: python -m pip install 'mcp==1.30.0' "
    "(or python -m pip install -r requirements.txt). "
    "The separate fastmcp package is not used."
)
try:
    if package_version("mcp").split(".")[0] != "1":
        raise ImportError(_MCP_INSTALL_HELP)
    from mcp.server.fastmcp import FastMCP
except (ImportError, PackageNotFoundError) as exc:
    raise RuntimeError(_MCP_INSTALL_HELP) from exc

from ducky.loop_guard import LoopGuard
import weakref
import uuid


class _GuardedFastMCP(FastMCP):
    """Register every tool through one guard; scope SSE state per connection.

    f0.6 W-13（hark 审计 P0-3 / muse H-2）：破坏性工具默认不注册。宿主
    Agent 被提示注入时可借破坏性工具改写/删除记忆；「confirm 参数由模型
    自己填」不构成独立确认。现在 mem_update / mem_delete 需部署侧显式
    AIDUMEI_MCP_DANGEROUS_TOOLS=1 才注册，默认工具集只含读+追加写。
    """
    DANGEROUS_TOOLS_ENV = "AIDUMEI_MCP_DANGEROUS_TOOLS"
    # f0.6+++ WP2：mem_delete_all / autodream_trigger / crystals_detect /
    # conflict_resolve 随其 REST 端点一起移除，破坏性工具只剩这两个。
    DANGEROUS_TOOL_NAMES = frozenset({"mem_update", "mem_delete"})

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.loop_guard = LoopGuard.from_env(logger=logger)
        self._guard_sessions = weakref.WeakKeyDictionary()
        self._guard_sessions_lock = threading.Lock()
        self._dangerous_allowed = (
            os.environ.get(self.DANGEROUS_TOOLS_ENV, "").strip().lower()
            in ("1", "true", "yes", "on")
        )
        if not self._dangerous_allowed:
            logger.warning(
                "⚠️ [MCP] 破坏性工具未注册（%s 未开启）：%s。宿主注入一次调用即可清库"
                "的路径已默认焊死；确需从 MCP 使用请显式开启该环境变量。",
                self.DANGEROUS_TOOLS_ENV, ", ".join(sorted(self.DANGEROUS_TOOL_NAMES)))

    def _guard_scope(self):
        try:
            session = self.get_context().session
        except (ValueError, LookupError):
            return "direct"  # Direct Python calls, outside an MCP request.
        with self._guard_sessions_lock:
            if session not in self._guard_sessions:
                self._guard_sessions[session] = uuid.uuid4().hex
            return self._guard_sessions[session]

    def tool(self, *args, **kwargs):
        # Internal registration metadata, not an exposed tool argument. Only
        # fields the implementation actually JSON-decodes may be normalized.
        json_fields = kwargs.pop("fingerprint_json_fields", ())
        register = super().tool(*args, **kwargs)

        def decorator(fn):
            if not self._dangerous_allowed and fn.__name__ in self.DANGEROUS_TOOL_NAMES:
                # 不注册：装饰器原样返回函数，FastMCP 工具表里不出现。
                # 保留函数体便于直连测试与未来按需挂载。
                logger.info("[MCP] 跳过破坏性工具注册: %s", fn.__name__)
                return fn
            return register(self.loop_guard.wrap(fn, self._guard_scope, json_fields=json_fields))
        return decorator


mcp = _GuardedFastMCP("aidumem", log_level="INFO")


# ═══════════════════════════════════════════════════════
# ① 核心记忆 CRUD
# ═══════════════════════════════════════════════════════

@mcp.tool(fingerprint_json_fields=("messages",))
def mem_add(messages: str, user_id: str = DEFAULT_USER_ID, bank_id: str = DEFAULT_BANK_ID) -> str:
    """添加记忆到 aiduMEI（自动提炼 + 向量化存储）。

    Args:
        messages: JSON 字符串，格式 [{"role": "user/assistant", "content": "..."}]
        user_id:  用户标识，默认取 AIDUMEM_DEFAULT_USER_ID 环境变量
    """
    try:
        msg_list = json.loads(messages)
    except json.JSONDecodeError as e:
        return _err(f"messages JSON 解析失败: {e}")
    result = _api_post("/add", {"messages": msg_list, "user_id": user_id, "bank_id": bank_id})
    return _ok(result)


@mcp.tool()
def mem_search(query: str, user_id: str = DEFAULT_USER_ID, top_k: int = 5, bank_id: str = DEFAULT_BANK_ID,
               session_id: str = "") -> str:
    """语义搜索记忆（内置相关性闸门 + 显著性 boost）。

    响应带三态判语 `recall_verdict`（v20.1）：
      · found     —— 有可信结果，可以引用；
      · not_found —— 记忆库里确实没有（空结果 / 低于置信下限）。请如实
                     承认「没有相关记忆」，不要拿低分结果编造；
      · degraded  —— 召回组件故障，「没有」不可知。请报告记忆系统异常，
                     而不是当成「查无此忆」。
    `verdict_basis` 给出判定依据，`recall_confidence` 为本次最高融合分。

    Args:
        query:   搜索关键词或自然语言问题
        user_id: 用户标识
        top_k:   返回结果数量，默认 5
        session_id: 可选，本轮会话标识。传了才启用回声抑制——本会话自己
                    刚写入的记忆不会再被当作「历史记忆」召回（v21.2 M2）。
    """
    # v21.2.0 审计整改轮：把 session_id 接上 —— 本模块早有完整的 session
    # 生命周期工具（session_start / session_end / session_report），检索却
    # 一直不带它，于是整条 MCP 通路上 v21.2 的回声抑制（M2）根本不存在：
    # 服务端读不到 session 就整段跳过，不报错也无从察觉。不传仍是不过滤。
    # v22.0（雷霆审计 A3）：MCP 经 API token 调用，空 caller 不再放行。
    # caller 来自宿主配置；工具传入的 user_id 仅选择目标。
    _payload = {"query": query, "user_id": user_id, "caller_user_id": _mcp_principal(),
                "top_k": top_k, "bank_id": bank_id}
    if session_id:
        _payload["session_id"] = session_id
    result = _api_post("/search", _payload)
    return _ok(result)


@mcp.tool()
def mem_update(memory_id: str, content: str, user_id: str = DEFAULT_USER_ID, bank_id: str = DEFAULT_BANK_ID) -> str:
    """更新指定 ID 的记忆内容。

    Args:
        memory_id: 记忆 UUID
        content:   新的记忆文本
        user_id:   用户标识
    """
    result = _api_post("/update", {"memory_id": memory_id, "content": content, "user_id": user_id, "bank_id": bank_id})
    return _ok(result)


@mcp.tool()
def mem_delete(memory_id: str, user_id: str = DEFAULT_USER_ID, bank_id: str = DEFAULT_BANK_ID) -> str:
    """删除指定 ID 的记忆。

    Args:
        memory_id: 记忆 UUID
        user_id:   用户标识
    """
    result = _api_post("/delete", {"memory_id": memory_id, "user_id": user_id, "bank_id": bank_id})
    return _ok(result)


# ═══════════════════════════════════════════════════════
# ② 统计与健康
# ═══════════════════════════════════════════════════════


@mcp.tool()
def mem_health() -> str:
    """检查 aiduMEI 服务健康状态，包含所有子模块探针结果和版本信息。"""
    result = _api_get("/health")
    return _ok(result)


# ═══════════════════════════════════════════════════════
# ③ 核心记忆块（Core Memory）
# ═══════════════════════════════════════════════════════

@mcp.tool()
def core_memory_list(user_id: str = DEFAULT_USER_ID,
                     bank_id: str = DEFAULT_BANK_ID) -> str:
    """列出指定记忆域的核心记忆块（默认当前配置的记忆域）。"""
    result = _api_get("/api/core-memory", {
        "user_id": user_id, "bank_id": bank_id, "caller_user_id": _mcp_principal(),
    })
    return _ok(result)


# ═══════════════════════════════════════════════════════
# 后台自动记忆线程（直读宿主 state.db，不走 HTTP）
# ═══════════════════════════════════════════════════════

def _read_last_id() -> int:
    if os.path.exists(LAST_ID_FILE):
        try:
            return int(Path(LAST_ID_FILE).read_text().strip())
        except (ValueError, OSError):
            return 0
    return 0


def _write_last_id(msg_id: int) -> None:
    os.makedirs(os.path.dirname(LAST_ID_FILE), exist_ok=True)
    Path(LAST_ID_FILE).write_text(str(msg_id))


def _open_state_db():
    """只读打开宿主 state.db：本进程只读它，不该持有写句柄、更不该在宿主写入时抢锁。"""
    return sqlite3.connect(Path(STATE_DB).resolve().as_uri() + "?mode=ro", uri=True)


def _fetch_new_messages(last_id: int, limit: int = 200) -> tuple[list, int]:
    """从宿主 state.db 增量读取新对话消息。"""
    if not STATE_DB:
        return [], last_id
    if not os.path.exists(STATE_DB):
        logger.warning(f"宿主会话库不存在: {STATE_DB}")
        return [], last_id
    try:
        conn = _open_state_db()
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT m.id, m.session_id, m.role, m.content, m.timestamp, s.source
            FROM messages m
            LEFT JOIN sessions s ON m.session_id = s.id
            WHERE m.id > ?
              AND m.role IN ('user', 'assistant')
              AND m.content IS NOT NULL
              AND m.content != ''
              AND length(m.content) > 10
            ORDER BY m.id ASC
            LIMIT ?
            """,
            (last_id, limit),
        )
        rows = cur.fetchall()
        conn.close()
        messages = [
            {
                "id": r["id"],
                "session_id": r["session_id"],
                "role": r["role"],
                "content": r["content"][:2000],
                "timestamp": r["timestamp"],
                "source": r["source"],
            }
            for r in rows
        ]
        max_id = max((m["id"] for m in messages), default=last_id)
        return messages, max_id
    except Exception as e:
        logger.error(f"读取 state.db 失败: {e}")
        return [], last_id


def _group_by_session(messages: list) -> dict:
    sessions: dict = {}
    for msg in messages:
        sid = msg["session_id"]
        sessions.setdefault(sid, []).append(msg)
    for sid in sessions:
        sessions[sid].sort(key=lambda x: x["id"])
    return sessions


def _format_conversation(messages: list) -> str:
    parts = []
    for msg in messages:
        label = "User" if msg["role"] == "user" else "Assistant"
        parts.append(f"{label}: {msg['content']}")
    return "\n\n".join(parts)


def _resolve_session_owner(session_id: str) -> str:
    """v22.0（雷霆审计 B11 · GPT-5.6 P1-02）：从宿主会话元数据推导 owner。

    此前固定写 DEFAULT_USER_ID——owner 不明确时静默写错租户，比报错更糟。
    从 state.db 的 session 行读 user_id；读不到或为空 → 返回空串，调用方拒写。

    宿主 sessions 表的主键列叫 `id`（messages.session_id 引用它，与
    _fetch_new_messages 的 JOIN 同一口径）。此前这里写成 `WHERE session_id=?`，
    在真实 state.db 上必报 no such column，被下面的 except 吞成空串 —— 每个会话都被判
    「owner 不明」拒写，自动记忆从未写进过一条。
    """
    try:
        conn = _open_state_db()
        try:
            row = conn.execute(
                "SELECT user_id FROM sessions WHERE id=?", (session_id,)
            ).fetchone()
            return (row[0] or "").strip() if row else ""
        finally:
            conn.close()
    except Exception:
        return ""


def _transient_http_error(error) -> bool:
    """_api_post 的业务错误形如 "HTTP 503"：5xx 与 429 视为暂时性，值得下一轮重试。"""
    parts = str(error or "").split()
    if len(parts) == 2 and parts[0] == "HTTP" and parts[1].isdigit():
        code = int(parts[1])
        return code >= 500 or code == 429
    return False


def run_auto_memory() -> None:
    """执行一次自动记忆提取（通过 HTTP 调用 /add 端点）。"""
    last_id = _read_last_id()
    messages, max_id = _fetch_new_messages(last_id)
    if not messages:
        return

    sessions = _group_by_session(messages)
    stored_total = 0
    skipped = 0
    # 暂时性失败（5xx / 429）的会话不能被游标越过：越过即永久丢失，下一轮再也读不到。
    # 4xx 是确定性的拒绝（凭据、caller、校验），重试也不会变，照旧跳过并告警。
    hold_from = None
    for sid, msgs in sessions.items():
        if len(msgs) < 2:
            continue
        owner = _resolve_session_owner(sid)
        if not owner:
            # v22.0（B11）：owner 不可判定 → 拒写并记 degraded，不 fallback 到 default
            logger.warning(
                "🛑 [auto-memory] 会话 %s 的 owner 无法从 state.db 推导，拒写"
                "（不再静默 fallback 到 default 域）", sid[:8])
            skipped += 1
            continue
        batch_key = (sid, msgs[0]["id"], msgs[-1]["id"])
        if batch_key in _auto_stored:
            continue          # 上一轮已写成功，只是游标因别的会话回退 —— 不重写
        conversation = _format_conversation(msgs)
        msg_list = [{"role": "user", "content": conversation}]
        resp = _api_post("/add", {"messages": msg_list, "user_id": owner}, timeout=60)
        if "error" not in resp:
            n = len(resp.get("results", []))
            stored_total += n
            _auto_stored.add(batch_key)
            _auto_retry_counts.pop(batch_key, None)
            logger.info(f"[auto-memory] 会话 {sid[:8]}… → {n} 条记忆（owner={owner}）")
        else:
            logger.warning(f"[auto-memory] 会话 {sid[:8]}… 存入失败: {resp.get('error')}")
            if _transient_http_error(resp.get("error")):
                tries = _auto_retry_counts.get(batch_key, 0) + 1
                _auto_retry_counts[batch_key] = tries
                if tries >= _AUTO_MAX_TRANSIENT_RETRIES:
                    _auto_retry_counts.pop(batch_key, None)
                    logger.error(
                        f"[auto-memory] 会话 {sid[:8]}… 连续 {tries} 轮暂时性失败，放行游标、跳过该批"
                        "（防止整条自动记忆线卡死）")
                    continue
                first = msgs[0]["id"]
                hold_from = first if hold_from is None else min(hold_from, first)

    if hold_from is not None:
        max_id = min(max_id, hold_from - 1)
        logger.warning(f"[auto-memory] 有会话暂时性写入失败，游标停在 {max_id}，下一轮重试")
    _write_last_id(max_id)
    if stored_total or skipped:
        logger.info(
            f"[auto-memory] 本轮存入 {stored_total} 条"
            f"（游标→{max_id}），拒写 {skipped} 条（owner 不明）")


# f0.7.1：暂时性失败的重试上限与「已写过」记录（进程内）。
#   · 某会话持续 5xx（例如某条消息本身触发 500）时，游标不能永远停住 —— 连续 N 轮后放行并告警；
#   · 游标回退重试时，同批里已经写成功的会话不再重写一遍。
_AUTO_MAX_TRANSIENT_RETRIES = 3
_auto_retry_counts: dict = {}
_auto_stored: set = set()


def auto_memory_loop() -> None:
    while True:
        try:
            if STATE_DB:
                run_auto_memory()
        except Exception as e:
            logger.error(f"❌ 自动记忆异常: {e}", exc_info=True)
        time.sleep(21600)  # 每 6 小时一次


# ═══════════════════════════════════════════════════════
# 主入口 — 必须保持在文件最末尾，确保上方所有 @mcp.tool 装饰器都已执行
# （stdio 模式下 mcp.run() 会阻塞，若提前会漏注册其后的工具）
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=f"aiduMEI MCP Server v{SERVICE_VERSION}")
    parser.add_argument("--sse", action="store_true", help="以 SSE HTTP 模式启动（默认 stdio）")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--host", type=str, default="127.0.0.1")
    args = parser.parse_args()

    # 启动后台自动记忆线程
    threading.Thread(target=auto_memory_loop, daemon=True, name="auto-memory").start()
    logger.info(f"🧠 aiduMEI MCP Server v{SERVICE_VERSION} 启动（API_BASE={API_BASE}）")

    if args.sse:
        logger.info(f"🌐 SSE 模式，监听 {args.host}:{args.port}")
        loopback = args.host in {"127.0.0.1", "localhost", "::1"}
        if not loopback and not _sse_authorization_allowed(args.host):
            raise RuntimeError(
                "MCP SSE refuses to bind a non-loopback host without AIDUMEM_API_TOKEN "
                "(or explicit AIDUMEM_ALLOW_INSECURE_PUBLIC=1 plus "
                f"AIDUMEI_I_CONFIRM_PUBLIC_NO_AUTH='{args.host}')."
            )
        import uvicorn
        # v20.4.0（P1-7）：非回环绑定时逐请求认证包住 sse_app
        app = _build_sse_app_with_auth(mcp, loopback=loopback, host=args.host)
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    else:
        logger.info("📟 stdio 模式启动")
        mcp.run(transport="stdio")
