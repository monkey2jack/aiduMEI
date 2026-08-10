#!/usr/bin/env python3
"""
aiduMEI dev server — static file host + local-test proxy to aiduMEM.

This version talks to the LOCAL test instance (127.0.0.1:8777 by default),
not the Hangzhou production server. Write endpoints are open so we can test
the full round-trip. Do NOT point AIDUMEM_UPSTREAM at the production server
while running this mode.

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

UPSTREAM = os.environ.get("AIDUMEM_UPSTREAM", "http://127.0.0.1:8777")
PORT = int(os.environ.get("AIDUMEI_PORT", "8788"))
WEB_ROOT = os.path.dirname(os.path.abspath(__file__))
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

        try:
            with urllib.request.urlopen(req, data=body, timeout=TIMEOUT) as resp:
                payload = resp.read()
                code = resp.status
                ctype = resp.headers.get("Content-Type", "application/json; charset=utf-8")
        except urllib.error.HTTPError as e:
            payload = e.read()
            code = e.code
            ctype = e.headers.get("Content-Type", "application/json; charset=utf-8")
        except Exception as e:
            self._send_json(502, {"status": "error", "error": str(e), "upstream": UPSTREAM})
            return

        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path.startswith("/api/"):
            self._proxy("GET", self.path[4:])
            return
        if self.path == "/":
            self.path = "/index.html"
        super().do_GET()

    def do_POST(self):
        if not self.path.startswith("/api/"):
            self._send_json(404, {"status": "error", "error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b"{}"
        self._proxy("POST", self.path[4:], body=body)

    def do_PUT(self):
        if not self.path.startswith("/api/"):
            self._send_json(404, {"status": "error", "error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b"{}"
        self._proxy("PUT", self.path[4:], body=body)

    def do_DELETE(self):
        if not self.path.startswith("/api/"):
            self._send_json(404, {"status": "error", "error": "not found"})
            return
        self._proxy("DELETE", self.path[4:])

    def do_PATCH(self):
        self._send_json(403, {"status": "blocked", "error": "PATCH not supported"})


def main():
    print("=" * 64)
    print("  aiduMEI dev server  [LOCAL-TEST MODE - writes open]")
    print("=" * 64)
    print("  UI       : http://127.0.0.1:%d/" % PORT)
    print("  upstream : %s" % UPSTREAM)
    print("  WARNING  : point at LOCAL test instance, not production!")
    print("=" * 64)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
