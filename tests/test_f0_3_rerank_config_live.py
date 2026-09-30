"""Exercise automatic reranking through real configuration reads and saves."""
import json
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ducky.mem0_runtime as runtime
import ducky.routes_config as routes
from ducky import scoring


@pytest.fixture
def channel(tmp_path, monkeypatch):
    path = tmp_path / "models.json"
    monkeypatch.setattr(runtime, "MEM0_CONFIG", str(path))
    monkeypatch.setattr(runtime, "BASE_DIR", str(tmp_path))
    monkeypatch.setattr(runtime, "USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.setattr(runtime, "_llm_usage", {})
    monkeypatch.setattr(routes, "_CFG_PATH", str(path))
    monkeypatch.setenv("AIDUMEI_ENGINE_MODE", "auto")
    monkeypatch.delenv("AIDUMEI_RERANKER_API_KEY", raising=False)
    calls = []

    def provider(cfg, query, documents, top_n):
        calls.append(dict(cfg))
        return [{"index": 0, "relevance_score": 0.9}]

    monkeypatch.setitem(runtime.RERANK_PROVIDERS, "test", provider)
    from ducky.federation import routes as federation
    monkeypatch.setattr(federation, "_is_admin_caller", lambda caller: caller == "test-admin")
    app = FastAPI()
    routes.register_config_routes(app)
    client = TestClient(app)
    path.write_text(json.dumps({"rerank": {"provider": "test", "config": {
        "model": "rank-a", "api_key": "test-key", "openai_base_url": "https://example.invalid/v1",
    }}, "unrelated": {"preserve": True}}))
    return path, calls, client


def _rank():
    runtime.reset_rerank_telemetry()
    scored = [{"memory": "A synthetic candidate", "_hybrid_score": 0.2}]
    applied = scoring._apply_rerank("synthetic query", scored, 1)
    return applied, runtime.last_rerank_telemetry(), scored


def test_legacy_config_automatically_reranks(channel):
    _, calls, client = channel
    applied, telemetry, scored = _rank()
    assert applied and telemetry["status"] == "ok" and telemetry["applied"]
    assert scored[0]["_rerank_score"] == 0.9
    assert len(calls) == 1
    assert client.get("/config").json()["rerank"]["enabled"] is True


def test_config_save_disables_and_reenables_next_request(channel):
    path, calls, client = channel
    assert _rank()[0]
    assert client.put("/config/rerank?caller=test-admin", json={"enabled": False}).status_code == 200
    applied, telemetry, scored = _rank()
    assert not applied and telemetry["status"] == "disabled"
    assert scored[0]["_hybrid_score"] == 0.2 and len(calls) == 1
    assert runtime.rerank_config_status()["enabled"] is False
    assert client.put("/config/rerank?caller=test-admin", json={"enabled": True}).status_code == 200
    assert _rank()[0] and len(calls) == 2
    assert json.loads(path.read_text())["unrelated"] == {"preserve": True}


def test_save_model_address_and_key_takes_effect_without_reload(channel):
    _, calls, client = channel
    _rank()
    update = {"model": "rank-b", "openai_base_url": "https://example.invalid/other", "api_key": "new-test-key"}
    response = client.put("/config/rerank?caller=test-admin", json={"config": update})
    assert response.status_code == 200
    _rank()
    assert calls[-1]["model"] == "rank-b"
    assert calls[-1]["base_url"] == update["openai_base_url"]
    assert calls[-1]["api_key"] == "new-test-key"
    assert "new-test-key" not in response.text


def test_editing_disabled_channel_keeps_it_disabled(channel):
    _, calls, client = channel
    client.put("/config/rerank?caller=test-admin", json={"enabled": False})
    response = client.put("/config/rerank?caller=test-admin", json={"config": {"model": "rank-b", "api_key": ""}})
    assert response.status_code == 200
    assert not _rank()[0] and not calls
    assert runtime._load_rerank_config()["api_key"] == "test-key"


@pytest.mark.parametrize("enabled", ["false", "true", 0, 1, None])
def test_non_boolean_switch_is_rejected_without_writing(channel, enabled):
    path, _, client = channel
    before = path.read_bytes()
    response = client.put("/config/rerank?caller=test-admin", json={"enabled": enabled})
    assert response.status_code == 400
    assert path.read_bytes() == before


def test_file_replacement_and_removal_never_reuse_old_credentials(channel):
    path, calls, _ = channel
    assert _rank()[0]
    raw = json.loads(path.read_text())
    raw["rerank"]["enabled"] = False
    replacement = path.with_suffix(".next")
    replacement.write_text(json.dumps(raw))
    replacement.replace(path)
    assert _rank()[1]["status"] == "disabled"
    path.unlink()
    assert _rank()[1]["status"] == "not_configured"
    assert len(calls) == 1


def test_bad_config_reports_error_without_using_previous_key(channel):
    path, calls, _ = channel
    assert _rank()[0]
    path.write_text("{invalid json")
    applied, telemetry, _ = _rank()
    assert not applied and telemetry["status"] == "config_error"
    assert runtime.rerank_config_status()["configured"] is False
    assert len(calls) == 1


def test_environment_key_and_key_file_rotation(channel, monkeypatch):
    path, calls, _ = channel
    raw = json.loads(path.read_text())
    raw["rerank"]["config"]["api_key"] = ""
    path.write_text(json.dumps(raw))
    key_file = path.parent / ".sf_key"
    key_file.write_text("file-key-a")
    _rank()
    key_file.write_text("file-key-b")
    _rank()
    monkeypatch.setenv("AIDUMEI_RERANKER_API_KEY", "env-test-key")
    _rank()
    assert [c["api_key"] for c in calls] == ["file-key-a", "file-key-b", "env-test-key"]


def test_alias_config_remains_automatic_and_editable(channel):
    path, calls, client = channel
    raw = json.loads(path.read_text())
    raw["reranker"] = raw.pop("rerank")
    path.write_text(json.dumps(raw))
    assert _rank()[0]
    response = client.put("/config/rerank?caller=test-admin", json={"enabled": False})
    assert response.status_code == 200
    assert not _rank()[0] and len(calls) == 1
    assert runtime._load_rerank_config()["model"] == "rank-a"


def test_local_mode_blocks_even_an_enabled_channel(channel, monkeypatch):
    _, calls, _ = channel
    monkeypatch.setenv("AIDUMEI_ENGINE_MODE", "local")
    assert _rank()[1]["status"] == "blocked_by_engine_mode"
    assert calls == []


def test_save_does_not_cancel_inflight_but_blocks_next_request(channel, monkeypatch):
    _, calls, client = channel
    started, release = threading.Event(), threading.Event()
    result = []

    def provider(cfg, query, documents, top_n):
        calls.append(dict(cfg))
        started.set()
        assert release.wait(5)
        return [{"index": 0, "relevance_score": 0.9}]

    monkeypatch.setitem(runtime.RERANK_PROVIDERS, "test", provider)
    worker = threading.Thread(target=lambda: result.append(_rank()[0]))
    worker.start()
    try:
        assert started.wait(5)
        assert client.put("/config/rerank?caller=test-admin", json={"enabled": False}).status_code == 200
        assert _rank()[1]["status"] == "disabled"
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive() and result == [True] and len(calls) == 1
