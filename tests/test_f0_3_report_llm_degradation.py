"""f0.3 A5 -- report.py: 24h LLM degradation metric + cron count without a magic 8.

* LLM outcomes (ok / failed / gate_timeout) are recorded per hour by both LLM
  channels (ducky.llm_client.record_llm_outcome); report.py turns the last 24h
  into a top-level `llm_degradation_24h` block with a warning threshold.
* report.py's "how many cron tasks must be installed" fallback used the literal
  8 although the manifest has 9 tasks since v21.2.0 -- a drift false-green.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import time
from pathlib import Path

import pytest

import ducky.llm_client as llm_client
from ducky import utils

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_790_000_000.0
ANCHORED = {"describe": "f0.3", "exact_tag": "f0.3", "dirty": False, "anchored": True}
HEALTHY_MAINT = {"crontab_task_count": 9, "crontab_expected_count": 9, "crontab_installed_count": 9,
                 "crontab_verified": True, "crontab_tasks": {}, "latest_backup": {"verified": True}}


def _report_module():
    spec = importlib.util.spec_from_file_location("_f03_report", ROOT / "scripts" / "report.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def ledger_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(utils, "DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AIDUMEI_LLM_DEGRADED_WARN_RATIO", raising=False)
    return tmp_path


def _seed(ok: int, failed: int, gate_timeout: int = 0, *, now: float = NOW, origin: str = "mem0"):
    for kind, n in (("ok", ok), ("failed", failed), ("gate_timeout", gate_timeout)):
        for _ in range(n):
            llm_client.record_llm_outcome(kind, origin=origin, now=now)


# ---------------------------------------------------------------- the outcome ledger
def test_ledger_counts_by_outcome_origin_and_window():
    _seed(3, 0)
    llm_client.record_llm_outcome("failed", origin="call_llm", now=NOW - 3600)
    llm_client.record_llm_outcome("gate_timeout", origin="mem0", now=NOW - 2 * 3600)
    llm_client.record_llm_outcome("failed", origin="mem0", now=NOW - 30 * 3600)  # outside 24h
    w = llm_client.llm_outcome_window(24, now=NOW)
    assert (w["ok"], w["failed"], w["gate_timeout"], w["total"]) == (3, 1, 1, 5)
    assert w["by_origin"]["mem0"] == {"ok": 3, "failed": 0, "gate_timeout": 1}
    assert w["by_origin"]["call_llm"]["failed"] == 1
    # negative control: a wider window does see the 30h-old failure, so the 24h cut is real
    assert llm_client.llm_outcome_window(48, now=NOW)["failed"] == 2


def test_ledger_prunes_buckets_older_than_eight_days(ledger_dir):
    llm_client.record_llm_outcome("failed", origin="mem0", now=NOW - 10 * 86400)
    llm_client.record_llm_outcome("ok", origin="mem0", now=NOW)
    hours = json.loads((ledger_dir / "llm_outcomes.json").read_text())["hours"]
    assert list(hours) == [time.strftime("%Y-%m-%dT%H", time.gmtime(NOW))]


def test_ledger_survives_a_corrupt_file(ledger_dir):
    (ledger_dir / "llm_outcomes.json").write_text("{not json", encoding="utf-8")
    assert llm_client.llm_outcome_window(24, now=NOW)["total"] == 0
    llm_client.record_llm_outcome("ok", origin="mem0", now=NOW)
    assert llm_client.llm_outcome_window(24, now=NOW)["ok"] == 1


def test_call_llm_records_what_really_happened(monkeypatch):
    monkeypatch.setattr(llm_client, "_config_cache",
                        {"model": "m", "base_url": "http://llm.invalid/v1", "api_key": "k"})

    class _Resp:
        def __init__(self, code, text):
            self.status_code, self.text = code, text

    replies = iter([_Resp(500, "upstream busy"),
                    _Resp(200, json.dumps({"choices": [{"message": {"content": "fine"}}]}))])
    monkeypatch.setattr(llm_client.requests, "post", lambda *a, **k: next(replies))
    assert llm_client.call_llm("a") is None
    assert llm_client.call_llm("b") == "fine"
    w = llm_client.llm_outcome_window(1)
    assert (w["failed"], w["ok"]) == (1, 1) and w["by_origin"]["call_llm"]["failed"] == 1
    # an unconfigured LLM is a configuration choice, not a degradation: nothing is recorded
    monkeypatch.setattr(llm_client, "_config_cache", {"model": "", "base_url": "", "api_key": ""})
    assert llm_client.call_llm("c") is None
    assert llm_client.llm_outcome_window(1)["total"] == 2


# ---------------------------------------------------------------- the report block
def test_block_warns_on_high_ratio_with_enough_samples():
    rep = _report_module()
    _seed(22, 2, 1)
    block = rep._llm_degradation_block(now=NOW)
    assert block["calls"] == 25 and block["degraded"] == 3 and block["ratio"] == 0.12
    assert block["warning"] is True and block["warn_ratio"] == 0.05


def test_block_does_not_warn_on_a_tiny_sample():
    rep = _report_module()
    _seed(2, 3)   # 60% failed, but only 5 calls
    block = rep._llm_degradation_block(now=NOW)
    assert block["ratio"] == 0.6 and block["warning"] is False and block["note"]


def test_block_does_not_warn_below_threshold():
    rep = _report_module()
    _seed(30, 1)
    assert rep._llm_degradation_block(now=NOW)["warning"] is False


def test_threshold_env_is_honoured_and_garbage_falls_back(monkeypatch):
    rep = _report_module()
    _seed(22, 3)
    monkeypatch.setenv("AIDUMEI_LLM_DEGRADED_WARN_RATIO", "0.5")
    assert rep._llm_degradation_block(now=NOW)["warning"] is False
    monkeypatch.setenv("AIDUMEI_LLM_DEGRADED_WARN_RATIO", "nan")
    block = rep._llm_degradation_block(now=NOW)
    assert block["warn_ratio"] == 0.05 and block["warning"] is True


def _full(rep, monkeypatch, health=None):
    real_block = rep._llm_degradation_block          # the seeded ledger lives at NOW
    monkeypatch.setattr(rep, "_llm_degradation_block", lambda: real_block(now=NOW))
    monkeypatch.setattr(rep, "_maintenance_block", lambda: dict(HEALTHY_MAINT))
    monkeypatch.setattr(rep, "_git_describe", lambda: dict(ANCHORED))
    monkeypatch.setattr(rep, "_git_commit", lambda: "0" * 40)
    base = {"health_status": "ok", "status": "ok", "degraded": [], "warming_up": [],
            "warnings": [], "probes": {}}
    return rep._full_report(health or base)


def test_full_report_surfaces_degradation_top_level_and_exits_2(monkeypatch):
    rep = _report_module()
    _seed(20, 5)
    health = {"health_status": "ok", "status": "ok", "degraded": [], "warming_up": [],
              "warnings": [], "probes": {}}
    report = _full(rep, monkeypatch, health)
    assert report["llm_degradation_24h"]["warning"] is True
    assert report["anomalies"]["warnings"], "the degradation must surface as a warning"
    assert health["warnings"] == [], "the /health payload itself must not be mutated"
    assert any("LLM DEGRADED" in a for a in report["next_actions"])
    assert rep._exit_code(report) == 2


def test_full_report_healthy_llm_exits_0(monkeypatch):
    rep = _report_module()
    _seed(40, 0)
    report = _full(rep, monkeypatch)
    assert report["llm_degradation_24h"]["warning"] is False
    assert not any("LLM DEGRADED" in a for a in report["next_actions"])
    assert rep._exit_code(report) == 0, report["next_actions"]


# ---------------------------------------------------------------- cron count: no magic 8
def _report_dict(maintenance):
    return {"health_status": "ok", "degraded": [], "warming_up": [], "anomalies": {},
            "git_describe": dict(ANCHORED), "maintenance": maintenance}


def test_manifest_count_matches_the_installer_list():
    rep = _report_module()
    listed = json.loads(subprocess.run(["bash", str(ROOT / "scripts" / "update_crontab.sh"), "--list"],
                                       capture_output=True, text=True, check=True, timeout=60).stdout)
    assert rep._manifest_task_count() == len(listed["tasks"]) == 9


def test_required_count_falls_back_to_the_manifest_not_to_8():
    rep = _report_module()
    maint = {"crontab_task_count": None, "crontab_installed_count": 8,
             "crontab_verified": None, "latest_backup": {"verified": True}}
    assert rep._required_task_count(maint) == 9
    assert rep._exit_code(_report_dict(maint)) == 2
    # negative control: the pre-f0.3 expression would have accepted 8 installed tasks
    intended = maint["crontab_task_count"]
    old_required = intended if isinstance(intended, int) and intended > 0 else 8
    assert not maint["crontab_installed_count"] < old_required


def test_required_count_prefers_list_then_verifier_expected():
    rep = _report_module()
    assert rep._required_task_count({"crontab_task_count": 9, "crontab_expected_count": 7}) == 9
    assert rep._required_task_count({"crontab_task_count": None, "crontab_expected_count": 7}) == 7
    assert rep._required_task_count({"crontab_task_count": True}) == 9, "bool is not a count"


def test_unknowable_required_count_is_a_warning(monkeypatch):
    rep = _report_module()
    monkeypatch.setattr(rep, "_manifest_task_count", lambda: None)
    maint = {"crontab_task_count": None, "crontab_installed_count": 9,
             "crontab_verified": None, "latest_backup": {"verified": True}}
    assert rep._required_task_count(maint) is None
    assert rep._exit_code(_report_dict(maint)) == 2
    assert any("update_crontab" in a for a in rep._safe_next_actions({"health_status": "ok"}, maint))
