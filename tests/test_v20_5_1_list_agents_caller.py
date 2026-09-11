"""
tests/test_v20_5_1_list_agents_caller.py — T-08：GET /federation/agents 补 caller 门槛
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
缺陷形态：同文件五个管理/查询端点（create_grant / list_grants / revoke_grant /
get_lineage / verify_lineage）都接 `_require_caller` fail-closed，唯独
`federation_list_agents` 裸调 `registry_mod.list_agents` —— 联邦里有哪些
Agent、各自挂了多少事实、谁在线，对「没说自己是谁」的请求全量敞开。
Agent 清单本身就是侦察面（拿它才能挑受害者 agent_id 去试 grants）。

判据（先红后绿）：
  1. 无 caller → 403 caller_agent_id_required（修复前此用例红：裸奔 200）
  2. 合法 caller → 200，列表内容与库内注册一致（集合相等）
  3. 逃生门 AIDUMEI_ALLOW_IMPLICIT_CALLER=1 → 旧单机行为原样恢复
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp_dir = tempfile.mkdtemp(prefix="aidumem_v20_5_1_list_agents_")
_TEST_DB = os.path.join(_tmp_dir, "facts.db")

import ducky.utils as utils  # noqa: E402

utils.FACTS_DB = _TEST_DB


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch):
    # 全量合跑时其他模块可能已把 FACTS_DB 改走它们的临时库，逐用例指回
    utils.FACTS_DB = _TEST_DB
    from ducky.schema_bootstrap import ensure_core_schema
    from ducky.federation.schema import ensure_federation_schema
    ensure_core_schema(force=True)
    ensure_federation_schema(force=True)
    # 严格默认：不留逃生门、不设 admin、不配 caller 绑定（T-07 另有专测）
    monkeypatch.delenv("AIDUMEI_ALLOW_IMPLICIT_CALLER", raising=False)
    monkeypatch.delenv("AIDUMEI_FEDERATION_ADMINS", raising=False)
    monkeypatch.delenv("AIDUMEI_CALLER_BINDINGS", raising=False)
    conn = utils.get_facts_conn()
    try:
        conn.execute("DELETE FROM agents")
        conn.execute("DELETE FROM facts")
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()
    yield


def _http_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ducky.federation.routes import register_federation_routes
    app = FastAPI()
    register_federation_routes(app)
    return TestClient(app)


def _seed_two_agents():
    from ducky.federation.registry import register_agent
    register_agent("agent_alpha", display_name="甲")
    register_agent("agent_beta", display_name="乙")


def test_list_agents_without_caller_gets_403():
    """无 caller → 403 caller_agent_id_required（与 create_grant 等五端点同构）。"""
    _seed_two_agents()
    client = _http_client()
    resp = client.get("/federation/agents")
    assert resp.status_code == 403, resp.text
    assert "caller_agent_id_required" in str(resp.json()["detail"])


def test_list_agents_with_caller_passes_and_lists_all():
    """合法 caller → 放行；返回集合与注册表逐员相等（不写「返回了 A」式弱断言）。"""
    _seed_two_agents()
    client = _http_client()
    resp = client.get("/federation/agents", params={"caller_agent_id": "agent_alpha"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert {a["agent_id"] for a in body["agents"]} == {"agent_alpha", "agent_beta"}
    assert body["count"] == 2


def test_list_agents_escape_hatch_restores_legacy_behavior(monkeypatch):
    """AIDUMEI_ALLOW_IMPLICIT_CALLER=1：旧单机请求（无 caller）恢复放行。"""
    monkeypatch.setenv("AIDUMEI_ALLOW_IMPLICIT_CALLER", "1")
    _seed_two_agents()
    client = _http_client()
    resp = client.get("/federation/agents")
    assert resp.status_code == 200, resp.text
    assert {a["agent_id"] for a in resp.json()["agents"]} == {"agent_alpha", "agent_beta"}


def test_list_agents_filters_still_forwarded_with_caller():
    """caller 门槛不能把原有查询参数吃掉：profile / include_inactive 照旧生效。"""
    from ducky.federation.registry import deactivate_agent, register_agent
    register_agent("agent_alpha", profile="ops")
    register_agent("agent_beta", profile="chat")
    deactivate_agent("agent_beta")
    client = _http_client()

    resp = client.get("/federation/agents", params={
        "caller_agent_id": "agent_alpha", "profile": "ops",
    })
    assert resp.status_code == 200, resp.text
    assert {a["agent_id"] for a in resp.json()["agents"]} == {"agent_alpha"}

    resp = client.get("/federation/agents", params={
        "caller_agent_id": "agent_alpha", "include_inactive": False,
    })
    assert resp.status_code == 200, resp.text
    assert {a["agent_id"] for a in resp.json()["agents"]} == {"agent_alpha"}
