"""v20.4.0 P1 面守卫合集：SSE 逐请求认证（真 socket）、限流兜底桶、
/add 清洗统一、Form 上限、TRUST_PROXY 启动 WARN。

来源：三方审计 Codex P1-03 / Kimi P2-1、P2-3 / 动态审计 🟡-2、🟡-3。
"""

import socket
import threading
import time

import httpx
import pytest


# ── P1-7 · MCP SSE 逐请求认证（真 uvicorn socket，TestClient 不算数）──

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_sse_transport_requires_bearer_on_nonloopback_form(monkeypatch):
    """非回环形态（loopback=False 包装）下：无/错 Authorization → 401，
    正确 token → 放行进 MCP SSE（200 流）。真 socket + 真 uvicorn。"""
    monkeypatch.setenv("AIDUMEM_API_TOKEN", "sse-guard-token-1")
    # 本机 SOCKS 代理会掐死 httpx（reference_socks_proxy_httpx_trap）：剥干净
    for var in ("ALL_PROXY", "all_proxy", "http_proxy", "https_proxy",
                "HTTP_PROXY", "HTTPS_PROXY"):
        monkeypatch.delenv(var, raising=False)
    ms = pytest.importorskip("mcp_server")

    app = ms._build_sse_app_with_auth(ms.mcp, loopback=False)
    import uvicorn
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn 没起来"
    base = f"http://127.0.0.1:{port}"
    try:
        r = httpx.get(f"{base}/sse", timeout=5, trust_env=False)
        assert r.status_code == 401, f"无凭据连 SSE 应 401, got {r.status_code}"
        r = httpx.get(f"{base}/sse", headers={"Authorization": "Bearer wrong"}, timeout=5, trust_env=False)
        assert r.status_code == 401, f"错误 token 应 401, got {r.status_code}"
        # 正确 token：SSE 是长连接，用流式打开拿到状态码即断开
        with httpx.Client(trust_env=False, timeout=5) as hc, \
                hc.stream("GET", f"{base}/sse",
                          headers={"Authorization": "Bearer sse-guard-token-1"}) as resp:
            assert resp.status_code == 200, f"正确 token 应放行, got {resp.status_code}"
    finally:
        server.should_exit = True
        th.join(timeout=10)


def test_sse_loopback_form_stays_open(monkeypatch):
    """回环形态不加逐请求认证（与 api_server 回环信任模型同口径）——
    包装函数对 loopback=True 必须原样返回内层 app。"""
    monkeypatch.setenv("AIDUMEM_API_TOKEN", "sse-guard-token-2")
    ms = pytest.importorskip("mcp_server")
    inner_marker = ms.mcp.sse_app(mount_path="/sse").__class__
    wrapped = ms._build_sse_app_with_auth(ms.mcp, loopback=True)
    assert isinstance(wrapped, inner_marker), "回环形态被套了认证壳 —— 契约变了"


# ── P1-8 · 限流兜底桶（轮换 user_id 绕不过全局上限）──

def test_global_bucket_blocks_user_id_rotation():
    from ducky import rate_guard as rg
    base = time.time()
    route = "guard-global-probe"
    # 每次换一个 user_id：按租户桶永不超（每租户 1 次 < limit 5），
    # 全局桶在第 11 个请求拦住。
    for i in range(10):
        assert rg.check_rate(route, f"rotate-{i}", limit=5, global_limit=10,
                             now=base + i * 0.01) is None, f"第 {i+1} 发不该被拦"
    retry = rg.check_rate(route, "rotate-999", limit=5, global_limit=10,
                          now=base + 0.5)
    assert retry is not None and retry >= 1, "轮换 user_id 打穿了全局桶（Kimi P2-3 原文）"


def test_per_tenant_bucket_unchanged():
    from ducky import rate_guard as rg
    base = time.time()
    route = "guard-tenant-probe"
    for i in range(3):
        assert rg.check_rate(route, "same-user", limit=3, global_limit=0,
                             now=base + i * 0.01) is None
    assert rg.check_rate(route, "same-user", limit=3, global_limit=0,
                         now=base + 0.5) is not None, "按租户桶被兜底桶改动弄坏了"


# ── P1-9 · /add 清洗统一（控制字符不入库，结构保留）──

def test_sanitize_messages_struct_preserves_shape():
    from ducky.security.injection_guard import sanitize_messages_struct
    msgs = [{"role": "user", "content": "abc\x01def"},
            {"role": "assistant", "content": "ok\x00"}]
    out = sanitize_messages_struct(msgs)
    assert out[0]["content"] == "abcdef" and out[1]["content"] == "ok"
    assert out[0]["role"] == "user", "清洗把结构洗坏了"
    assert sanitize_messages_struct("a\x1fb\nc") == "ab\nc", "换行必须保留"
    assert sanitize_messages_struct({"content": "x\x0by", "k": 1}) == {"content": "xy", "k": 1}


def test_add_async_preview_is_sanitized(tmp_path, monkeypatch):
    """端到端可观测点：async 回执的 preview 来自净化后的全文。"""
    monkeypatch.setenv("AIDUMEM_LOG_DIR", str(tmp_path / "logs"))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    class _MemStub:
        @staticmethod
        def add(content, user_id=None, metadata=None, infer=False, **kw):
            return {"results": [{"id": "s1", "memory": str(content)}]}
        @staticmethod
        def search(query, **kw):
            return {"results": []}
        @staticmethod
        def get_all(**kw):
            return {"results": []}

    import ducky.hot.add as add_mod
    monkeypatch.setattr(add_mod, "get_memory", lambda: _MemStub(), raising=False)
    monkeypatch.setattr(add_mod, "patch_llm_for_speed", lambda mem: None, raising=False)
    from ducky.hot.add import register_add_routes
    app = FastAPI()
    register_add_routes(app)
    client = TestClient(app)
    r = client.post("/add", json={
        "messages": [{"role": "user", "content": "raw\x01control\x02chars kept?"}],
        "user_id": "san-u1", "infer": False, "async_mode": True,
    })
    assert r.status_code == 200, r.text[:200]
    preview = r.json().get("preview", "")
    assert "\x01" not in preview and "\x02" not in preview, (
        "async preview 仍带控制字符 —— /add 还在丢弃清洗结果（Kimi P2-1）")
    assert "rawcontrolchars" in preview.replace(" ", ""), preview


# ── P1-11 · Form 上限 ──

def test_facts_compress_form_has_length_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDUMEM_LOG_DIR", str(tmp_path / "logs"))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ducky.extended.routes import register_extended_routes
    from ducky.api_models import TEXT_FIELD_MAX_CHARS
    app = FastAPI()
    # 替身签名对齐生产的注册面：三个依赖注入参数（本测试只打 Form 校验层，
    # 校验在进入业务体之前由 FastAPI 完成，占位实现不会被 422 路径触达）
    from ducky.utils import get_facts_conn
    register_extended_routes(app, lambda: None, get_facts_conn, lambda t: [])
    client = TestClient(app)
    too_long = "x" * (TEXT_FIELD_MAX_CHARS + 1)
    r = client.post("/facts/compress", data={"text": too_long})
    assert r.status_code == 422, (
        f"Form 全文超上限应 422（动态审计 🟡-3）, got {r.status_code}")
    ok = client.post("/facts/compress", data={"text": "error: one line\nnormal line"})
    assert ok.status_code == 200, ok.text[:200]


# ── P1-10 · TRUST_PROXY 启动 WARN ──

def test_trust_proxy_without_credential_warns_three_concessions(monkeypatch, caplog):
    import logging
    import api_server as srv
    monkeypatch.setenv("AIDUMEI_TRUST_PROXY", "1")
    monkeypatch.delenv("AIDUMEM_API_TOKEN", raising=False)
    monkeypatch.setattr(srv, "_auth_enabled", lambda: False)
    monkeypatch.setattr(srv, "_detect_bind_host", lambda: "127.0.0.1")
    with caplog.at_level(logging.WARNING):
        srv._enforce_public_binding_policy()
    text = caplog.text
    assert "三道防线" in text, "无凭据 + TRUST_PROXY=1 没有打醒目 WARN（动态审计 🟡-2）"
    for marker in ("X-Forwarded-For", "Host", "跨站"):
        assert marker in text, f"WARN 没点名让渡项 {marker}"
