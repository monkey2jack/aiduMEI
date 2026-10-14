"""Execute the cron monitor with synthetic HTTP endpoints and deployed scope."""
import json
from pathlib import Path
import runpy

import pytest


def test_monitor_passes_owner_caller_and_encoded_bank(monkeypatch, tmp_path):
    import ducky.utils as utils
    import requests

    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "embedder": {"config": {"model": "fixture", "openai_base_url": "http://fixture/v1", "api_key": "fixture"}},
        "llm": {"config": {"model": "fixture", "openai_base_url": "http://fixture/v1", "api_key": "fixture"}},
    }))
    monkeypatch.setenv("AIDUMEM_CONFIG_FILE", str(config))
    monkeypatch.setenv("HERMES_CONFIG", str(tmp_path / "absent.yaml"))
    monkeypatch.setenv("AIDUMEI_INTEGRATION_MODE", "api")
    monkeypatch.setattr(utils, "DEFAULT_USER_ID", "owner & one")
    monkeypatch.setattr(utils, "env_or_env_file", lambda name, default: "bank & two" if name == "AIDUMEI_BANK_ID" else default)
    monkeypatch.setattr(utils, "api_auth_headers", lambda: {"Authorization": "Bearer fixture"})
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {"health_status": "ok", "degraded": [], "total": 3}

    def request(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr(requests, "get", request)
    monkeypatch.setattr(requests, "post", request)
    with pytest.raises(SystemExit) as result:
        runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/health_check.py"))
    assert result.value.code == 0
    stats = next(kwargs for url, kwargs in calls if url.endswith("/stats"))
    search = next(kwargs for url, kwargs in calls if url.endswith("/search"))
    expected = {"user_id": "owner & one", "caller_user_id": "owner & one", "bank_id": "bank & two"}
    assert stats["params"] == expected
    assert {k: search["json"][k] for k in expected} == expected


# ------------------------------------------------ f0.4+ WP3: transition alert

def _run_monitor(monkeypatch, tmp_path, *, status="ok", degraded=()):
    import ducky.utils as utils
    import requests

    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "embedder": {"config": {"model": "fixture", "openai_base_url": "http://fixture/v1", "api_key": "fixture"}},
        "llm": {"config": {"model": "fixture", "openai_base_url": "http://fixture/v1", "api_key": "fixture"}},
    }))
    monkeypatch.setenv("AIDUMEM_CONFIG_FILE", str(config))
    monkeypatch.setenv("HERMES_CONFIG", str(tmp_path / "absent.yaml"))
    monkeypatch.setenv("AIDUMEI_INTEGRATION_MODE", "api")
    monkeypatch.setattr(utils, "api_auth_headers", lambda: {})

    class Response:
        status_code = 200

        def json(self):
            return {"health_status": status, "degraded": list(degraded), "total": 3}

    monkeypatch.setattr(requests, "get", lambda url, **kw: Response())
    monkeypatch.setattr(requests, "post", lambda url, **kw: Response())
    with pytest.raises(SystemExit) as result:
        runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/health_check.py"))
    return result.value.code


def _alert_env(monkeypatch, tmp_path):
    log = tmp_path / "alerts.log"
    monkeypatch.setenv("AIDUMEI_ALERT_COMMAND",
                       f'printf "%s|%s|%s\\n" "$AIDUMEI_ALERT_PREVIOUS_STATUS" '
                       f'"$AIDUMEI_ALERT_STATUS" "$AIDUMEI_ALERT_SUMMARY" >> "{log}"')
    monkeypatch.setenv("AIDUMEI_ALERT_STATE_FILE", str(tmp_path / "state.json"))
    return log


def _lines(log):
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_alert_fires_once_per_transition_and_on_recovery(monkeypatch, tmp_path):
    """ok → degraded 报一次、持续降级不刷屏、恢复也报一次。"""
    log = _alert_env(monkeypatch, tmp_path)
    assert _run_monitor(monkeypatch, tmp_path) == 0
    first = _lines(log)
    assert len(first) == 1 and first[0].startswith("unknown|ok|"), first   # 首跑立基线

    assert _run_monitor(monkeypatch, tmp_path) == 0
    assert len(_lines(log)) == 1, "状态没变却又报了一次"

    assert _run_monitor(monkeypatch, tmp_path, status="degraded",
                        degraded=["mutation_journal"]) == 1
    assert _run_monitor(monkeypatch, tmp_path, status="degraded",
                        degraded=["mutation_journal"]) == 1
    lines = _lines(log)
    assert len(lines) == 2, f"持续降级刷屏了：{lines}"
    assert lines[1].startswith("ok|degraded|") and "aidumem_api" in lines[1], lines[1]

    assert _run_monitor(monkeypatch, tmp_path) == 0
    lines = _lines(log)
    assert len(lines) == 3 and lines[2].startswith("degraded|ok|"), lines


def test_persisting_degradation_repeats_after_suppression_window(monkeypatch, tmp_path):
    log = _alert_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AIDUMEI_ALERT_SUPPRESS_SEC", "0")
    for _ in range(2):
        _run_monitor(monkeypatch, tmp_path, status="degraded", degraded=["x"])
    assert len(_lines(log)) == 2, "抑制窗口过了还不复报，长期降级会被当成一次性事件"


def test_no_alert_command_means_no_side_effects(monkeypatch, tmp_path):
    """Negative control: without the command nothing is written or run."""
    monkeypatch.delenv("AIDUMEI_ALERT_COMMAND", raising=False)
    monkeypatch.setenv("AIDUMEI_ALERT_STATE_FILE", str(tmp_path / "state.json"))
    _run_monitor(monkeypatch, tmp_path, status="degraded", degraded=["x"])
    assert not (tmp_path / "state.json").exists()


def test_broken_alert_command_does_not_change_the_verdict(monkeypatch, tmp_path):
    monkeypatch.setenv("AIDUMEI_ALERT_COMMAND", "exit 7")
    monkeypatch.setenv("AIDUMEI_ALERT_STATE_FILE", str(tmp_path / "state.json"))
    assert _run_monitor(monkeypatch, tmp_path) == 0
    assert _run_monitor(monkeypatch, tmp_path, status="degraded", degraded=["x"]) == 1
