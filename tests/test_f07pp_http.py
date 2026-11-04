"""f0.7++ HTTP end-to-end (second outside audit, 2026-11-03/04).  One subprocess: api_server reads env at import."""
import json
import os
import subprocess
import sys

import pytest

_PROBE = os.path.join(os.path.dirname(__file__), "_f07pp_probe.py")


@pytest.fixture(scope="module")
def out():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AIDUMEM_", "AIDUMEI_"))}
    p = subprocess.run([sys.executable, _PROBE], capture_output=True, text=True, timeout=300, env=env)
    assert p.returncode == 0, p.stderr[-3000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


def test_agent_key_enforcement_puts_facts_in_tenant_mode(out):
    # Before f0.7.5 a key reading its own user_id saw the owner's (and every "unclaimed") fact.
    assert out["bob_facts"] == [200, False, False, True]
    assert out["alice_facts"] == [200, False, True, False]
    # owner identity keeps its own + unclaimed rows, but is no longer the whole bank
    assert out["owner_facts"] == [200, True, False, False]


def test_unbound_main_token_keeps_single_owner_semantics(out):
    assert out["main_facts"] == [200, True, True]


def test_opinions_set_checks_the_facts_real_owner(out):
    assert out["alice_own_opinion"] == 200
    assert out["bob_cross_opinion"] == [404, 404]
    assert out["bob_opinion_rows"] == 0
    assert out["bob_reads_alice_opinions"] is False


def test_evolve_feedback_text_is_redacted(out):
    assert out["feedback_status"] == 200
    assert out["feedback_residue"] == [{}, {}]


def test_list_content_and_extra_keys_cannot_bypass_the_write_gate(out):
    assert out["list_content_status"] == 200
    assert out["list_content_residue"] == {}
    assert out["list_name_residue"] == {}


def test_zero_width_env_style_and_metadata_secrets_never_stored(out):
    assert out["zwsp_status"] == 200 and out["zwsp_residue"] == {}
    assert out["env_residue"] == {}
    assert out["metadata_residue"] == {}


def test_non_ascii_credentials_are_401_not_500(out):
    assert out["nonascii_bearer"] == 401
    assert out["nonascii_alt"] == 401
