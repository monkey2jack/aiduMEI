"""v20.3.2-beta P0-1：默认安装 + 同机反代 = 全站零凭据可读写。

外审 C-1（WorkBuddy/Hy4 独家发现，小猴实机复现）。这条链**每一环都是对的**，
合起来是个洞：

    默认安装 → 自动生成口令（source=auto）
    → ui_password_configured() 刻意不认 auto → _auth_enabled() = False
    → 中间件进「无凭据 → 只服务回环」分支
    → 判据是 request.client.host（TCP 对端 IP）
    → 同机 nginx 反代的对端恒为 127.0.0.1
    → **全部路由放行**

为什么是 P0 而不是配置失误：这是**默认安装路径的终点**，而「域名 + 同机反代」
正是本项目的标准生产形态。`X-Forwarded-For` 不可信是对的（rate_guard.py:121
已经写明拒绝采信），但**不采信的代价 —— 反代后对端恒为回环 —— 没被算进去**。

修法（fail-closed）：无凭据实例遇到**任何反代痕迹**一律拒绝，除非部署方显式
声明 AIDUMEI_TRUST_PROXY。不采信 XFF 的**值**，只采信它的**存在**——
存在即证明「这个请求经过了一跳」，而无凭据实例不该服务任何经过跳转的请求。
"""
import importlib
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def bare_app(tmp_path, monkeypatch):
    """零凭据实例：自动生成口令，不设 token，不设 UI 口令。"""
    monkeypatch.setenv("AIDUMEM_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("AIDUMEM_LOG_DIR", str(tmp_path / "l"))
    for k in ("AIDUMEM_API_TOKEN", "AIDUMEM_UI_PASSWORD", "AIDUMEI_TRUST_PROXY"):
        monkeypatch.delenv(k, raising=False)
    import api_server as A
    importlib.reload(A) if False else None
    A._ensure_ui_password()
    assert A._auth_enabled() is False, "夹具前提破了：这个实例本该是无凭据形态"
    return A


_PROXY_HEADERS = [
    {"X-Forwarded-For": "203.0.113.7"},
    {"X-Real-IP": "203.0.113.7"},
    {"Forwarded": "for=203.0.113.7;proto=https"},
    {"X-Forwarded-Host": "memory.example.com"},
]


@pytest.mark.parametrize("headers", _PROXY_HEADERS)
def test_no_credential_instance_refuses_proxied_requests(bare_app, headers):
    """**P0-1 靶心**：无凭据 + 反代痕迹 → 必须拒绝。

    修复前：对端是 127.0.0.1（nginx 同机），判为回环，全部路由放行。
    """
    c = TestClient(bare_app.app, raise_server_exceptions=False)
    r = c.get("/facts", params={"user_id": "probe", "bank_id": "default"}, headers=headers)
    assert r.status_code == 503, (
        f"无凭据实例接受了带反代痕迹（{list(headers)[0]}）的请求 → {r.status_code}；"
        "同机反代下这等于全站零凭据可读写"
    )
    assert r.json().get("code") == "no_credential_public_access_denied"


def test_direct_loopback_still_works_without_credentials(bare_app):
    """**回归**：纯本机直连（无代理头）必须照旧放行。

    hermes 插件 / MCP / cron 三条存量集成从不带凭据也不带代理头 ——
    这条守的就是它们，把它们打断的修法一律不许上。
    """
    c = TestClient(bare_app.app, raise_server_exceptions=False)
    r = c.get("/facts", params={"user_id": "probe", "bank_id": "default"})
    assert r.status_code != 503, "把无凭据的本机直连打断了：存量集成会集体 503"


@pytest.mark.parametrize("headers", _PROXY_HEADERS[:2])
def test_trust_proxy_opt_in_restores_proxied_access(bare_app, monkeypatch, headers):
    """显式声明可信反代后，代理请求恢复放行（逃生阀存在且生效）。"""
    monkeypatch.setenv("AIDUMEI_TRUST_PROXY", "1")
    c = TestClient(bare_app.app, raise_server_exceptions=False)
    r = c.get("/facts", params={"user_id": "probe", "bank_id": "default"}, headers=headers)
    assert r.status_code != 503, "部署方已显式声明可信反代，仍被拒 —— 逃生阀没接线"


def test_credentialed_instance_is_unaffected_by_proxy_headers(tmp_path, monkeypatch):
    """**负向对照**：配了凭据的实例走正常鉴权，与代理头无关。

    这条防的是「把判据写宽成一律拒绝代理头」—— 那会让所有反代部署集体不可用。
    """
    monkeypatch.setenv("AIDUMEM_DATA_DIR", str(tmp_path / "d2"))
    monkeypatch.setenv("AIDUMEM_LOG_DIR", str(tmp_path / "l2"))
    monkeypatch.setenv("AIDUMEM_API_TOKEN", "probe-token-not-a-real-secret")
    monkeypatch.delenv("AIDUMEI_TRUST_PROXY", raising=False)
    import api_server as A
    assert A._auth_enabled() is True, "夹具前提破了：这个实例本该有凭据"
    c = TestClient(A.app, raise_server_exceptions=False)
    r = c.get("/facts", params={"user_id": "probe", "bank_id": "default"},
              headers={"X-Forwarded-For": "203.0.113.7",
                       "Authorization": "Bearer probe-token-not-a-real-secret"})
    assert r.status_code != 503, "有凭据的反代部署被误拒 —— 判据写太宽"


def test_proxy_signal_helper_does_not_trust_header_values(bare_app):
    """判据只看代理头**在不在**，不看它的**值** —— 值可伪造，存在性不可否认。"""
    assert bare_app._request_via_proxy({"x-forwarded-for": "127.0.0.1"}) is True, (
        "伪造成 127.0.0.1 的 XFF 骗过了判据 —— 判据在读值而不是读存在性")
    assert bare_app._request_via_proxy({}) is False
