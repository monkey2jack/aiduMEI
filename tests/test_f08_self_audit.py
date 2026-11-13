"""f0.8.0 自审（41439a2..HEAD 安全相关 diff 通读）发现项的反向用例。合成数据。

1. 记忆桥不从项目共享 `.claude/settings.json` 读 `autoMemoryDirectory`（随仓库提交的文件
   能把「记忆目录」指到 `~/` 或仓库本身，桥会把 CC 在那里改的任意 .md 改写并导入记忆）。
2. MCP SSE 认证包装：websocket 作用域也要凭据（此前非 http 作用域一律直通）。
3. 出站校验：NAT64 前缀里内嵌的元数据地址，放行名单也放不过。
"""
from __future__ import annotations

import asyncio
import json

from ducky.security import egress
from scripts.cc_memory_sweep import B
from tests.test_f08_cc_memory_bridge import _topic, cc  # noqa: F401  复用沙箱


# ── 1. 项目共享设置不能改记忆目录 ──

def test_project_shared_settings_cannot_redirect_memory_dir(cc, tmp_path):  # noqa: F811
    repo = tmp_path / "cloned-repo"
    (repo / ".claude").mkdir(parents=True)
    (repo / ".claude" / "settings.json").write_text(json.dumps({"autoMemoryDirectory": str(repo)}))
    target = repo / "README.md"
    target.write_text("# 合成仓库\n口令 password=Synthetic-Pass-123\n", encoding="utf-8")
    pl = {"tool_name": "Write", "tool_input": {"file_path": str(target)}, "cwd": str(repo),
          "session_id": "cc-s1"}
    assert B.auto_memory_setting(str(repo)) is None
    assert all(d != repo for d in B.memory_dirs_for(pl))
    assert B.hook_target(pl) is None, "共享仓库把自己设成了记忆目录，桥却认了"


def test_project_local_and_user_settings_still_honored(cc, tmp_path, monkeypatch):  # noqa: F811
    repo = tmp_path / "proj"
    (repo / ".claude").mkdir(parents=True)
    mine = cc["home"] / "local-mem"
    mine.mkdir()
    (repo / ".claude" / "settings.local.json").write_text(json.dumps({"autoMemoryDirectory": str(mine)}))
    # f0.8+（R3-02）：项目级 local 设置只在落进 AIDUMEI_CC_MEMORY_ROOTS 白名单时才采信
    assert B.auto_memory_setting(str(repo)) is None
    monkeypatch.setenv("AIDUMEI_CC_MEMORY_ROOTS", str(mine))
    assert B.auto_memory_setting(str(repo)) == mine
    (repo / ".claude" / "settings.local.json").unlink()
    (cc["home"] / ".claude" / "settings.json").write_text(json.dumps({"autoMemoryDirectory": "~/local-mem"}))
    assert B.auto_memory_setting(str(repo)) == mine


# ── 2. MCP SSE websocket 作用域 ──

def test_mcp_sse_websocket_scope_requires_token(monkeypatch):
    import mcp_server as ms
    monkeypatch.setattr(ms, "api_auth_headers", lambda: {"Authorization": "Bearer sse-tok"})
    monkeypatch.delenv("AIDUMEM_ALLOW_INSECURE_PUBLIC", raising=False)
    reached = []

    async def inner(scope, receive, send):
        reached.append(scope["type"])

    monkeypatch.setattr(ms.mcp, "sse_app", lambda mount_path="/sse": inner)
    app = ms._build_sse_app_with_auth(ms.mcp, loopback=True, host="127.0.0.1")

    async def call(kind, headers):
        sent = []

        async def receive():
            return {"type": "websocket.connect"} if kind == "websocket" else {"type": "lifespan.startup"}

        async def send(msg):
            sent.append(msg)

        await app({"type": kind, "path": "/sse", "headers": headers}, receive, send)
        return sent

    sent = asyncio.run(call("websocket", []))
    assert reached == [] and sent and sent[0]["type"] == "websocket.close"
    asyncio.run(call("websocket", [(b"authorization", b"Bearer sse-tok")]))
    assert reached == ["websocket"]
    asyncio.run(call("lifespan", []))
    assert reached == ["websocket", "lifespan"]          # lifespan 直通


# ── 3. NAT64 内嵌元数据地址 ──

def test_nat64_embedded_metadata_blocked_even_when_allowlisted(monkeypatch):
    monkeypatch.setenv("AIDUMEI_MODEL_ENDPOINT_ALLOW", "nat64-host.example,64:ff9b::/96")
    egress.set_resolver(lambda h: ["64:ff9b::a9fe:a9fe"] if h == "nat64-host.example" else [])
    try:
        assert egress.check_url("https://nat64-host.example/v1"), "放行名单主机解析到 NAT64 元数据地址"
        assert egress.check_url("http://[64:ff9b::a9fe:a9fe]/latest")
        assert egress.is_metadata_ip(__import__("ipaddress").ip_address("64:ff9b::a9fe:a9fe"))
    finally:
        egress.set_resolver(None)
