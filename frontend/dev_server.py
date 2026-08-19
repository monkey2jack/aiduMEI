#!/usr/bin/env python3
"""
aiduMEI dev server — static file host + local-test proxy to aiduMEM.

This version talks to the LOCAL test instance (127.0.0.1:8777 by default),
not a remote production server. Write endpoints are open so we can test
the full round-trip.

⚠️ LOCAL TEST ONLY — never point AIDUMEM_UPSTREAM at a production instance.
   Write endpoints (/add /update /delete /facts/expire ...) are whitelisted
   here precisely because the target is expected to be disposable.
   To browse a production console, use the service's own port with the
   auth gate on — not this proxy.

凭据（v19.4.2 修补）
-------------------
本代理曾是**鉴权守卫的盲区**：它既在被跳过的 `frontend/` 目录里，
用的又是第四个变量名 `AIDUMEM_UPSTREAM`（守卫只认 AIDUMEM_API_BASE /
AIDUMEM_URL / :8767）。于是门禁一开，控制台每个面板都 401 ——
和 v19.4.1 那一版 hook 静默 401 是同一种病，却发生在标题叫《守卫扩面》
的这一版里。现在它走 `ducky.utils.api_auth_headers()`，与全仓同一条
兜底链（环境变量 → AIDUMEM_ENV_FILE → ~/.aidumem/.env）。
未配 token 时返回空 dict，本机零配置行为完全不变。

Usage
-----
    python3 dev_server.py            # open http://127.0.0.1:8788/
    AIDUMEM_UPSTREAM=http://127.0.0.1:8777 python3 dev_server.py
"""

import json
import os
import sys
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

WEB_ROOT = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(WEB_ROOT)
if _REPO_ROOT not in sys.path:            # 本文件常被直接 `python3 frontend/dev_server.py` 拉起
    sys.path.insert(0, _REPO_ROOT)

try:
    from ducky.utils import api_auth_headers  # noqa: E402
except ImportError as exc:                # pragma: no cover - 环境问题，不是逻辑分支
    # 故意不降级为「无凭据继续跑」：那正是本轮要根除的病 ——
    # 服务端门禁一开，面板会全线 401，而 401 长得就像「后端没数据」。
    # 宁可起不来并说清原因，也不要起来之后静默失灵。
    sys.stderr.write(
        "dev_server: 无法 import ducky.utils（%s）。\n"
        "本代理需要仓库的运行环境才能取到 API 凭据，请用仓库 venv 启动：\n"
        "    .venv/bin/python frontend/dev_server.py\n" % exc
    )
    raise SystemExit(1)

UPSTREAM = os.environ.get("AIDUMEM_UPSTREAM", "http://127.0.0.1:8777")
PORT = int(os.environ.get("AIDUMEI_PORT", "8788"))
TIMEOUT = 60

# ---------------------------------------------------------------------------
# Read-only endpoints (always allowed)
# ---------------------------------------------------------------------------
ALLOW_GET = {
    "/health", "/stats", "/usage", "/recent", "/raw/stats", "/workspace",
    "/facts", "/facts/categories", "/facts/tags", "/facts/trust-stats",
    "/facts/entities", "/facts/entities/list", "/facts/related",
    "/facts/reason", "/facts/search", "/facts/preferences", "/facts/delta",
    "/knowledge/tree", "/tree/nodes", "/code/graph",
    "/evolve/report", "/crystals",
    "/observe", "/observe/related", "/scene", "/persona", "/persona/ai-self",
    "/session/list", "/session/report", "/search/deep",
    "/federation/agents", "/federation/awareness", "/federation/broadcast",
    "/federation/tiers", "/federation/recall",
    "/api/core-memory", "/api/checkpoint/latest",
    "/api/autodream/status", "/api/autodream/report", "/auto-memory/status",
    "/add/coalesce", "/add/coalesce/stats",
    "/config", "/config/schema",
}

ALLOW_GET_PREFIX = (
    "/api/core-memory/", "/api/checkpoint/", "/add/job/",
    "/config/", "/facts/",
)

# Write endpoints — open in local-test mode
ALLOW_POST = {
    "/search", "/search_trace", "/recall_chain", "/code/impact", "/jlens",
    "/add", "/add/raw", "/add/coalesce/flush",
    "/delete", "/update",
    "/evolve/feedback", "/evolve/cycle",
    "/reload",
    "/conflict/resolve",
    "/crystals/detect", "/crystals/approve", "/crystals/reject",
    "/facts/add", "/facts/expire", "/facts/compress",
    "/federation/agents/register", "/federation/facts/add",
}

ALLOW_PUT_PREFIX = (
    "/config/",
    "/prompts/",
)

ALLOW_DELETE_PREFIX = (
    "/archive/",
)


def is_allowed(method, path):
    if method == "GET":
        if path in ALLOW_GET:
            return True
        return any(path.startswith(p) for p in ALLOW_GET_PREFIX)
    if method == "POST":
        return path in ALLOW_POST
    if method == "PUT":
        return any(path.startswith(p) for p in ALLOW_PUT_PREFIX)
    if method == "DELETE":
        return any(path.startswith(p) for p in ALLOW_DELETE_PREFIX)
    return False


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB_ROOT, **kw)

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))

    def _send_json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self, method, raw_path, body=None):
        path, _, query = raw_path.partition("?")

        if not is_allowed(method, path):
            sys.stderr.write("  BLOCKED %s %s\n" % (method, path))
            self._send_json(403, {"status": "blocked", "method": method, "path": path})
            return

        url = UPSTREAM + path + (("?" + query) if query else "")
        req = urllib.request.Request(url, method=method)
        req.add_header("Accept", "application/json")
        if body is not None:
            req.add_header("Content-Type", "application/json")
        # 门禁凭据：每次请求时读取（而非模块加载时定格），
        # 这样 .env 里换了 token 之后重启上游即可，不必重启本代理。
        # 未配置 token 时是空 dict，行为与门禁未启用时完全一致。
        for header, value in api_auth_headers().items():
            req.add_header(header, value)

        try:
            with urllib.request.urlopen(req, data=body, timeout=TIMEOUT) as resp:
                payload = resp.read()
                code = resp.status
                ctype = resp.headers.get("Content-Type", "application/json; charset=utf-8")
        except urllib.error.HTTPError as e:
            payload = e.read()
            code = e.code
            ctype = e.headers.get("Content-Type", "application/json; charset=utf-8")
            if code in (401, 403):
                # 不留痕的 401 在浏览器里长得像「后端没数据」，能骗人一整天。
                sys.stderr.write(
                    "  auth_failed %s %s -> %d（上游门禁已开，本代理没取到有效 token；"
                    "请检查 AIDUMEM_API_TOKEN 或 .env）\n" % (method, path, code)
                )
        except Exception as e:
            self._send_json(502, {"status": "error", "error": str(e), "upstream": UPSTREAM})
            return

        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _handle_api(self, method, want_body=False):
        """四个 do_* 的公共骨架：只认 /api 前缀，剥掉它再交给 _proxy。

        原先四个方法各写一遍同样的前缀判断与读 body，改一处要记得改四处 ——
        凭据这类「必须每条路径都生效」的东西，最怕的就是这种复制粘贴。
        """
        if not self.path.startswith("/api/"):
            self._send_json(404, {"status": "error", "error": "not found"})
            return
        body = None
        if want_body:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b"{}"
        self._proxy(method, self.path[4:], body=body)

    def do_GET(self):
        if self.path.startswith("/api/"):
            self._handle_api("GET")
            return
        if self.path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self):
        self._handle_api("POST", want_body=True)

    def do_PUT(self):
        self._handle_api("PUT", want_body=True)

    def do_DELETE(self):
        self._handle_api("DELETE")

    def do_PATCH(self):
        self._send_json(403, {"status": "blocked", "error": "PATCH not supported"})


def main():
    # 整块 banner 走 stderr：请求日志（log_message）也在 stderr，两者才同序。
    # 走 stdout 的话，`nohup ... > dev.log` 下 print 是块缓冲的，
    # banner 会一直躺在缓冲区里 —— 而「auth 有没有加载」正是要在启动那一刻看的，
    # 等到进程退出才刷出来，等于没有。
    lines = [
        "=" * 64,
        "  aiduMEI dev server  [LOCAL-TEST MODE - writes open]",
        "=" * 64,
        "  UI       : http://127.0.0.1:%d/" % PORT,
        "  upstream : %s" % UPSTREAM,
        # 只报「有没有」，不报值 —— 但必须报，否则「代理裸奔」要等到面板全白才发现。
        "  auth     : %s" % ("token loaded" if api_auth_headers() else "none (gate must be off)"),
        "  WARNING  : point at LOCAL test instance, not production!",
        "=" * 64,
    ]
    sys.stderr.write("\n".join(lines) + "\n")
    sys.stderr.flush()
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
