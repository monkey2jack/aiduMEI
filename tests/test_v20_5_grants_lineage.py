"""
tests/test_v20_5_grants_lineage.py — aiduMEI v20.5.0a 联邦授权与记忆谱系针对性测试
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
验证：
  1. federation_grants 表与 memory_lineage 表 schema 幂等初始化与 facts 谱系字段
  2. 联邦授权 Grant 创建、权限判定（动作 actions、作用域 resource_scope、租户 user_id）
  3. 授权过期失效（expires_at 判定）与即时撤销机制（revoke_grant）
  4. 越权拒绝（未授权跨 Agent 访问/读写默认拒绝）
  5. 记忆谱系（Memory Lineage）SHA-256 哈希计算与链式版本演化（version、previous_version_hash）
  6. 谱系完整性与断链自检（verify_lineage_integrity）
  7. 联邦写入（write_fact）及更新、冲突消解时密码学谱系账本的同生共死同事务保障
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp_dir = tempfile.mkdtemp(prefix="aidumem_v20_5_test_")
_TEST_DB = os.path.join(_tmp_dir, "facts.db")

import ducky.utils as utils  # noqa: E402

utils.FACTS_DB = _TEST_DB


@pytest.fixture(autouse=True)
def setup_test_db():
    """每次测试前保证测试库包含核心 schema 与联邦/谱系迁移，并清空授权表防止污染。"""
    from ducky.schema_bootstrap import ensure_core_schema
    from ducky.federation.schema import ensure_federation_schema
    ensure_core_schema(force=True)
    ensure_federation_schema(force=True)
    conn = utils.get_facts_conn()
    try:
        conn.execute("DELETE FROM federation_grants")
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()
    yield


# ── 1. 联邦授权 Grants 测试 ──────────────────────────────────

def test_grants_create_and_list():
    from ducky.federation.grants import create_grant, list_grants

    res = create_grant("agent_a", "agent_b", resource_scope="category:security", actions="read,write")
    assert res["status"] == "ok"
    gid = res["grant_id"]

    all_grants = list_grants(grantor_agent="agent_a")
    assert any(g["grant_id"] == gid for g in all_grants)
    target = next(g for g in all_grants if g["grant_id"] == gid)
    assert target["grantee_agent"] == "agent_b"
    assert target["resource_scope"] == "category:security"
    assert "read" in target["actions"]
    assert "write" in target["actions"]


def test_grants_permission_checking_scope():
    from ducky.federation.grants import check_grant_permission, create_grant

    # 同一 Agent 始终允许
    assert check_grant_permission("agent_a", "agent_a", "read") is True

    # 未授权时默认拒绝
    assert check_grant_permission("agent_a", "agent_b", "read") is False

    # 授权只读 category:finance
    create_grant("agent_a", "agent_b", resource_scope="category:finance", actions="read")

    # 命中 scope 和 action 允许
    assert check_grant_permission("agent_a", "agent_b", "read", category="finance") is True
    # 动作不匹配拒绝
    assert check_grant_permission("agent_a", "agent_b", "write", category="finance") is False
    # scope 不匹配拒绝
    assert check_grant_permission("agent_a", "agent_b", "read", category="health") is False


def test_grants_instant_revocation():
    from ducky.federation.grants import check_grant_permission, create_grant, revoke_grant

    res = create_grant("agent_x", "agent_y", resource_scope="*", actions="read")
    gid = res["grant_id"]

    assert check_grant_permission("agent_x", "agent_y", "read") is True

    # 撤销授权
    rev = revoke_grant(gid, revoked_by="admin_monkey")
    assert rev["status"] == "ok"

    # 即时失效
    assert check_grant_permission("agent_x", "agent_y", "read") is False


def test_grants_expiration():
    from ducky.federation.grants import check_grant_permission, create_grant

    # 创建一个已过期的授权
    past_iso = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    create_grant("agent_p", "agent_q", resource_scope="*", actions="read", expires_at=past_iso)

    assert check_grant_permission("agent_p", "agent_q", "read") is False

    # 创建一个未来的有效授权
    future_iso = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    create_grant("agent_p", "agent_r", resource_scope="*", actions="read", expires_at=future_iso)

    assert check_grant_permission("agent_p", "agent_r", "read") is True


def test_grants_wildcard_grantee():
    from ducky.federation.grants import check_grant_permission, create_grant

    # 允许全局 Agent 访问 public 分类
    create_grant("agent_host", "*", resource_scope="category:public", actions="read")

    assert check_grant_permission("agent_host", "agent_any_1", "read", category="public") is True
    assert check_grant_permission("agent_host", "agent_any_2", "read", category="public") is True
    assert check_grant_permission("agent_host", "agent_any_2", "read", category="private") is False


# ── 2. 记忆密码学谱系 Lineage 测试 ──────────────────────────

def test_compute_content_hash_deterministic():
    from ducky.memory_lineage import compute_content_hash

    h1 = compute_content_hash("重要指令：对得起每一位用户")
    h2 = compute_content_hash("重要指令：对得起每一位用户")
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex string

    h_empty = compute_content_hash("")
    assert h_empty == "0" * 64


def test_lineage_version_chain():
    from ducky.memory_lineage import (
        get_memory_lineage,
        record_lineage,
        verify_lineage_integrity,
    )

    conn = utils.get_facts_conn()
    try:
        # 版本 1
        v1 = record_lineage(
            conn,
            memory_id="test:mem:101",
            content="小猫今天吃了小饼干",
            action="CREATE",
            actor="monkey",
            source="user",
        )
        assert v1["version"] == 1
        assert v1["previous_version_hash"] == ""

        # 版本 2
        v2 = record_lineage(
            conn,
            memory_id="test:mem:101",
            content="小猫今天吃了小饼干和苹果",
            action="UPDATE",
            actor="monkey",
            source="user",
        )
        assert v2["version"] == 2
        assert v2["previous_version_hash"] == v1["content_hash"]

        # 版本 3
        v3 = record_lineage(
            conn,
            memory_id="test:mem:101",
            content="小猫今天吃了小饼干、苹果和草莓",
            action="CONFLICT_RESOLVE",
            actor="conflict_resolver",
            source="conflict",
        )
        assert v3["version"] == 3
        assert v3["previous_version_hash"] == v2["content_hash"]

        conn.commit()
    finally:
        conn.close()

    # 查询历史谱系链
    chain = get_memory_lineage("test:mem:101")
    assert len(chain) == 3
    assert [c["version"] for c in chain] == [1, 2, 3]
    assert chain[1]["previous_version_hash"] == chain[0]["content_hash"]
    assert chain[2]["previous_version_hash"] == chain[1]["content_hash"]

    # 完整性校验
    audit = verify_lineage_integrity("test:mem:101")
    assert audit["status"] == "ok"
    assert audit["broken_count"] == 0


def test_lineage_detects_broken_chain():
    from ducky.memory_lineage import verify_lineage_integrity

    conn = utils.get_facts_conn()
    try:
        # 人工注入一条父哈希篡改的损坏链
        conn.execute(
            """INSERT INTO memory_lineage
               (memory_id, version, content_hash, previous_version_hash, action, actor)
               VALUES ('tampered:mem', 1, 'hash_1111111111111111111111111111111111111111111111111111111111111111', '', 'CREATE', 'test')"""
        )
        conn.execute(
            """INSERT INTO memory_lineage
               (memory_id, version, content_hash, previous_version_hash, action, actor)
               VALUES ('tampered:mem', 2, 'hash_2222222222222222222222222222222222222222222222222222222222222222', 'WRONG_PARENT_HASH', 'UPDATE', 'test')"""
        )
        conn.commit()
    finally:
        conn.close()

    audit = verify_lineage_integrity("tampered:mem")
    assert audit["status"] == "broken"
    assert audit["broken_count"] > 0
    assert "父哈希断链" in audit["broken_details"][0]["reason"]


# ── 3. 写入端点 (write_fact) 谱系与版本自增集成测试 ────────

def test_write_fact_populates_lineage_and_hashes():
    from ducky.federation.writer import write_fact
    from ducky.memory_lineage import compute_content_hash

    res1 = write_fact(
        "测试谱系", "项A", "事实初始内容", agent_id="agent_lineage_test", dedup=False
    )
    assert res1["status"] == "ok"
    fid = res1["fact_id"]

    conn = utils.get_facts_conn()
    try:
        row = conn.execute(
            "SELECT content_hash, version, previous_version_hash, last_actor FROM facts WHERE id=?",
            (fid,),
        ).fetchone()
        assert row is not None
        assert row[0] == compute_content_hash("事实初始内容")
        assert row[1] == 1
        assert row[2] == ""
    finally:
        conn.close()

    # 再次更新该事实
    res2 = write_fact(
        "测试谱系", "项A", "事实升级内容", agent_id="agent_lineage_test", dedup=True
    )
    assert res2["status"] == "ok"

    conn = utils.get_facts_conn()
    try:
        row2 = conn.execute(
            "SELECT content_hash, version, previous_version_hash, last_actor FROM facts WHERE id=?",
            (fid,),
        ).fetchone()
        assert row2[0] == compute_content_hash("事实升级内容")
        assert row2[1] == 2
        assert row2[2] == compute_content_hash("事实初始内容")
    finally:
        conn.close()


# ── 4. dedup merge 谱系推进（P0-3 射程补全）────────────────

def test_dedup_merge_advances_lineage_and_hash():
    """merge 是事实正文真实变更（self-edit SQL 兜底），hash/version 必须同步推进。"""
    from ducky.federation.dedup import apply_merge
    from ducky.federation.writer import write_fact
    from ducky.memory_lineage import compute_content_hash, get_memory_lineage

    res = write_fact("合并谱系区", "merge键", "短内容版本", agent_id="agent_merge_l",
                     dedup=False)
    assert res["status"] == "ok"
    fid = res["fact_id"]

    m = apply_merge(fid, "这是一段明显更长的合并内容版本，按保留信息量更大原则胜出")
    assert m["status"] == "ok"

    conn = utils.get_facts_conn()
    try:
        row = conn.execute(
            "SELECT content_hash, version, previous_version_hash FROM facts WHERE id=?",
            (fid,),
        ).fetchone()
        assert row[0] == compute_content_hash(
            "这是一段明显更长的合并内容版本，按保留信息量更大原则胜出")
        assert row[1] == 2
        assert row[2] == compute_content_hash("短内容版本")
    finally:
        conn.close()

    chain = get_memory_lineage(f"fact:{fid}")
    actions = [c["action"] for c in chain]
    assert "MERGE" in actions, f"merge 未记谱系: {actions}"
    assert chain[-1]["version"] >= 2


# ── 5. HTTP 端点 Grant 策略拦截（P0-2 织入验证）────────────

def _http_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ducky.federation.routes import register_federation_routes
    app = FastAPI()
    register_federation_routes(app)
    return TestClient(app)


def test_endpoint_cross_agent_without_grant_gets_403():
    """跨 Agent 读写：无 Grant → 403 默认拒绝（不再认 shared 单方标记）。"""
    client = _http_client()

    # recall：agent_b 以 caller 身份检索 agent_a 名下记忆
    resp = client.get("/federation/recall", params={
        "query": "任意", "agent_id": "agent_a", "caller_agent_id": "agent_b",
    })
    assert resp.status_code == 403, resp.text
    assert "federation_grant_required" in str(resp.json()["detail"])

    # facts/add：跨 Agent 写入
    resp = client.post("/federation/facts/add", params={
        "category": "x", "fact_key": "k", "fact_value": "v",
        "agent_id": "agent_a", "caller_agent_id": "agent_b",
    })
    assert resp.status_code == 403, resp.text

    # broadcast / awareness：跨 Agent 拉取
    resp = client.get("/federation/broadcast", params={
        "agent_id": "agent_a", "caller_agent_id": "agent_b",
    })
    assert resp.status_code == 403, resp.text
    resp = client.get("/federation/awareness", params={
        "agent_id": "agent_a", "caller_agent_id": "agent_b",
    })
    assert resp.status_code == 403, resp.text


def test_endpoint_grant_allows_then_revoke_blocks():
    """授权后放行 200；即时撤销后再访问 403——毫秒级生效无残留窗口。"""
    from ducky.federation.grants import create_grant, revoke_grant

    client = _http_client()
    create_grant("agent_a", "agent_b", resource_scope="*", actions="read,write")

    resp = client.get("/federation/recall", params={
        "query": "咖啡", "agent_id": "agent_a", "caller_agent_id": "agent_b",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"

    # 撤销即时生效
    grants = [g for g in __import__("ducky.federation.grants", fromlist=["list_grants"]).list_grants(
        grantor_agent="agent_a", grantee_agent="agent_b")]
    gid = grants[-1]["grant_id"]
    assert revoke_grant(gid, revoked_by="agent_a")["status"] == "ok"

    resp = client.get("/federation/recall", params={
        "query": "咖啡", "agent_id": "agent_a", "caller_agent_id": "agent_b",
    })
    assert resp.status_code == 403, resp.text


def test_endpoint_read_grant_cannot_write():
    """动作谓词精确隔离：read Grant 不许跨 Agent 写。"""
    from ducky.federation.grants import create_grant

    client = _http_client()
    create_grant("agent_a", "agent_b", resource_scope="*", actions="read")

    resp = client.get("/federation/recall", params={
        "query": "x", "agent_id": "agent_a", "caller_agent_id": "agent_b",
    })
    assert resp.status_code == 200, resp.text

    resp = client.post("/federation/facts/add", params={
        "category": "x", "fact_key": "k", "fact_value": "v",
        "agent_id": "agent_a", "caller_agent_id": "agent_b",
    })
    assert resp.status_code == 403, "read Grant 不得隐含 write"


def test_endpoint_legacy_single_agent_unchanged():
    """向下兼容：不传 caller_agent_id 的旧单机请求零破坏（回环放行）。"""
    client = _http_client()

    resp = client.get("/federation/recall", params={"query": "任意", "agent_id": "agent_a"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"

    resp = client.get("/federation/recall", params={
        "query": "任意", "agent_id": "agent_a", "caller_agent_id": "agent_a",
    })
    assert resp.status_code == 200, "本 Agent 回环必须放行"


def test_endpoint_expired_grant_rejected():
    """过期授权自动拒绝（403），不残留访问窗口。"""
    from ducky.federation.grants import create_grant

    past_iso = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    create_grant("agent_a", "agent_c", resource_scope="*", actions="read",
                 expires_at=past_iso)

    client = _http_client()
    resp = client.get("/federation/recall", params={
        "query": "x", "agent_id": "agent_a", "caller_agent_id": "agent_c",
    })
    assert resp.status_code == 403, "过期 Grant 不得放行"
