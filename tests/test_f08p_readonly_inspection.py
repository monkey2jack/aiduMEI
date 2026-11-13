"""Inspection must surface missing evidence and manual/watchdog restarts."""
from copy import deepcopy

from scripts import inspect_runtime as probe


def _ok():
    return {"health": {"status": "ok", "details_available": True, "git_sha": "a" * 8},
            "code": {"head": "a" * 40, "dirty": False},
            "service": {"ActiveState": "active", "MainPID": "10", "NRestarts": "0",
                        "ExecMainStartTimestamp": "before", "ActiveEnterTimestamp": "before"},
            "errors_24h": {"traceback": 0, "error": 0, "http_5xx": 0},
            "memory": {"count": 1, "integrity": "ok"}}


def test_watchdog_restart_is_detected_even_when_nrestarts_stays_zero():
    before = _ok()
    after = deepcopy(before)
    after["service"].update(MainPID="20", ExecMainStartTimestamp="after", ActiveEnterTimestamp="after")
    assert "service_restarted" in probe.evaluate(after, before)["yellow"]


def test_missing_full_health_or_log_access_never_reports_green():
    report = _ok()
    report["health"]["details_available"] = False
    report["errors_24h"] = {"error_type": "RuntimeError"}
    verdict = probe.evaluate(report)
    assert verdict["verdict"] == "yellow"
    assert set(verdict["yellow"]) == {"full_health_unavailable", "errors_24h_unavailable"}


def test_runtime_errors_and_database_failure_are_red():
    report = _ok()
    report["memory"] = {"error_type": "DatabaseError"}
    report["errors_24h"]["http_5xx"] = 1
    assert probe.evaluate(report)["verdict"] == "red"


def test_head_drift_and_dirty_worktree_are_reported():
    report = _ok()
    report["code"]["dirty"] = True
    verdict = probe.evaluate(report, expected_sha="b" * 40)
    assert {"production_head_drift", "uncommitted_production_changes"} <= set(verdict["yellow"])


def test_inspection_credentials_fall_back_without_importing_the_application(tmp_path, monkeypatch):
    monkeypatch.delenv("AIDUMEM_API_TOKEN", raising=False)
    config = tmp_path / "runtime.env"
    config.write_text("export AIDUMEM_API_TOKEN='synthetic-inspection-token'\n")
    monkeypatch.setenv("AIDUMEM_ENV_FILE", str(config))
    missing = tmp_path / "missing.env"
    assert probe._token(missing) == "synthetic-inspection-token"
    monkeypatch.setenv("AIDUMEM_API_TOKEN", "environment-takes-priority")
    assert probe._token(missing) == "environment-takes-priority"
    assert not (tmp_path / "data").exists() and not (tmp_path / "logs").exists()
