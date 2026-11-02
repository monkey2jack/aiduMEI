"""f0.7+ HTTP end-to-end: P7 hard block, shadow verdicts, provenance on recall, forget (P8),
ext-audit N4 (cross-tenant delete) and S3 (per-agent keys).  One subprocess: api_server reads env at import."""
import json
import os
import subprocess
import sys

import pytest

_PROBE = os.path.join(os.path.dirname(__file__), "_f07plus_probe.py")


@pytest.fixture(scope="module")
def out():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AIDUMEM_", "AIDUMEI_"))}
    p = subprocess.run([sys.executable, _PROBE], capture_output=True, text=True, timeout=300, env=env)
    assert p.returncode == 0, p.stderr[-2000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


def test_p7_secret_never_reaches_any_layer(out):
    assert out["secret_add"] == 200
    assert out["secret_residue"] == {}
    assert out["secret_redacted_recall"] is True


def test_p7_raw_mail_copy_refused_and_logged_without_text(out):
    assert out["mail_status"] == 422
    assert out["mail_residue"] == {}
    assert out["mail_logged"] == [["raw_email_copy", None]]


def test_shadow_verdicts_and_inferred_recall(out):
    assert ["reject", "direct", "daily"] in out["verdicts"]
    assert ["keep", "inferred", "profile"] in out["verdicts"]
    assert out["inferred_flag"] == [True, "inferred", "session_distill:3 turns"]


def test_delete_cascades_write_policy_rows(out):
    assert out["delete_status"] == "committed" and out["verdict_rows_after_delete"] == 0


def test_forget_lists_first_then_deletes_for_good(out):
    assert out["forget_listing"] == "candidates" and out["still_there_after_listing"] is True
    assert out["forget_status"] == "ok" and out["forget_search"] is False
    assert out["forget_residue"] == {}
    assert out["rule_hit"] == 1 and out["rule_delete"] == 1
    assert out["bob_cannot_list_alice_rules"] == 403


def test_n4_foreign_id_deletes_nothing(out):
    # Before f0.7+: status committed and erin's salience row deleted (1 -> 0).
    assert out["n4"] == ["not_found", 1, 1] and out["n4_erin_still"] is True


def test_s3_agent_keys(out):
    assert out["keys_file_mode"] == "0o600" and out["keys_file_has_plain"] is False
    assert out["no_token"] == 401 and out["wrong_token"] == 401
    assert out["agent_own"] == 200 and out["agent_own_enforced"] == 200
    assert out["agent_other_shadow"] == 200 and out["agent_other_enforced"] == 403
    assert out["main_unbound_any"] == 200 and out["main_bound_other_shadow"] == 200
    assert out["key_events"] == [["helper", "erin", "mismatch_shadow", 1], ["helper", "erin", "refused", 1],
                                 ["owner", "erin", "mismatch_shadow", 1]]


def test_f073_key_binding_holds_on_every_route(out):
    # Before f0.7.3 only handlers calling the Pantheon policy point checked the key:
    # /stats, /recent, /tombstones, /metrics … answered a bound key for any tenant.
    assert set(out["route_own"].values()) == {200}, out["route_own"]
    assert {k: v for k, v in out["route_other"].items() if v != 403} == {}, out["route_other"]
    assert out["main_bound_route"] == [403, 200]


def test_f073_shadow_mode_still_only_counts(out):
    assert out["route_other_shadow"] == 200
    seen = {tuple(r[:3]) for r in out["route_events"]}
    assert {("*", "read", "mismatch_shadow"), ("*", "admin:GET /metrics", "refused"),
            ("erin", "delete", "refused")} <= seen, out["route_events"]


def test_f073_delete_all_keeps_never_store_rules(out):
    assert out["rules_after_delete_all"] is True


def test_f07plus_regression_categories():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AIDUMEM_", "AIDUMEI_"))}
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = subprocess.run([sys.executable, "-m", "benchmarks.regression_f07plus", "--baseline",
                        os.path.join(root, "benchmarks", "regression_f07plus_baseline.json")],
                       capture_output=True, text=True, timeout=600, env=env, cwd=root)
    assert p.returncode == 0, (p.stdout[-1500:], p.stderr[-1500:])
