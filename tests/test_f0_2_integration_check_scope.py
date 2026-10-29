"""Integration acceptance must reject wrong-domain and HTTP-200 business errors."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts/agent_integration_check.py"


def _load():
    spec = importlib.util.spec_from_file_location("_integration_check_scope", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module.STEPS.clear()
    return module


@pytest.mark.parametrize("broken", ["", "core_wrong_scope", "delete_error",
                                    "add_accepted", "search_missing"])
def test_lifecycle_check_proves_query_scope_and_business_status(monkeypatch, capsys, broken):
    """f0.6+++ WP2: the lifecycle is /health → /add → /search → GET
    /api/core-memory → /delete (per hit); /gate, /add/raw, CoreMemory inject,
    /session/start|end and /delete_all are gone."""
    script = _load()
    monkeypatch.setattr(sys, "argv", ["agent_integration_check.py", "--tenant", "agent-integration-scope-probe"])
    called = []

    def fake_request(method, path, body=None, expect=(200,)):
        endpoint = urlsplit(path).path
        query = {k: values[0] for k, values in parse_qs(urlsplit(path).query).items()}
        called.append((endpoint, query, body))
        if endpoint == "/health":
            return 200, {"health_status": "ok", "probes": {"ingest_reads_24h": 0}}
        if endpoint == "/add":
            if broken == "add_accepted":
                return 200, {"status": "accepted", "durable": False}
            return 200, {"status": "ok"}
        if endpoint == "/search":
            if not isinstance(body, dict) or body.get("caller_user_id") != body.get("user_id"):
                return 403, {"detail": "bearer caller required"}
            if broken == "search_missing":
                return 200, {"status": "ok", "recall_verdict": "not_found", "results": []}
            return 200, {"status": "ok", "recall_verdict": "found",
                         "results": [{"id": "m-1", "memory": f"{body['query']} is the integration handshake."}]}
        if endpoint == "/api/core-memory":
            assert method == "GET"
            owner = "default" if broken == "core_wrong_scope" else body.get("user_id", "default")
            return 200, {"status": "ok", "user_id": owner,
                         "bank_id": body.get("bank_id", "default"), "blocks": {}}
        if endpoint == "/delete":
            if body.get("caller_user_id") != body.get("user_id"):
                return 403, {"detail": "bearer caller required"}
            if broken == "delete_error":
                return 500, {"status": "failed"}
            return 200, {"status": "committed"}
        raise AssertionError(endpoint)

    monkeypatch.setattr(script, "request", fake_request)
    exit_code = script.main()
    result = json.loads(capsys.readouterr().out)
    if broken:
        assert exit_code == 1 and result["status"] == "fail", (broken, result)
    else:
        assert exit_code == 0 and result["status"] == "pass", result
        add_body = next(body for ep, _, body in called if ep == "/add")
        assert add_body["metadata"]["force_sync"] is True
        search_body = next(body for ep, _, body in called if ep == "/search")
        assert search_body["caller_user_id"] == search_body["user_id"] == "agent-integration-scope-probe"
        core = next(body for ep, _, body in called if ep == "/api/core-memory")
        assert core["user_id"] == core["caller_user_id"] == "agent-integration-scope-probe"
        assert core["bank_id"] == "default"
        deleted = [body["memory_id"] for ep, _, body in called if ep == "/delete"]
        assert deleted == ["m-1"]
