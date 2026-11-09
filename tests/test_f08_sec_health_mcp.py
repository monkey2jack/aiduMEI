"""f0.8 ③-4 /health 完整视图只给管理面；③-7 MCP SSE 回环也要凭据。"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

MAIN = "main-token-f08-health-0001"
_FULL_ONLY = ("git_sha", "full_version", "modules", "degraded_details", "warnings")


@pytest.fixture()
def client(monkeypatch, tmp_path):
    from ducky.hot import health as H
    monkeypatch.setenv("AIDUMEM_API_TOKEN", MAIN)
    monkeypatch.setenv("AIDUMEI_AGENT_KEYS_FILE", str(tmp_path / "agent_keys.json"))
    H._PUBLIC_CACHE.update(ts=0.0, full=None)
    app = FastAPI()
    H.register_health_routes(app)
    return TestClient(app)


def _is_public(body: dict) -> bool:
    return set(body) == {"status", "version", "health_status", "probes"} and \
        set(body["probes"]) == {"_redacted", "runtime_paths"}


def test_anonymous_gets_minimal_view(client):
    body = client.get("/health").json()
    assert _is_public(body)
    assert not any(k in body for k in _FULL_ONLY)


def test_agent_key_gets_public_view_not_full(client):
    """外审 deepseek F-13：绑定单一租户的 agent 钥匙此前拿到完整载荷（git_sha + 绝对路径）。"""
    from ducky import agent_keys
    tok = agent_keys.new_token()
    agent_keys.add_key("aliceagent", ["alice"], tok)
    body = client.get("/health", headers={"Authorization": f"Bearer {tok}"}).json()
    assert _is_public(body), sorted(body)
    assert "/" not in str(body["probes"]).replace("Bearer <AIDUMEM_API_TOKEN>", "")


def test_main_token_gets_full_view(client):
    body = client.get("/health", headers={"Authorization": f"Bearer {MAIN}"}).json()
    assert "git_sha" in body and "probes" in body and "degraded" in body
    assert "_redacted" not in body["probes"]


def test_bound_main_token_still_gets_full_view(client, tmp_path):
    """单一合法客户端（巡检用主钥匙读 /health）：主钥匙在 agent_keys.json 里绑了身份也照样完整。"""
    import json
    from ducky import agent_keys
    tok = agent_keys.new_token()
    agent_keys.add_key("other", ["bob"], tok)
    data = json.loads((tmp_path / "agent_keys.json").read_text())
    data["main"] = {"agent": "owner", "callers": ["owner"]}
    agent_keys.save(data)
    body = client.get("/health", headers={"Authorization": f"Bearer {MAIN}"}).json()
    assert "git_sha" in body and "_redacted" not in body["probes"]
    body = client.get("/health", headers={"X-API-Token": MAIN}).json()
    assert "git_sha" in body


def test_wrong_token_gets_public_view(client):
    assert _is_public(client.get("/health", headers={"Authorization": "Bearer nope"}).json())


def test_gate_off_keeps_full_view(monkeypatch, tmp_path):
    from ducky.hot import health as H
    monkeypatch.delenv("AIDUMEM_API_TOKEN", raising=False)
    monkeypatch.setenv("AIDUMEI_AGENT_KEYS_FILE", str(tmp_path / "none.json"))
    monkeypatch.setattr(H, "_auth_gate_enabled", lambda: False)
    app = FastAPI()
    H.register_health_routes(app)
    body = TestClient(app).get("/health").json()
    assert "git_sha" in body


def test_full_view_allowed_matrix():
    from ducky import agent_keys
    from ducky.hot import health as H
    from ducky.security import auth
    assert H._full_view_allowed(False, False) is True
    assert H._full_view_allowed(True, False) is False
    auth.set_request_auth_kind("session")
    agent_keys.clear_current()
    assert H._full_view_allowed(True, True) is True
    auth.set_request_auth_kind("bearer")
    agent_keys.set_current({"agent": "a", "callers": ["a"]}, "fp")
    assert H._full_view_allowed(True, True) is False
    agent_keys.set_current(None, "fp")
    assert H._full_view_allowed(True, True) is True
    agent_keys.clear_current()
    auth.clear_request_auth_kind()


# ── MCP SSE ──

def test_mcp_sse_loopback_without_token_refuses(monkeypatch):
    import mcp_server as ms
    monkeypatch.setattr(ms, "api_auth_headers", lambda: {})
    monkeypatch.delenv("AIDUMEM_ALLOW_INSECURE_PUBLIC", raising=False)
    assert ms._sse_authorization_allowed("127.0.0.1") is False


def test_mcp_sse_loopback_wrapped_rejects_missing_and_wrong_token(monkeypatch):
    """回环形态此前原样返回内层 app：本机任何进程连 SSE 即继承主 token 权限。"""
    import asyncio
    import mcp_server as ms
    monkeypatch.setattr(ms, "api_auth_headers", lambda: {"Authorization": "Bearer sse-tok"})
    app = ms._build_sse_app_with_auth(ms.mcp, loopback=True, host="127.0.0.1")

    async def call(headers, path="/sse"):
        sent = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg):
            sent.append(msg)
            if msg["type"] == "http.response.start" and msg["status"] != 401:
                raise asyncio.CancelledError  # 真进了 SSE 流就断开

        scope = {"type": "http", "method": "GET", "path": path, "raw_path": path.encode(),
                 "query_string": b"", "headers": headers, "client": ("127.0.0.1", 5555),
                 "server": ("127.0.0.1", 8766), "scheme": "http", "root_path": "",
                 "http_version": "1.1", "asgi": {"version": "3.0"}}
        try:
            await app(scope, receive, send)
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        return next((m["status"] for m in sent if m["type"] == "http.response.start"), None)

    assert asyncio.run(call([])) == 401
    assert asyncio.run(call([(b"authorization", b"Bearer wrong")])) == 401
    assert asyncio.run(call([], path="/messages/")) == 401
    assert asyncio.run(call([(b"authorization", b"Bearer sse-tok")])) != 401


def test_main_token_full_view_without_auth_kind_context(monkeypatch):
    """主 token 判定不依赖中间件写入的 auth kind（api_server 不可导入 / 独立挂载时也成立）。"""
    from types import SimpleNamespace
    from ducky.hot import health as H
    from ducky.security import auth
    monkeypatch.setenv("AIDUMEM_API_TOKEN", MAIN)
    auth.clear_request_auth_kind()
    good = SimpleNamespace(headers={"Authorization": f"Bearer {MAIN}"})
    alt = SimpleNamespace(headers={"X-API-Token": MAIN})
    bad = SimpleNamespace(headers={"Authorization": "Bearer not-the-main-token"})
    assert H._full_view_allowed(True, True, good) is True
    assert H._full_view_allowed(True, True, alt) is True
    assert H._full_view_allowed(True, True, bad) is False
    assert H._full_view_allowed(True, False, good) is False      # 未通过鉴权一律不给
