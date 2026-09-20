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


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("AIDUMEM_ALLOW_INSECURE_PUBLIC", "AIDUMEI_TRUST_PROXY",
              "AIDUMEI_ALLOW_IMPLICIT_CALLER", "AIDUMEI_I_CONFIRM_PUBLIC_NO_AUTH",
              "AIDUMEM_API_TOKEN", "AIDUMEM_HOST"):
        monkeypatch.delenv(k, raising=False)
    yield


def _policy(monkeypatch, *, host="0.0.0.0", insecure="1", trust="1",
            confirm=None, token=None):
    """在受控环境里调 _enforce_public_binding_policy。"""
    monkeypatch.setenv("AIDUMEM_HOST", host)
    if insecure:
        monkeypatch.setenv("AIDUMEM_ALLOW_INSECURE_PUBLIC", insecure)
    if trust:
        monkeypatch.setenv("AIDUMEI_TRUST_PROXY", trust)
    if confirm is not None:
        monkeypatch.setenv("AIDUMEI_I_CONFIRM_PUBLIC_NO_AUTH", confirm)
    if token:
        monkeypatch.setenv("AIDUMEM_API_TOKEN", token)
    import importlib
    import api_server
    importlib.reload(api_server)
    return api_server._enforce_public_binding_policy


def test_combo_denied_without_confirmation(monkeypatch):
    """A7 核心：INSECURE_PUBLIC ∧ TRUST_PROXY ∧ 无凭据 → 拒绝启动。"""
    with pytest.raises(RuntimeError, match="INSECURE_PUBLIC \\+ TRUST_PROXY"):
        _policy(monkeypatch)


def test_combo_allowed_with_exact_host_confirmation(monkeypatch):
    """确认变量逐字等于监听地址 → 放行（知情部署）。"""
    _policy(monkeypatch, confirm="0.0.0.0")  # 不抛 = 通过


def test_confirmation_wrong_value_denied(monkeypatch):
    """确认变量值不对（1/true/别的地址）→ 仍拒绝。"""
    for bad in ("1", "true", "127.0.0.1", "yes"):
        with pytest.raises(RuntimeError):
            _policy(monkeypatch, confirm=bad)


def test_single_insecure_public_still_allowed(monkeypatch):
    """单开 INSECURE_PUBLIC（不开 TRUST_PROXY）→ 旧行为保留，只 WARNING。"""
    _policy(monkeypatch, trust=None)  # 不抛


def test_loopback_unaffected(monkeypatch):
    """回环监听不受组合闸影响（闸只管公网）。"""
    _policy(monkeypatch, host="127.0.0.1")  # 不抛


def test_with_credential_unaffected(monkeypatch):
    """配了凭据 → 组合闸不触发（有门禁就不算裸奔）。"""
    _policy(monkeypatch, token="secret-token-value")  # 不抛


def test_unsafe_combo_probe_reports_state(monkeypatch):
    """/health 的 unsafe_combo 探针：组合态如实列出。"""
    monkeypatch.setenv("AIDUMEM_ALLOW_INSECURE_PUBLIC", "1")
    monkeypatch.setenv("AIDUMEI_TRUST_PROXY", "1")
    from ducky.hot.health import _run_full_probe
    probes = _run_full_probe()
    combo = probes.get("unsafe_combo") or []
    assert "insecure_public" in combo and "trust_proxy" in combo
    assert probes.get("unsafe_combo_ok") is False


def test_unsafe_combo_probe_quiet_when_clean(monkeypatch):
    """干净部署：unsafe_combo 为空、_ok 为 True。"""
    monkeypatch.setenv("AIDUMEM_API_TOKEN", "secret-token-value")
    from ducky.hot.health import _run_full_probe
    probes = _run_full_probe()
    assert (probes.get("unsafe_combo") or []) == []
    assert probes.get("unsafe_combo_ok") is True
