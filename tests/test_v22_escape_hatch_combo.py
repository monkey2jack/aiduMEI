"""v22.0 雷霆审计 A7 · 逃逸门组合闸守卫

GLM F-04 / Muse P1-2 / Gemini P0-04 互证：三个安全逃生舱
（ALLOW_INSECURE_PUBLIC / TRUST_PROXY / ALLOW_IMPLICIT_CALLER）相互独立、
无组合校验——全开 + 无凭据时公网裸奔只剩 WARNING。

v22.0：INSECURE_PUBLIC ∧ TRUST_PROXY ∧ 无凭据 → 拒绝启动，
除非 AIDUMEI_I_CONFIRM_PUBLIC_NO_AUTH 逐字等于监听地址（防 1/true 蒙混）。
/health 增加 unsafe_combo 探针让组合态可见。
"""
from __future__ import annotations

import pytest


def _call_policy(monkeypatch, *, host="0.0.0.0", insecure=True, trust=True,
                 confirm=None, has_auth=False):
    """在受控环境里调 _enforce_public_binding_policy。

    模块级 import 状态会被其他测试污染（conftest 先 import api_server），
    所以这里不打桩模块属性，而是用 monkeypatch.setenv + 直接调函数，
    让函数自己读 env（与生产路径一致）。
    """
    import api_server
    # 打桩三个谓词使行为只由参数决定（模块级缓存已污染）
    monkeypatch.setattr(api_server, "_detect_bind_host", lambda: host)
    monkeypatch.setattr(api_server, "_trust_proxy_enabled", lambda: trust)
    monkeypatch.setattr(api_server, "_auth_enabled", lambda: has_auth)
    if insecure:
        monkeypatch.setenv("AIDUMEM_ALLOW_INSECURE_PUBLIC", "1")
    else:
        monkeypatch.delenv("AIDUMEM_ALLOW_INSECURE_PUBLIC", raising=False)
    if confirm is not None:
        monkeypatch.setenv("AIDUMEI_I_CONFIRM_PUBLIC_NO_AUTH", confirm)
    else:
        monkeypatch.delenv("AIDUMEI_I_CONFIRM_PUBLIC_NO_AUTH", raising=False)
    return api_server._enforce_public_binding_policy()


def test_combo_denied_without_confirmation(monkeypatch):
    """A7 核心：INSECURE_PUBLIC ∧ TRUST_PROXY ∧ 无凭据 → 拒绝启动。"""
    with pytest.raises(RuntimeError) as ei:
        _call_policy(monkeypatch)
    assert "INSECURE_PUBLIC + TRUST_PROXY" in str(ei.value), str(ei.value)


def test_combo_allowed_with_exact_host_confirmation(monkeypatch):
    """确认变量逐字等于监听地址 → 放行（知情部署）。"""
    _call_policy(monkeypatch, confirm="0.0.0.0")  # 不抛 = 通过


def test_confirmation_wrong_value_denied(monkeypatch):
    """确认变量值不对（1/true/别的地址）→ 仍拒绝。"""
    for bad in ("1", "true", "127.0.0.1", "yes"):
        with pytest.raises(RuntimeError) as ei:
            _call_policy(monkeypatch, confirm=bad)
        assert "INSECURE_PUBLIC + TRUST_PROXY" in str(ei.value)


def test_single_insecure_public_still_allowed(monkeypatch):
    """单开 INSECURE_PUBLIC（不开 TRUST_PROXY）→ 旧行为保留，只 WARNING。"""
    _call_policy(monkeypatch, trust=False)  # 不抛


def test_loopback_unaffected(monkeypatch):
    """回环监听不受组合闸影响（闸只管公网）。"""
    _call_policy(monkeypatch, host="127.0.0.1")  # 不抛


def test_with_credential_unaffected(monkeypatch):
    """配了凭据 → 组合闸不触发（有门禁就不算裸奔）。"""
    _call_policy(monkeypatch, has_auth=True)  # 不抛
