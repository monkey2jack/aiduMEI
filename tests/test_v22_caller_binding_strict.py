"""v22.0 雷霆审计 A2 · caller↔凭据绑定第三态守卫

背景：`_enforce_caller_binding` 的旧逻辑是「BINDINGS 未配置 → 放行；
配置了但本 token 指纹未登记 → 也放行」。第二条造成「per-token 手动登记，
新 token 默认裸奔」——运维加一把 token 就静默开一个洞。

v22.0 引入 AIDUMEI_CALLER_BINDING_MODE 第三态：
- off（默认/非法值）  逐字 v21.x：未登记即放行（兼容红线）
- permissive         未登记 WARN 放行（迁移窗口）
- strict             未登记 403（消灭新 token 裸奔）

负向对照：改回「未登记即 return」必须让 strict 用例变红。
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def bindings_env(monkeypatch):
    """配好 BINDINGS + 一个已登记指纹 + 一个未登记指纹。"""
    import json
    table = {"registeredfp0001": ["agent_a"]}
    monkeypatch.setenv("AIDUMEI_CALLER_BINDINGS", json.dumps(table))
    yield table
    monkeypatch.delenv("AIDUMEI_CALLER_BINDINGS", raising=False)
    monkeypatch.delenv("AIDUMEI_CALLER_BINDING_MODE", raising=False)


def _set_fp(monkeypatch, fp: str):
    """打桩当前请求的 token 指纹。"""
    import ducky.security.auth as auth_mod
    monkeypatch.setattr(auth_mod, "current_request_token_fingerprint", lambda: fp, raising=False)


def test_mode_off_unregistered_passes(bindings_env, monkeypatch):
    """兼容红线：mode 不在场时，未登记指纹逐字走 v21.x 放行。"""
    monkeypatch.delenv("AIDUMEI_CALLER_BINDING_MODE", raising=False)
    _set_fp(monkeypatch, "unregistered9999")
    from ducky.federation.routes import _enforce_caller_binding
    _enforce_caller_binding("agent_b", "test_op")  # 不抛 = 通过


def test_mode_permissive_warns_but_passes(bindings_env, monkeypatch):
    """迁移窗口：未登记放行（但记 WARN）。"""
    monkeypatch.setenv("AIDUMEI_CALLER_BINDING_MODE", "permissive")
    _set_fp(monkeypatch, "unregistered9999")
    from ducky.federation.routes import _enforce_caller_binding
    _enforce_caller_binding("agent_b", "test_op")  # 不抛 = 通过


def test_mode_strict_rejects_unregistered(bindings_env, monkeypatch):
    """v22.0 核心：strict 下未登记指纹必须 403。"""
    from fastapi import HTTPException
    monkeypatch.setenv("AIDUMEI_CALLER_BINDING_MODE", "strict")
    _set_fp(monkeypatch, "unregistered9999")
    from ducky.federation.routes import _enforce_caller_binding
    with pytest.raises(HTTPException) as ei:
        _enforce_caller_binding("agent_b", "test_op")
    assert ei.value.status_code == 403
    assert ei.value.detail["error"] == "token_fingerprint_unregistered"


def test_mode_strict_registered_still_enforces_whitelist(bindings_env, monkeypatch):
    """strict 下已登记指纹：白名单外 caller 仍拦，白名单内放行。"""
    from fastapi import HTTPException
    monkeypatch.setenv("AIDUMEI_CALLER_BINDING_MODE", "strict")
    _set_fp(monkeypatch, "registeredfp0001")
    from ducky.federation.routes import _enforce_caller_binding
    _enforce_caller_binding("agent_a", "test_op")  # 白名单内 → 通过
    with pytest.raises(HTTPException) as ei:
        _enforce_caller_binding("agent_z", "test_op")
    assert ei.value.status_code == 403
    assert ei.value.detail["error"] == "caller_token_binding_mismatch"


def test_no_fingerprint_session_cookie_unaffected(bindings_env, monkeypatch):
    """session cookie / 无凭据回环不带指纹 → 不受 strict 影响（兼容红线）。"""
    monkeypatch.setenv("AIDUMEI_CALLER_BINDING_MODE", "strict")
    _set_fp(monkeypatch, "")  # 无指纹
    from ducky.federation.routes import _enforce_caller_binding
    _enforce_caller_binding("agent_b", "test_op")  # 不抛 = 通过


def test_invalid_mode_falls_back_to_off(bindings_env, monkeypatch):
    """非法值按 off（与「安全档配置写错不能静默失效」相反——mode 是新增逃生门，
    非法值保守回落旧行为，不制造新断点）。"""
    monkeypatch.setenv("AIDUMEI_CALLER_BINDING_MODE", "garbage")
    _set_fp(monkeypatch, "unregistered9999")
    from ducky.federation.routes import _enforce_caller_binding
    _enforce_caller_binding("agent_b", "test_op")  # 不抛
