"""
tests/test_v20_5_0_grant_authz.py — v20.5.0 正式版 联邦授权闭环守卫
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
用户审计 🔴-2 整改守卫（与 Luna P0-1/P1-1/P1-2 同源）：
  1. 冒充形态：不传 caller + 传 victim 的 agent_id → 必须 403
  2. 自签形态：grantor=victim 而 caller 是第三方 → 必须 403
  3. 合法形态：caller == grantor → 放行；admin 可为他人签发
  4. revoked_by/created_by 从 caller 派生，不可自报
  5. 逃生门 AIDUMEI_ALLOW_IMPLICIT_CALLER=1 的兼容语义
  6. 🟡-3 expires_at 非法值：创建拒绝 + 存量非法值按过期处理
  7. 🟡-4 未知 scope 维度 fail-closed（创建即拒 + 运行时不通配）
  8. 🟡-6 谱系/授权查询端点租户校验
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp_dir = tempfile.mkdtemp(prefix="aidumem_v20_5_0_authz_")
_TEST_DB = os.path.join(_tmp_dir, "facts.db")

import ducky.utils as utils  # noqa: E402

utils.FACTS_DB = _TEST_DB


@pytest.fixture(autouse=True)
def setup_test_db(monkeypatch):
    # 全量合跑时其他测试模块可能已把 FACTS_DB 改走它们的临时库，
    # 每条用例前显式指回本文件的库（与 test_v20_federation_bank_scope 同款防御）
    utils.FACTS_DB = _TEST_DB
    from ducky.schema_bootstrap import ensure_core_schema
    from ducky.federation.schema import ensure_federation_schema
    ensure_core_schema(force=True)
    ensure_federation_schema(force=True)
    # 严格默认：不留逃生门、不设 admin（各用例按需自行 monkeypatch）
    monkeypatch.delenv("AIDUMEI_ALLOW_IMPLICIT_CALLER", raising=False)
    monkeypatch.delenv("AIDUMEI_FEDERATION_ADMINS", raising=False)
    conn = utils.get_facts_conn()
    try:
        conn.execute("DELETE FROM federation_grants")
        conn.execute("DELETE FROM memory_lineage")
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


def _seed_victim_fact() -> int:
    """victim 写入一条私密事实，返回 fact_id。"""
    from ducky.federation.writer import write_fact
    res = write_fact("private", "pin", "victim 的私密记忆", agent_id="victim", dedup=False)
    assert res["status"] == "ok"
    return res["fact_id"]


# ── 1. 冒充与自签（🔴-2 两个实测复现形态）────────────────────

def test_impersonation_without_caller_gets_403():
    """🔴-2 形态①：不传 caller_agent_id、传 victim 的 agent_id → 403。"""
    _seed_victim_fact()
    client = _http_client()
    resp = client.get("/federation/recall", params={"query": "私密", "agent_id": "victim"})
    assert resp.status_code == 403, resp.text
    assert "caller_agent_id_required" in str(resp.json()["detail"])


def test_self_signed_grant_gets_403():
    """🔴-2 形态②：第三方以 victim 名义给自己签发授权 → 403。"""
    client = _http_client()
    resp = client.post("/federation/grants", params={
        "grantor_agent": "victim", "grantee_agent": "*",
        "resource_scope": "*", "actions": "read,write,delete",
        "caller_agent_id": "any_random_attacker",
    })
    assert resp.status_code == 403, resp.text
    assert "grantor_identity_mismatch" in str(resp.json()["detail"])

    # 授权确实没有落库
    from ducky.federation.grants import check_grant_permission
    assert check_grant_permission("victim", "any_random_attacker", "read") is False


def test_grant_create_requires_caller():
    """管理面连「没说自己是谁」也不认。"""
    client = _http_client()
    resp = client.post("/federation/grants", params={
        "grantor_agent": "victim", "grantee_agent": "bob",
    })
    assert resp.status_code == 403, resp.text
    assert "caller_agent_id_required" in str(resp.json()["detail"])


def test_legit_self_grant_then_recall():
    """合法形态：caller == grantor 签发 → 被授权方可读；冒充仍不行。"""
    _seed_victim_fact()
    client = _http_client()
    resp = client.post("/federation/grants", params={
        "grantor_agent": "victim", "grantee_agent": "friend",
        "actions": "read", "caller_agent_id": "victim",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["created_by"] == "victim"

    resp = client.get("/federation/recall", params={
        "query": "私密", "agent_id": "victim", "caller_agent_id": "friend",
    })
    assert resp.status_code == 200, resp.text


def test_admin_can_grant_for_others(monkeypatch):
    """admin 名单内的 caller 可为他人签发（治理面显式配置）。"""
    monkeypatch.setenv("AIDUMEI_FEDERATION_ADMINS", "ops_bot")
    client = _http_client()
    resp = client.post("/federation/grants", params={
        "grantor_agent": "victim", "grantee_agent": "auditor",
        "actions": "read", "caller_agent_id": "ops_bot",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["created_by"] == "ops_bot"


def test_revoke_derives_actor_from_caller():
    """revoked_by 从 caller 派生：owner 可撤、第三方不可撤、身份不可自报。"""
    from ducky.federation.grants import create_grant, list_grants

    gid = create_grant("victim", "friend", actions="read", created_by="victim")["grant_id"]
    client = _http_client()

    # 第三方撤销 → 403
    resp = client.post("/federation/grants/revoke", params={
        "grant_id": gid, "caller_agent_id": "attacker",
    })
    assert resp.status_code == 403, resp.text

    # owner 撤销 → ok，且 revoked_by 是 caller 本人
    resp = client.post("/federation/grants/revoke", params={
        "grant_id": gid, "caller_agent_id": "victim",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["revoked_by"] == "victim"

    row = list_grants(grantor_agent="victim", include_revoked=True)[0]
    assert row["revoked_by"] == "victim"


def test_list_grants_tenant_scoped():
    """非 admin 只能查自己签出的授权；显式查别人 → 403（🟡-6 同源）。"""
    from ducky.federation.grants import create_grant
    create_grant("alice", "bob", actions="read", created_by="alice")
    client = _http_client()

    resp = client.get("/federation/grants", params={"caller_agent_id": "alice"})
    assert resp.status_code == 200
    assert len(resp.json()) == 1 or resp.json().get("status") == "ok" or isinstance(resp.json(), list)

    resp = client.get("/federation/grants", params={
        "grantor_agent": "alice", "caller_agent_id": "bob",
    })
    assert resp.status_code == 403, resp.text


# ── 2. 逃生门语义 ────────────────────────────────────────────

def test_escape_hatch_restores_legacy_behavior(monkeypatch):
    """AIDUMEI_ALLOW_IMPLICIT_CALLER=1：旧单机请求（无 caller）恢复放行。"""
    monkeypatch.setenv("AIDUMEI_ALLOW_IMPLICIT_CALLER", "1")
    _seed_victim_fact()
    client = _http_client()
    resp = client.get("/federation/recall", params={"query": "私密", "agent_id": "victim"})
    assert resp.status_code == 200, resp.text


# ── 3. 🟡-3 expires_at 非法值 ────────────────────────────────

def test_expires_at_garbage_rejected_at_create():
    from ducky.federation.grants import create_grant
    res = create_grant("a", "b", expires_at="garbage", created_by="a")
    assert res["status"] == "error"
    assert "expires_at" in res["detail"]


def test_expires_at_garbage_in_storage_treated_as_expired():
    """存量非法值（历史库/手工写入）按已过期处理，不得静默变永久。"""
    from ducky.federation.grants import check_grant_permission, create_grant

    gid = create_grant("a", "b", actions="read", created_by="a")["grant_id"]
    conn = utils.get_facts_conn()
    try:
        conn.execute("UPDATE federation_grants SET expires_at='garbage' WHERE grant_id=?", (gid,))
        conn.commit()
    finally:
        conn.close()
    assert check_grant_permission("a", "b", "read") is False


# ── 4. 🟡-4 未知 scope 维度 fail-closed ─────────────────────

def test_match_scope_unknown_dimension_rejected():
    from ducky.federation.grants import _match_scope
    assert _match_scope("owner:alice", category="x") is False
    assert _match_scope("catgory:tasks", category="tasks") is False  # 拼错一个字母
    assert _match_scope("category:finance;owner:alice", category="finance") is False
    assert _match_scope("category:finance", category="finance") is True
    assert _match_scope("*") is True


def test_create_grant_rejects_unknown_scope_dimension():
    from ducky.federation.grants import create_grant
    res = create_grant("a", "b", resource_scope="owner:alice", created_by="a")
    assert res["status"] == "error"
    assert "未知 scope 维度" in res["detail"]


# ── 5. 🟡-6 谱系查询端点租户校验 ─────────────────────────────

def test_lineage_query_enforces_ownership():
    """alice 可查自己事实的谱系；bob 查 → 403；无 caller → 403。"""
    from ducky.federation.writer import write_fact
    r = write_fact("private", "pin", "alice 的秘密", agent_id="alice", dedup=False)
    mid = f"fact:{r['fact_id']}"
    client = _http_client()

    resp = client.get("/federation/lineage", params={"memory_id": mid, "caller_agent_id": "alice"})
    assert resp.status_code == 200, resp.text

    resp = client.get("/federation/lineage", params={"memory_id": mid, "caller_agent_id": "bob"})
    assert resp.status_code == 403, resp.text

    resp = client.get("/federation/lineage", params={"memory_id": mid})
    assert resp.status_code == 403, resp.text


def test_lineage_verify_scope_rules():
    """单链 verify 走归属校验；全库 verify 仅 admin。"""
    from ducky.federation.writer import write_fact
    r = write_fact("private", "pin", "alice 的秘密", agent_id="alice", dedup=False)
    mid = f"fact:{r['fact_id']}"
    client = _http_client()

    resp = client.get("/federation/lineage/verify", params={"memory_id": mid, "caller_agent_id": "alice"})
    assert resp.status_code == 200, resp.text

    resp = client.get("/federation/lineage/verify", params={"caller_agent_id": "alice"})
    assert resp.status_code == 403, resp.text
