"""aiduMEM —— Hermes Agent 官方 MemoryProvider 适配层。

在 v15 之前，aiduMEM 只能通过 `agent-hooks` 里的 shell 脚本（pre_llm_call）
把记忆塞进上下文。那条路能跑，但拿不到官方 provider 的任何生命周期钩子：
压缩前抢救、memory 写入镜像、工具调用、备份路径、会话结束归档全都没有，
而且脚本一旦 payload 字段变形就会静默失效（v14 的血训就是这么来的）。

这个插件把 aiduMEM 接到官方 `MemoryProvider` 抽象上：

    prefetch          → POST /search + /api/core-memory/inject（开局注入）
    sync_turn         → POST /add（后台线程，不阻塞对话）
    on_pre_compress   → POST /add，把即将被压掉的轮次先落盘
    on_memory_write   → POST /facts/add，镜像内置 memory 的写入
    on_session_end    → POST /session/end，触发服务端归档/反思
    get_tool_schemas  → aidumem_search / aidumem_remember / aidumem_status
    backup_paths      → 数据目录交给 Hermes 的备份流程

服务本身不做鉴权，所以默认只连回环地址。要跨机使用请在前面挂一层
带认证的反向代理，并通过 AIDUMEM_URL 指过去。

环境变量（全部可选，见仓库 .env.example）：
    AIDUMEM_URL         默认 http://127.0.0.1:8767
    AIDUMEM_USER_ID     默认 default
    AIDUMEM_DATA_DIR    备份用数据目录，默认 ~/aidumem
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional
from urllib import error as urlerror
from urllib import request as urlrequest

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

logger = logging.getLogger(__name__)

DEFAULT_URL = "http://127.0.0.1:8767"
_CONNECT_TIMEOUT = 2.0       # 探活：不能让 is_available 拖慢启动
_QUERY_TIMEOUT = 6.0         # 检索：阻塞在 turn 开头，必须短
_WRITE_TIMEOUT = 20.0        # 写入：都在后台线程里，可以放宽
_MIN_QUERY_LEN = 3
_MAX_CONTEXT_CHARS = 4000


# ---------------------------------------------------------------------------
# HTTP 小客户端（只用标准库，避免给宿主装依赖）
# ---------------------------------------------------------------------------

class _Client:
    def __init__(self, base_url: str, user_id: str):
        self.base = base_url.rstrip("/")
        self.user_id = user_id
        # 🔴P0-1（v19.4.1）：与后端读同一个环境变量携带 Bearer token。
        # 后端一旦启用门禁（设了 AIDUMEM_API_TOKEN），插件不带凭据会全线 401，
        # 而记忆层失败是静默的（try_request 吞异常）—— 用户只会觉得
        # 「记忆突然不好用了」，排查成本极高。这里主动对齐。
        self.api_token = os.environ.get("AIDUMEM_API_TOKEN", "").strip()

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[dict] = None,
        timeout: float = _QUERY_TIMEOUT,
    ) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        req = urlrequest.Request(
            f"{self.base}{path}",
            data=data,
            method=method,
            headers=headers,
        )
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}

    def try_request(self, method: str, path: str, **kwargs) -> Optional[Any]:
        """失败返回 None —— 记忆层永远不该让对话崩掉。"""
        try:
            return self.request(method, path, **kwargs)
        except (urlerror.URLError, OSError, ValueError) as exc:
            logger.debug("aiduMEM %s %s failed: %s", method, path, exc)
            return None


# ---------------------------------------------------------------------------
# 工具 schema
# ---------------------------------------------------------------------------

SEARCH_SCHEMA = {
    "name": "aidumem_search",
    "description": (
        "Search aiduMEM long-term memory for facts from past sessions — user "
        "preferences, project decisions, people, dates, past troubleshooting. "
        "Use whenever prior context would change the answer."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to look for."},
            "limit": {"type": "integer", "description": "Max results (default 5)."},
        },
        "required": ["query"],
    },
}

REMEMBER_SCHEMA = {
    "name": "aidumem_remember",
    "description": (
        "Store a durable fact in aiduMEM: user preferences, corrections, stable "
        "environment facts, decisions. Not for task progress or transient state."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The fact to remember."},
        },
        "required": ["content"],
    },
}

STATUS_SCHEMA = {
    "name": "aidumem_status",
    "description": "Check aiduMEM health — version, probes, degraded subsystems, usage.",
    "parameters": {"type": "object", "properties": {}, "required": []},
}


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class AiduMemProvider(MemoryProvider):
    """aiduMEM 自托管记忆服务的官方 provider 实现。"""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = dict(config or {})
        url = cfg.get("url") or os.environ.get("AIDUMEM_URL") or DEFAULT_URL
        user_id = cfg.get("user_id") or os.environ.get("AIDUMEM_USER_ID") or "default"
        self._client = _Client(url, user_id)
        self._session_id = ""
        self._threads: List[threading.Thread] = []

    # -- 身份 ---------------------------------------------------------------

    @property
    def name(self) -> str:
        return "aidumem"

    def is_available(self) -> bool:
        health = self._client.try_request("GET", "/health", timeout=_CONNECT_TIMEOUT)
        return isinstance(health, dict) and bool(health)

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "url",
                "description": "aiduMEM service URL (keep on loopback unless proxied with auth)",
                "default": DEFAULT_URL,
                "env_var": "AIDUMEM_URL",
            },
            {
                "key": "user_id",
                "description": "Memory namespace / user id",
                "default": "default",
                "env_var": "AIDUMEM_USER_ID",
            },
        ]

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        self._client.try_request(
            "POST", f"/session/start?session_id={session_id}", timeout=_CONNECT_TIMEOUT
        )

    def system_prompt_block(self) -> str:
        return (
            "# aiduMEM\n"
            "Self-hosted long-term memory is active. Use aidumem_search before "
            "asking the user to repeat past context, and aidumem_remember for "
            "durable facts worth keeping across sessions."
        )

    # -- 读路径 -------------------------------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """turn 开头同步注入：CoreMemory 常驻块 + 本轮相关检索。"""
        blocks: List[str] = []

        core = self._client.try_request(
            "POST", "/api/core-memory/inject", body={}, timeout=_QUERY_TIMEOUT
        )
        if isinstance(core, dict) and core.get("context"):
            blocks.append(str(core["context"]))

        if query and len(query.strip()) >= _MIN_QUERY_LEN:
            hits = self._client.try_request(
                "POST",
                "/search",
                body={"query": query.strip()[:2000], "user_id": self._client.user_id, "limit": 5},
                timeout=_QUERY_TIMEOUT,
            )
            lines = self._format_hits(hits)
            if lines:
                blocks.append("[aiduMEM 记忆检索]\n" + "\n".join(lines))

        if not blocks:
            return ""
        out = "\n".join(blocks)
        return out[:_MAX_CONTEXT_CHARS]

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """prefetch 已经是同步短超时，无需再排队。"""
        return None

    @staticmethod
    def _format_hits(hits: Any) -> List[str]:
        if not isinstance(hits, dict):
            return []
        results = hits.get("results") or []
        lines = []
        for item in results:
            if not isinstance(item, dict):
                continue
            text = (item.get("memory") or item.get("fact_value") or "").strip()
            if text:
                lines.append(f"· {text[:300]}")
        return lines

    # -- 写路径（全部后台，绝不阻塞对话）------------------------------------

    def _spawn(self, fn, name: str) -> None:
        t = threading.Thread(target=fn, daemon=True, name=name)
        t.start()
        self._threads = [x for x in self._threads if x.is_alive()][-8:] + [t]

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        if len((user_content or "").strip()) < _MIN_QUERY_LEN:
            return
        combined = f"User: {user_content[:4000]}\nAssistant: {(assistant_content or '')[:4000]}"
        # messages 带的是含工具调用的完整轮次；aiduMEM 服务端只吃纯文本，
        # 这里只记条数当元数据，供归档时判断这轮有多重。
        turn_size = len(messages) if isinstance(messages, list) else 0

        def _write():
            self._client.try_request(
                "POST",
                "/add",
                body={
                    "messages": combined,
                    "user_id": self._client.user_id,
                    "async_mode": True,
                    "metadata": {
                        "source": "hermes_turn",
                        "session_id": session_id or self._session_id,
                        "turn_size": turn_size,
                    },
                },
                timeout=_WRITE_TIMEOUT,
            )

        self._spawn(_write, "aidumem-turn")

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """压缩会丢掉原始轮次 —— 先把它们落进长期记忆再让它被压。"""
        parts = []
        for msg in (messages or [])[-12:]:
            role = msg.get("role")
            content = msg.get("content")
            if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
                parts.append(f"{role}: {content[:600]}")
        if not parts:
            return ""
        blob = "\n".join(parts)

        def _flush():
            self._client.try_request(
                "POST",
                "/add",
                body={
                    "messages": blob,
                    "user_id": self._client.user_id,
                    "async_mode": True,
                    "metadata": {"source": "pre_compress", "session_id": self._session_id},
                },
                timeout=_WRITE_TIMEOUT,
            )
            logger.info("aiduMEM pre-compress flush: %d messages", len(parts))

        self._spawn(_flush, "aidumem-precompress")
        return ""

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """把 Hermes 内置 MEMORY.md / USER.md 的写入镜像成结构化 fact。"""
        if action not in {"add", "replace"} or not content:
            return
        category = "user_profile" if target == "user" else "agent_memory"
        from urllib.parse import quote

        source = "hermes_memory_tool"
        if isinstance(metadata, dict) and metadata.get("source"):
            source = f"hermes_memory_tool/{str(metadata['source'])[:40]}"

        qs = (
            f"/facts/add?category={quote(category)}"
            f"&fact_key={quote(f'hermes/{target}')}"
            f"&fact_value={quote(content[:4000])}"
            f"&source={quote(source)}"
        )

        def _mirror():
            self._client.try_request("POST", qs, timeout=_WRITE_TIMEOUT)

        self._spawn(_mirror, "aidumem-memwrite")

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        sid = self._session_id
        if not sid:
            return

        def _end():
            self._client.try_request("POST", f"/session/end?session_id={sid}", timeout=_WRITE_TIMEOUT)

        self._spawn(_end, "aidumem-session-end")

    # -- 工具 ---------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [SEARCH_SCHEMA, REMEMBER_SCHEMA, STATUS_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name == "aidumem_search":
            query = (args.get("query") or "").strip()
            if not query:
                return tool_error("query is required")
            limit = args.get("limit")
            hits = self._client.try_request(
                "POST",
                "/search",
                body={
                    "query": query[:2000],
                    "user_id": self._client.user_id,
                    "limit": int(limit) if isinstance(limit, (int, str)) and str(limit).isdigit() else 5,
                },
                timeout=_QUERY_TIMEOUT,
            )
            if hits is None:
                return tool_error("aiduMEM unreachable")
            lines = self._format_hits(hits)
            return json.dumps(
                {"result": "\n".join(lines) if lines else "No relevant memories found."},
                ensure_ascii=False,
            )

        if tool_name == "aidumem_remember":
            content = (args.get("content") or "").strip()
            if not content:
                return tool_error("content is required")
            res = self._client.try_request(
                "POST",
                "/add",
                body={
                    "messages": content[:8000],
                    "user_id": self._client.user_id,
                    "metadata": {"source": "hermes_tool", "session_id": self._session_id},
                },
                timeout=_WRITE_TIMEOUT,
            )
            if res is None:
                return tool_error("aiduMEM unreachable")
            return json.dumps({"result": "Stored in aiduMEM."}, ensure_ascii=False)

        if tool_name == "aidumem_status":
            health = self._client.try_request("GET", "/health", timeout=_QUERY_TIMEOUT)
            if health is None:
                return tool_error("aiduMEM unreachable")
            # /usage 返回逐日全量历史（几十 KB 起），整块塞进工具结果会把
            # health 挤出截断边界。只留最近一天。
            usage_raw = self._client.try_request("GET", "/usage", timeout=_QUERY_TIMEOUT) or {}
            usage_latest: Dict[str, Any] = {}
            try:
                daily = usage_raw.get("usage") if isinstance(usage_raw, dict) else None
                if isinstance(daily, dict) and daily:
                    latest_day = max(daily.keys())
                    usage_latest = {"date": latest_day, **{"totals": daily[latest_day]}}
            except Exception:  # noqa: BLE001 — 用量是附赠信息，坏了不该影响 health
                usage_latest = {}
            return json.dumps({"health": health, "usage_latest": usage_latest}, ensure_ascii=False)[:4000]

        return tool_error(f"Unknown tool: {tool_name}")

    # -- 备份 / 收尾 --------------------------------------------------------

    def backup_paths(self) -> List[str]:
        """把 aiduMEM 数据目录挂进宿主备份流程。

        找不到目录就返回空 —— 但要出声，否则「备份里没有记忆」这件事
        会一路静默到需要恢复的那天。
        """
        explicit = os.environ.get("AIDUMEM_DATA_DIR")
        candidates = [explicit] if explicit else []
        home = os.path.expanduser("~")
        candidates += [os.path.join(home, "aidumem"), os.path.join(home, ".aidumem")]
        for path in candidates:
            if path and os.path.isdir(path):
                return [path]
        logger.warning(
            "aiduMEM: 数据目录未找到，记忆不会进入宿主备份。"
            "设 AIDUMEM_DATA_DIR 指向数据目录（已试: %s）",
            ", ".join(p for p in candidates if p),
        )
        return []

    def shutdown(self) -> None:
        for t in self._threads:
            if t.is_alive():
                t.join(timeout=5.0)


def register(ctx) -> None:
    ctx.register_memory_provider(AiduMemProvider())
