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
