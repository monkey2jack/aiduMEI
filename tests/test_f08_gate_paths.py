"""f0.8 ② the same enforced gate as /add on the other write paths: POST /update and the
background auto_memory loop.  (/session/distill stores nothing itself; MCP mem_add and
mcp_server.run_auto_memory go through /add.)  One subprocess: api_server reads env at import."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_PROBE = os.path.join(os.path.dirname(__file__), "_f08_gate_paths_probe.py")
P1 = "write_policy:P1:host_machine_text"


@pytest.fixture(scope="module")
def out():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AIDUMEM_", "AIDUMEI_"))}
    p = subprocess.run([sys.executable, _PROBE], capture_output=True, text=True, timeout=300, env=env)
    assert p.returncode == 0, p.stderr[-2000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


# ── one gate, not a copy ─────────────────────────────────────────────────
@pytest.mark.parametrize("rel", ["ducky/hot/crud.py", "ducky/extended/auto_memory.py"])
def test_path_reuses_the_add_gate(rel):
    src = (_ROOT / rel).read_text(encoding="utf-8")
    assert "enforce_messages(" in src
    for private in ("gate_decision(", "is_noise(", "blacklist_hit(", "tombstone_gated_write(", "ENFORCED_NOISE"):
        assert private not in src, f"{rel} re-implements the gate ({private})"


# ── POST /update ─────────────────────────────────────────────────────────
def test_update_noise_is_gated_and_old_memory_kept(out):
    assert out["upd_noise"] == [200, "gated", ["P1:host_machine_text"], False, 1, 0, [[P1]]]
    assert out["upd_image"] == [200, "gated", ["P1:host_machine_text"], 0]


def test_update_normal_text_is_stored(out):
    assert out["upd_normal"] == [200, None, False, 1, 0]


def test_update_short_ack_is_not_gated(out):
    assert out["upd_short"] == [200, False, 1]


def test_update_checks_ownership_before_gating(out):
    assert out["upd_missing_id"] == [404, 2]


def test_update_p8_drops_without_residue(out):
    assert out["upd_p8"] == [True, 200, "gated", ["P8"], {}, 1]


def test_update_env_switches_back_to_shadow_or_off(out):
    assert out["upd_p1_shadow"] == [200, False, 1]
    assert out["upd_p8_shadow"] == [200, False, 1]
    assert out["upd_off"] == [200, False, 1]


def test_update_tombstone_failure_stores_as_before(out):
    assert out["upd_tomb_fail"] == [200, False, 1, 0]


# ── background auto_memory loop ──────────────────────────────────────────
def test_auto_memory_gates_noise_and_keeps_the_conversation(out):
    gated, extracted, kept, silent, image, tombs = out["am_noise"]
    assert (gated, extracted, kept, silent, image) == (2, 1, 1, 0, 0)     # short ack "好" not gated
    assert tombs == [[P1], [P1]]


def test_auto_memory_all_noise_session_is_skipped(out):
    assert out["am_all_noise"] == [2, 0, 0]


def test_auto_memory_p8_rule_split_across_messages(out):
    assert out["am_p8_cross"] == [1, 0, {}, {}]


def test_auto_memory_p1_shadow(out):
    assert out["am_p1_shadow"] == [0, 1, 1]


def test_auto_memory_tombstone_failure_stores_as_before(out):
    assert out["am_tomb_fail"] == [0, 1, 1]
