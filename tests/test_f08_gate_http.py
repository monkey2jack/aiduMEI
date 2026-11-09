"""f0.8 ② HTTP end-to-end: enforced P1 / P8 / thinking gate on /add, tombstone restore, env back to shadow.
One subprocess: api_server reads env at import."""
import json
import os
import subprocess
import sys

import pytest

_PROBE = os.path.join(os.path.dirname(__file__), "_f08_gate_probe.py")


@pytest.fixture(scope="module")
def out():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AIDUMEM_", "AIDUMEI_"))}
    p = subprocess.run([sys.executable, _PROBE], capture_output=True, text=True, timeout=300, env=env)
    assert p.returncode == 0, p.stderr[-2000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


def test_machine_noise_goes_to_tombstone_not_live(out):
    assert out["img_status"] == [200, "gated", ["P1:host_machine_text"]]
    assert out["img_live"] == 0
    assert out["img_tomb"] == ["write_policy:P1:host_machine_text", "write_policy", 7, True]
    assert out["img_listed"] is True
    assert out["img_verdict"][0][:2] == ["reject", "enforce"]


def test_gated_noise_is_restorable(out):
    assert out["img_restore"] == "ok" and out["img_live_after_restore"] == 1


def test_mixed_turn_keeps_the_real_sentence(out):
    assert out["mixed"] == [200, 1, 1, 0]
    assert out["continuation"] == [200, "gated", 0]


def test_short_confirmations_are_stored_in_daily(out):
    assert out["short"] == [["daily", "daily", "shadow"], ["daily", "daily", "shadow"]]
    assert out["short_live"] == [1, 1]


def test_thinking_summary_is_gated(out):
    assert out["thinking"] == [200, "gated", ["THINKING"], 0]


def test_p8_forget_rule_drops_without_any_residue(out):
    assert out["p8"] == [True, 200, "gated", ["P8"], {}]


def test_env_turns_enforcement_back_to_shadow(out):
    assert out["p1_shadow"][0] == 200 and out["p1_shadow"][2] is False and out["p1_shadow"][3] == 1
    assert out["p8_shadow"] == [200, False, 1]
