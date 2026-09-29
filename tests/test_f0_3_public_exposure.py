"""f0.3 public-exposure gate: a credential-less public bind needs a confirmation.

Before f0.3, AIDUMEM_ALLOW_INSECURE_PUBLIC=1 alone on a non-loopback bind only
logged a warning and started, i.e. anyone who could reach the host could read
and write every memory.  Only the INSECURE_PUBLIC + TRUST_PROXY combination
required AIDUMEI_I_CONFIRM_PUBLIC_NO_AUTH to equal the listen address.  Now
both shapes require that confirmation; the combination keeps its own message.

The tests call the real api_server._enforce_public_binding_policy(); host
detection runs for real (env and argv), only the credential predicate is
pinned so a password hash left in the sandbox data dir cannot flip results.
"""

from __future__ import annotations

import sys

import pytest

_ENV = ("AIDUMEM_HOST", "AIDUMEM_ALLOW_INSECURE_PUBLIC", "AIDUMEI_TRUST_PROXY",
        "AIDUMEI_I_CONFIRM_PUBLIC_NO_AUTH", "AIDUMEM_API_TOKEN")


@pytest.fixture
def gate(monkeypatch):
    import api_server

    for key in _ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(sys, "argv", ["aidumem"])
    monkeypatch.setattr(api_server, "_auth_enabled", lambda: False)

    def run(*, host: str | None = "0.0.0.0", insecure: bool = True, trust: bool = False,
            confirm: str | None = None, argv: list[str] | None = None) -> None:
        if host is not None:
            monkeypatch.setenv("AIDUMEM_HOST", host)
        if insecure:
            monkeypatch.setenv("AIDUMEM_ALLOW_INSECURE_PUBLIC", "1")
        if trust:
            monkeypatch.setenv("AIDUMEI_TRUST_PROXY", "1")
        if confirm is not None:
            monkeypatch.setenv("AIDUMEI_I_CONFIRM_PUBLIC_NO_AUTH", confirm)
        if argv is not None:
            monkeypatch.setattr(sys, "argv", argv)
        api_server._enforce_public_binding_policy()

    return run


def test_single_insecure_public_is_refused_without_confirmation(gate) -> None:
    with pytest.raises(RuntimeError) as refused:
        gate()
    assert "INSECURE_PUBLIC without any credential on '0.0.0.0'" in str(refused.value)
    assert "AIDUMEI_I_CONFIRM_PUBLIC_NO_AUTH='0.0.0.0'" in str(refused.value)


@pytest.mark.parametrize("confirm", ["1", "true", "yes", "127.0.0.1", "::", "10.0.0.5", ""])
def test_single_insecure_public_rejects_values_other_than_the_listen_host(gate, confirm) -> None:
    with pytest.raises(RuntimeError):
        gate(confirm=confirm)


@pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.5", "::"])
def test_single_insecure_public_starts_when_confirmation_equals_host(gate, host) -> None:
    gate(host=host, confirm=host)  # does not raise


def test_host_given_to_uvicorn_on_argv_is_the_one_that_must_be_confirmed(gate) -> None:
    argv = ["uvicorn", "api_server:app", "--host", "0.0.0.0"]
    with pytest.raises(RuntimeError):
        gate(host=None, argv=argv)
    with pytest.raises(RuntimeError):
        gate(host=None, argv=argv, confirm="127.0.0.1")
    gate(host=None, argv=argv, confirm="0.0.0.0")


def test_combination_keeps_its_stricter_message_and_same_confirmation(gate) -> None:
    with pytest.raises(RuntimeError) as refused:
        gate(trust=True)
    assert "INSECURE_PUBLIC + TRUST_PROXY" in str(refused.value)
    gate(trust=True, confirm="0.0.0.0")


def test_public_bind_without_escape_hatch_is_still_refused(gate) -> None:
    with pytest.raises(RuntimeError) as refused:
        gate(insecure=False, confirm="0.0.0.0")  # confirmation alone opens nothing
    assert "without any credential is prohibited" in str(refused.value)


def test_loopback_and_credentialed_binds_need_no_confirmation(gate, monkeypatch) -> None:
    import api_server

    gate(host="127.0.0.1")
    monkeypatch.setattr(api_server, "_auth_enabled", lambda: True)
    gate(host="0.0.0.0")


def _previous_gate(host: str, *, insecure: bool, trust: bool, confirm: str, auth: bool) -> None:
    """Control flow of the faaa9af gate, for the negative control below."""
    if host in ("127.0.0.1", "localhost", "::1") or auth:
        return
    if insecure:
        if trust and confirm != host:
            raise RuntimeError("combination requires confirmation")
        return  # single escape hatch: a warning, then start
    raise RuntimeError("public bind without credential")


def test_negative_control_previous_gate_started_on_a_single_escape_hatch(gate) -> None:
    _previous_gate("0.0.0.0", insecure=True, trust=False, confirm="", auth=False)  # started
    with pytest.raises(RuntimeError):
        gate()  # the same inputs are refused now
