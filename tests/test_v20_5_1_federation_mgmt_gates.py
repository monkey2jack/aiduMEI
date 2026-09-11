"""v20.5.1（T-06 · 根因 R-1 接缝排查）：联邦管理面注册/休眠端点的 caller 门槛。

活体证据：`_require_caller` 在 v20.5.0 已 fail-closed 守住 grants/lineage 五端点，
但同文件的 register/deactivate 没接——门槛存在而端点没接上，正是 R-1
「判据被算出但未强制写入最终执行路径」的形态。本文件把两条接缝钉死：

- register：upsert 语义会改写他人 display_name/endpoint，且 ON CONFLICT 把
  已休眠 agent 重新激活（deactivate 被 register 反制）；
- deactivate：无门槛时任何持 Bearer 者可休眠任意 agent（联邦面 DoS）。

判据：无 caller 拒绝（逃生门关闭时）；caller ≠ agent_id 且非 admin 拒绝；
caller == agent_id 放行；admin 放行。
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ducky.federation.routes import register_federation_routes


@pytest.fixture()
def fed_client(tmp_path, monkeypatch):
    """独立 facts 库的联邦路由 client（绝不碰真实 data/）。"""
    monkeypatch.setenv("AIDUMEM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AIDUMEI_ALLOW_IMPLICIT_CALLER", raising=False)
    monkeypatch.delenv("AIDUMEI_CALLER_BINDINGS", raising=False)
    monkeypatch.delenv("AIDUMEI_FEDERATION_ADMINS", raising=False)
    app = FastAPI()
    register_federation_routes(app)
    return TestClient(app, raise_server_exceptions=False)


def test_register_requires_caller(fed_client):
    r = fed_client.post("/federation/agents/register",
                        params={"agent_id": "evil"})
    assert r.status_code in (400, 403), r.text
    assert "caller_agent_id_required" in r.text


def test_register_self_allowed(fed_client):
    r = fed_client.post("/federation/agents/register",
                        params={"agent_id": "dudu", "caller_agent_id": "dudu"})
    assert r.status_code == 200, r.text
    assert r.json().get("agent_id") == "dudu"


def test_register_other_forbidden(fed_client):
    r = fed_client.post("/federation/agents/register",
                        params={"agent_id": "dudu", "caller_agent_id": "evil"})
    assert r.status_code == 403, r.text
    assert "register_forbidden" in r.text


def test_register_cannot_resurrect_deactivated(fed_client):
    """deactivate 后被他人 register 复活，正是本组要堵的反制链。"""
    fed_client.post("/federation/agents/register",
                    params={"agent_id": "dudu", "caller_agent_id": "dudu"})
    fed_client.post("/federation/agents/deactivate",
                    params={"agent_id": "dudu", "caller_agent_id": "dudu"})
    r = fed_client.post("/federation/agents/register",
                        params={"agent_id": "dudu", "caller_agent_id": "evil"})
    assert r.status_code == 403, r.text
    agents = fed_client.get(
        "/federation/agents", params={"caller_agent_id": "dudu"}
    ).json()["agents"]
    dudu = next(a for a in agents if a["agent_id"] == "dudu")
    assert dudu["active"] == 0, "被休眠的 agent 被他人 register 复活了"


def test_deactivate_requires_caller(fed_client):
    r = fed_client.post("/federation/agents/deactivate",
                        params={"agent_id": "dudu"})
    assert r.status_code in (400, 403), r.text


def test_deactivate_other_forbidden(fed_client):
    fed_client.post("/federation/agents/register",
                    params={"agent_id": "dudu", "caller_agent_id": "dudu"})
    r = fed_client.post("/federation/agents/deactivate",
                        params={"agent_id": "dudu", "caller_agent_id": "evil"})
    assert r.status_code == 403, r.text
    assert "deactivate_forbidden" in r.text


def test_admin_can_manage_others(fed_client, monkeypatch):
    monkeypatch.setenv("AIDUMEI_FEDERATION_ADMINS", "ops")
    fed_client.post("/federation/agents/register",
                    params={"agent_id": "dudu", "caller_agent_id": "dudu"})
    r = fed_client.post("/federation/agents/deactivate",
                        params={"agent_id": "dudu", "caller_agent_id": "ops"})
    assert r.status_code == 200, r.text


def test_escape_hatch_preserves_legacy(fed_client, monkeypatch):
    """逃生门开启时，无 caller 的旧单机调用形态保持兼容。"""
    monkeypatch.setenv("AIDUMEI_ALLOW_IMPLICIT_CALLER", "1")
    r = fed_client.post("/federation/agents/register",
                        params={"agent_id": "legacy"})
    assert r.status_code == 200, r.text
