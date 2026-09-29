"""Bearer-backed Core and operational reads must declare their target hall."""

from __future__ import annotations

import ast
import copy
import importlib.util
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "f02-core-caller-test-token"
USER = "f02-other-hall"


@contextmanager
def _gated_server():
    """Reject the old Bearer shape and record the real HTTP requests."""
    calls: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _handle(self) -> None:
            parts = urllib.parse.urlsplit(self.path)
            query = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length)) if length else {}
            calls.append({"method": self.command, "path": parts.path, "query": query,
                          "body": body, "auth": self.headers.get("Authorization")})
            scope = query if self.command == "GET" else body
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                self._reply(401, {"detail": "bearer required"})
            elif scope.get("caller_user_id") != scope.get("user_id"):
                self._reply(403, {"detail": "caller must declare target hall"})
            elif parts.path.startswith("/api/core-memory"):
                self._reply(200, {"status": "ok", "user_id": scope["user_id"],
                                  "bank_id": scope.get("bank_id"), "path": parts.path})
            elif parts.path == "/search":
                self._reply(200, {"status": "ok", "results": [{"memory": "probe"}]})
            else:
                self._reply(404, {"detail": "unknown path"})

        def do_GET(self):  # noqa: N802
            self._handle()

        def do_POST(self):  # noqa: N802
            self._handle()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", calls
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def _load_mcp_core_functions(api_get):
    """Run the real tool bodies without requiring the optional MCP package."""
    tree = ast.parse((ROOT / "mcp_server.py").read_text(encoding="utf-8"))
    names = {"core_memory_list", "core_memory_get"}
    nodes = [copy.deepcopy(node) for node in tree.body
             if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in nodes} == names
    for node in nodes:
        node.decorator_list = []
    namespace = {
        "DEFAULT_USER_ID": "default", "DEFAULT_BANK_ID": "default",
        "_api_get": api_get, "_ok": json.dumps, "urllib": urllib,
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 "mcp_server.py", "exec"), namespace)
    return namespace


def test_gated_server_rejects_old_undeclared_caller_shape():
    """Negative control: a client omitting caller must see 403, even for default."""
    with _gated_server() as (base, _):
        for path, method, data in (
            ("/api/core-memory?user_id=default", "GET", None),
            ("/search", "POST", json.dumps({"query": "x", "user_id": USER}).encode()),
        ):
            req = urllib.request.Request(base + path, data=data, method=method,
                                         headers={"Authorization": f"Bearer {TOKEN}",
                                                  "Content-Type": "application/json"})
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(req, timeout=5)
            assert exc.value.code == 403


def test_mcp_core_tools_send_query_scope_for_default_and_custom_halls():
    with _gated_server() as (base, calls):
        def api_get(path, params=None):
            url = base + path + "?" + urllib.parse.urlencode(params or {})
            req = urllib.request.Request(url,
                                         headers={"Authorization": f"Bearer {TOKEN}"})
            with urllib.request.urlopen(req, timeout=5) as response:
                return json.load(response)

        tools = _load_mcp_core_functions(api_get)
        default = json.loads(tools["core_memory_list"]())
        custom = json.loads(tools["core_memory_list"](USER, "work"))
        block = json.loads(tools["core_memory_get"]("pref identity", USER, "work"))

    assert default["user_id"] == "default"
    assert custom["user_id"] == block["user_id"] == USER
    assert custom["bank_id"] == block["bank_id"] == "work"
    assert block["path"] == "/api/core-memory/pref%20identity"
    assert len(calls) == 3
    for call in calls:
        assert call["auth"] == f"Bearer {TOKEN}"
        assert call["query"]["caller_user_id"] == call["query"]["user_id"]


def test_integration_smoke_search_declares_its_synthetic_hall(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location(
        "integration_smoke_caller", ROOT / "tests/integration_smoke_api.py")
    script = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(script)
    with _gated_server() as (base, calls):
        monkeypatch.setattr(script, "API_BASE", base)
        monkeypatch.setattr(script, "_SMOKE_USER", USER)
        monkeypatch.setattr(script, "api_auth_headers",
                            lambda: {"Authorization": f"Bearer {TOKEN}"})
        assert script.test_07_search_read_path() is True
    capsys.readouterr()
    assert len(calls) == 1
    assert calls[0]["path"] == "/search"
    assert calls[0]["body"]["caller_user_id"] == calls[0]["body"]["user_id"] == USER


def test_consolidator_search_declares_requested_hall(monkeypatch):
    import scripts.consolidator as script

    with _gated_server() as (base, calls):
        monkeypatch.setattr(script, "API_BASE", base)
        monkeypatch.setattr(script, "_auth_headers",
                            lambda: {"Authorization": f"Bearer {TOKEN}"})
        assert script._get_all_via_api(USER, limit=3) == [{"memory": "probe"}]

    assert len(calls) == 1
    assert calls[0]["path"] == "/search"
    assert calls[0]["body"]["caller_user_id"] == calls[0]["body"]["user_id"] == USER
    assert calls[0]["body"]["limit"] == 3
