"""f0.8+ 流 A：服务端加固回归（外审 sesame R3-12 / R3-13 / R3-14）。合成数据。

- R3-12：router_usage 的 ssh 主机名来自环境变量，以 - 开头不许被 ssh 当成选项；StrictHostKeyChecking 只认合法值。
- R3-13：UI 会话（cookie）认证的写请求必须同源（Sec-Fetch-Site / Origin），同站子域也不行。
- R3-14：/docs、/openapi.json 只给 UI 会话与主令牌，agent 钥匙看不到。
"""
from __future__ import annotations

import pytest

from tests.test_v19_4_1_auth_gate import env  # noqa: F401  复用部署形态夹具（含 DB 重定向）

PW = "verystrongpassword"


# ═══════════════ R3-12 ═══════════════

def _capture_ssh(monkeypatch, hosts: str, strict: str | None = None):
    from ducky import router_usage as R
    monkeypatch.setenv("AIDUMEM_ROUTER_USAGE_ENABLED", "1")
    monkeypatch.setenv("AIDUMEM_ROUTER_SSH_HOSTS", hosts)
    monkeypatch.setenv("AIDUMEM_ROUTER_SSH_KEY", "/nonexistent/key")
    if strict is None:
        monkeypatch.delenv("AIDUMEM_ROUTER_SSH_STRICT", raising=False)
    else:
        monkeypatch.setenv("AIDUMEM_ROUTER_SSH_STRICT", strict)
    cmds = []

    def fake(cmd, timeout=None):
        cmds.append(cmd)
        raise OSError("synthetic")
    monkeypatch.setattr(R.subprocess, "check_output", fake)
    assert R.fetch_router_llm_usage() == {}
    return cmds


def test_r3_12_dash_host_is_never_passed_to_ssh(monkeypatch):
    cmds = _capture_ssh(monkeypatch, "-oProxyCommand=touch /tmp/pwned,gw.example.invalid")
    assert len(cmds) == 1
    cmd = cmds[0]
    assert not any(a.startswith("-oProxyCommand") for a in cmd)
    i = cmd.index("--")
    assert cmd[i + 1] == "gw.example.invalid", "主机名必须在 -- 之后"


def test_r3_12_strict_option_value_is_whitelisted(monkeypatch):
    cmds = _capture_ssh(monkeypatch, "gw.example.invalid", strict="yes -oProxyCommand=x")
    assert "StrictHostKeyChecking=accept-new" in cmds[0]
    cmds = _capture_ssh(monkeypatch, "gw.example.invalid", strict="yes")
    assert "StrictHostKeyChecking=yes" in cmds[0]


# ═══════════════ R3-13 ═══════════════

def _logged_in(env):  # noqa: F811
    env.set_ui_password(PW)
    c = env.client()
    assert c.post("/api/login", json={"password": PW}).status_code == 200
    return c


@pytest.mark.parametrize("hdrs", [
    {"Sec-Fetch-Site": "same-site"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Origin": "http://evil.sub.example.invalid"},
    {"Origin": "null"},
])
def test_r3_13_session_write_from_other_origin_refused(env, hdrs):  # noqa: F811
    c = _logged_in(env)
    r = c.post("/session/distill", params={"session_id": "s-r313"}, headers=hdrs)
    assert r.status_code == 403 and r.json()["code"] == "cross_origin_session_write_refused"


@pytest.mark.parametrize("hdrs", [
    {},                                          # 非浏览器客户端：两个头都没有
    {"Sec-Fetch-Site": "same-origin"},
    {"Sec-Fetch-Site": "none"},
    {"Origin": "http://testserver"},             # 与 Host 同源
    {"Sec-Fetch-Site": "same-origin", "Origin": "https://proxy.example.invalid"},  # 浏览器判同源为准
])
def test_r3_13_same_origin_session_write_allowed(env, hdrs):  # noqa: F811
    c = _logged_in(env)
    r = c.post("/session/distill", params={"session_id": "s-r313"}, headers=hdrs)
    assert r.status_code != 403, r.text


def test_r3_13_reads_and_bearer_writes_unaffected(env):  # noqa: F811
    env.set_token("tok-secret")
    c = _logged_in(env)
    assert c.get("/api/facts", headers={"Sec-Fetch-Site": "same-site"}).status_code == 200
    c.cookies.clear()
    r = c.post("/session/distill", params={"session_id": "s-r313"},
               headers={"Authorization": "Bearer tok-secret", "Sec-Fetch-Site": "cross-site"})
    assert r.status_code != 403


# ═══════════════ R3-14 ═══════════════

@pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
def test_r3_14_agent_key_cannot_read_docs(env, path, monkeypatch, tmp_path):  # noqa: F811
    from ducky import agent_keys
    monkeypatch.delenv("AIDUMEM_PUBLIC_DOCS", raising=False)
    monkeypatch.setenv("AIDUMEI_AGENT_KEYS_FILE", str(tmp_path / "agent_keys.json"))
    env.set_token("tok-main")
    agent_keys.add_key("agent-a", ["agent-a"], "amk_synthetic_agent_key")
    c = env.client()
    r = c.get(path, headers={"Authorization": "Bearer amk_synthetic_agent_key"})
    assert r.status_code == 403 and r.json()["code"] == "docs_admin_only"
    assert c.get(path, headers={"Authorization": "Bearer tok-main"}).status_code == 200
    # agent 钥匙照常能用业务接口
    assert c.get("/api/facts", headers={"Authorization": "Bearer amk_synthetic_agent_key"}).status_code != 401


def test_r3_14_console_session_can_read_docs(env, monkeypatch):  # noqa: F811
    monkeypatch.delenv("AIDUMEM_PUBLIC_DOCS", raising=False)
    c = _logged_in(env)
    assert c.get("/openapi.json").status_code == 200
