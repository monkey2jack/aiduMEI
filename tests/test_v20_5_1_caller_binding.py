"""
tests/test_v20_5_1_caller_binding.py — T-07：caller_agent_id 与凭据的轻量绑定
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
背景：`_require_caller` 已 fail-closed，但 caller 始终是**自报参数**，与
Bearer token 无绑定 —— 拿着合法 token 的人可以在 caller_agent_id 里
填写任意 agent_id。本轮做轻量版：token → 允许代表的 agent_id 白名单。

设计（最小穿透，兼容红线 = **未配置时行为与现状逐字一致**）：
  · 新增可选 env `AIDUMEI_CALLER_BINDINGS`：
    JSON `{"<token_sha256前16位>": ["agent_a", "agent_b"]}`
  · HTTP 中间件 Bearer / X-API-Token 校验通过时，把 token 指纹挂进
    请求上下文（contextvar）；联邦层 `_require_caller` 读取它。
  · 强制条件**同时**成立才拦：① 配置了 bindings；② 本请求 token 指纹
    已登记在 bindings。其余形态（无 token / token 未登记 / 未配置 env）
    一律按现状放行。
  · bindings 配置了但 JSON 非法 → fail-closed 403（安全档配置写错
    不能静默失效，与 _evidence_gate_on「非法值按开」同一家训）。

判据（先红后绿）：实现前 1~6 全部红（ helpers 不存在 / 校验不存在）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_tmp_dir = tempfile.mkdtemp(prefix="aidumem_v20_5_1_binding_")
_TEST_FACTS_DB = os.path.join(_tmp_dir, "facts.db")
_TEST_TEXT_DB = os.path.join(_tmp_dir, "text_fts.db")

import ducky.utils as utils  # noqa: E402

utils.FACTS_DB = _TEST_FACTS_DB
utils.TEXT_FTS_DB = _TEST_TEXT_DB

from ducky.security import auth as auth_mod  # noqa: E402
from ducky.federation.routes import _require_caller  # noqa: E402

_TOKEN_A = "unittest-token-alpha"
_TOKEN_B = "unittest-token-beta"  # 合法凭据但从未登记进 bindings


@pytest.fixture(autouse=True)
def setup_env(monkeypatch):
    utils.FACTS_DB = _TEST_FACTS_DB
    utils.TEXT_FTS_DB = _TEST_TEXT_DB
    from ducky.schema_bootstrap import ensure_core_schema
    from ducky.federation.schema import ensure_federation_schema
    ensure_core_schema(force=True)
    ensure_federation_schema(force=True)
    # 严格默认：不配 bindings、不配 token、不留逃生门、不设 admin
    monkeypatch.delenv("AIDUMEI_CALLER_BINDINGS", raising=False)
    monkeypatch.delenv("AIDUMEM_API_TOKEN", raising=False)
    monkeypatch.delenv("AIDUMEI_ALLOW_IMPLICIT_CALLER", raising=False)
    monkeypatch.delenv("AIDUMEI_FEDERATION_ADMINS", raising=False)
    # contextvar 是同进程状态，逐用例清零，防用例间泄漏
    auth_mod.clear_request_token_fingerprint()
    conn = utils.get_facts_conn()
    try:
        conn.execute("DELETE FROM federation_grants")
        conn.commit()
    except Exception:
        pass
    finally:
        conn.close()
    yield
    auth_mod.clear_request_token_fingerprint()


def _bindings_env(monkeypatch, mapping: dict) -> None:
    monkeypatch.setenv("AIDUMEI_CALLER_BINDINGS", json.dumps(mapping))


def _detail_error(exc) -> str:
    detail = exc.detail
    return detail.get("error", "") if isinstance(detail, dict) else str(detail)


# ── 1. 单元级：_require_caller × bindings 矩阵 ─────────────────────

def test_bound_token_impersonating_outside_whitelist_gets_403(monkeypatch):
    """token A 登记了白名单 [agent_a]，却自报 caller=agent_b → 必败。"""
    _bindings_env(monkeypatch, {auth_mod.fingerprint_token(_TOKEN_A): ["agent_a"]})
    auth_mod.set_request_token_fingerprint(_TOKEN_A)
    with pytest.raises(Exception) as exc_info:
        _require_caller("agent_b", operation="create_grant")
    assert getattr(exc_info.value, "status_code", None) == 403
    assert _detail_error(exc_info.value) == "caller_token_binding_mismatch"


def test_bound_token_within_whitelist_passes(monkeypatch):
    """白名单内的 caller 原样放行，返回值是规范化后的 caller。"""
    _bindings_env(monkeypatch, {auth_mod.fingerprint_token(_TOKEN_A): ["agent_a", "agent_b"]})
    auth_mod.set_request_token_fingerprint(_TOKEN_A)
    assert _require_caller("agent_b", operation="list_grants") == "agent_b"


def test_unbound_but_known_token_is_not_constrained(monkeypatch):
    """token B 是合法凭据但没登记进 bindings → 不强制（只约束「已声明范围」的 token）。"""
    _bindings_env(monkeypatch, {auth_mod.fingerprint_token(_TOKEN_A): ["agent_a"]})
    auth_mod.set_request_token_fingerprint(_TOKEN_B)
    assert _require_caller("agent_b", operation="list_agents") == "agent_b"


def test_no_token_on_request_is_not_constrained(monkeypatch):
    """请求根本没带 Bearer（session cookie / 门禁关闭 / 直连库调用）→ 不强制。"""
    _bindings_env(monkeypatch, {auth_mod.fingerprint_token(_TOKEN_A): ["agent_a"]})
    assert auth_mod.current_request_token_fingerprint() == ""
    assert _require_caller("agent_b", operation="revoke_grant") == "agent_b"


def test_bindings_unconfigured_keeps_legacy_behavior():
    """★兼容红线：不配 AIDUMEI_CALLER_BINDINGS 时，带不带 token 都逐字旧行为。"""
    auth_mod.set_request_token_fingerprint(_TOKEN_A)
    assert _require_caller("any_agent", operation="get_lineage") == "any_agent"


def test_broken_bindings_json_fails_closed(monkeypatch):
    """bindings 写错（非法 JSON）→ 403，绝不静默失效（fail-closed）。"""
    monkeypatch.setenv("AIDUMEI_CALLER_BINDINGS", "{not-json")
    auth_mod.set_request_token_fingerprint(_TOKEN_A)
    with pytest.raises(Exception) as exc_info:
        _require_caller("agent_a", operation="list_agents")
    assert getattr(exc_info.value, "status_code", None) == 403
    assert _detail_error(exc_info.value) == "caller_bindings_misconfigured"


def test_implicit_caller_escape_hatch_not_bound(monkeypatch):
    """逃生门放行的空 caller 不参与绑定（没有身份可绑）——旧单机语义不变。"""
    monkeypatch.setenv("AIDUMEI_ALLOW_IMPLICIT_CALLER", "1")
    _bindings_env(monkeypatch, {auth_mod.fingerprint_token(_TOKEN_A): ["agent_a"]})
    auth_mod.set_request_token_fingerprint(_TOKEN_A)
    assert _require_caller("", operation="list_agents") == ""


# ── 2. 穿透级：真实中间件 → contextvar → 联邦端点 ─────────────────────
# 判据落在完整 app（api_server.app）上： Bearer 过闸时指纹必须真的到达
# _require_caller —— 只测单元等于宣称「穿透方案可行」而未证。

def _full_app_client():
    import api_server
    from fastapi.testclient import TestClient
    return TestClient(api_server.app)


def test_http_bound_token_cannot_act_as_other_agent(monkeypatch):
    """中间件→联邦层穿透：token A 持白名单 [agent_a]，caller=agent_b 必 403。"""
    monkeypatch.setenv("AIDUMEM_API_TOKEN", _TOKEN_A)
    _bindings_env(monkeypatch, {auth_mod.fingerprint_token(_TOKEN_A): ["agent_a"]})
    client = _full_app_client()
    resp = client.post(
        "/federation/grants",
        params={"grantor_agent": "agent_b", "grantee_agent": "agent_c",
                "actions": "read", "caller_agent_id": "agent_b"},
        headers={"Authorization": f"Bearer {_TOKEN_A}"},
    )
    assert resp.status_code == 403, resp.text
    assert "caller_token_binding_mismatch" in str(resp.json()["detail"])


def test_http_bound_token_within_whitelist_succeeds(monkeypatch):
    """白名单内 caller（=grantor 本人）→ 200，created_by 从 caller 派生。"""
    monkeypatch.setenv("AIDUMEM_API_TOKEN", _TOKEN_A)
    _bindings_env(monkeypatch, {auth_mod.fingerprint_token(_TOKEN_A): ["agent_a"]})
    client = _full_app_client()
    resp = client.post(
        "/federation/grants",
        params={"grantor_agent": "agent_a", "grantee_agent": "agent_c",
                "actions": "read", "caller_agent_id": "agent_a"},
        headers={"Authorization": f"Bearer {_TOKEN_A}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["created_by"] == "agent_a"


def test_http_without_bindings_token_behavior_unchanged(monkeypatch):
    """★兼容红线（HTTP 层）：只配 token、不配 bindings —— caller 照旧只过
    caller 门槛与 grantor 一致性校验，不出现任何绑定错误码。"""
    monkeypatch.setenv("AIDUMEM_API_TOKEN", _TOKEN_A)
    client = _full_app_client()
    resp = client.post(
        "/federation/grants",
        params={"grantor_agent": "agent_b", "grantee_agent": "agent_c",
                "actions": "read", "caller_agent_id": "agent_b"},
        headers={"Authorization": f"Bearer {_TOKEN_A}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["created_by"] == "agent_b"


def test_http_unregistered_token_not_constrained_by_bindings(monkeypatch):
    """bindings 里只登记了 token A；请求带的是未登记的合法 token B →
    绑定不触发，原有身份校验（grantor 一致性）照旧接管。"""
    monkeypatch.setenv("AIDUMEM_API_TOKEN", _TOKEN_B)
    _bindings_env(monkeypatch, {auth_mod.fingerprint_token(_TOKEN_A): ["agent_a"]})
    client = _full_app_client()
    # grantor==caller：原有校验放行，绑定不拦
    resp = client.post(
        "/federation/grants",
        params={"grantor_agent": "agent_b", "grantee_agent": "agent_c",
                "actions": "read", "caller_agent_id": "agent_b"},
        headers={"Authorization": f"Bearer {_TOKEN_B}"},
    )
    assert resp.status_code == 200, resp.text
    # grantor!=caller：403 必须是**旧码** grantor_identity_mismatch，
    # 证明绑定层没有插手（插手会先抛出 caller_token_binding_mismatch）
    resp = client.post(
        "/federation/grants",
        params={"grantor_agent": "agent_a", "grantee_agent": "agent_c",
                "actions": "read", "caller_agent_id": "agent_b"},
        headers={"Authorization": f"Bearer {_TOKEN_B}"},
    )
    assert resp.status_code == 403, resp.text
    assert "grantor_identity_mismatch" in str(resp.json()["detail"])


def test_http_x_api_token_header_also_carries_fingerprint(monkeypatch):
    """X-API-Token 与 Bearer 是同一道门钥匙 B 的两种递法，绑定对两者同规。"""
    monkeypatch.setenv("AIDUMEM_API_TOKEN", _TOKEN_A)
    _bindings_env(monkeypatch, {auth_mod.fingerprint_token(_TOKEN_A): ["agent_a"]})
    client = _full_app_client()
    resp = client.post(
        "/federation/grants",
        params={"grantor_agent": "agent_b", "grantee_agent": "agent_c",
                "actions": "read", "caller_agent_id": "agent_b"},
        headers={"X-API-Token": _TOKEN_A},
    )
    assert resp.status_code == 403, resp.text
    assert "caller_token_binding_mismatch" in str(resp.json()["detail"])
