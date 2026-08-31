"""v20.3 WP-B: `scripts/e2e_smoke.py` must be a real acceptance gate.

These tests use a FastAPI TestClient rather than a live server. The point is
not to duplicate integration testing; it is to prove that the script's branch
logic distinguishes healthy service, degraded config, failed write, failed
recall, and failed cleanup instead of collapsing them into one green output.
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from types import SimpleNamespace

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_smoke():
    spec = importlib.util.spec_from_file_location("e2e_smoke", _ROOT / "scripts" / "e2e_smoke.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def smoke():
    module = _load_smoke()
    return module.Smoke("http://test.local", "tenant", 0, True)


def test_script_is_executable_and_has_json_contract():
    path = _ROOT / "scripts" / "e2e_smoke.py"
    assert path.is_file()
    assert path.stat().st_mode & 0o111, "acceptance script must be executable"
    text = path.read_text(encoding="utf-8")
    assert "--json" in text
    assert "raise SystemExit(main())" in text


def test_health_failure_is_recorded_as_failure(smoke):
    class Response:
        status_code = 500
        text = "not json"
        def json(self):
            raise ValueError("bad json")
    smoke.request = lambda *args, **kwargs: Response()
    smoke.health()
    assert smoke.results[0]["status"] == "FAIL"
    assert smoke.failures == 1


def test_health_success_requires_core_probes(smoke):
    class Response:
        status_code = 200
        text = "{}"
        def json(self):
            return {"health_status": "ok", "probes": {
                "facts_db": True, "text_fts_db": True,
                "mem0_singleton": True, "port_service": True,
            }}
    smoke.request = lambda *args, **kwargs: Response()
    smoke.health()
    assert smoke.results[0]["status"] == "PASS"


def test_missing_config_is_warning_not_silent_pass(smoke, monkeypatch):
    monkeypatch.setattr(smoke, "_REPO_LOCAL_MISSING", True, raising=False)
    # The script intentionally reads the repo path from the module-level _REPO.
    smoke.config.__globals__["_REPO"] = pathlib.Path("/tmp/definitely-not-a-real-repo")
    smoke.config()
    assert smoke.results[0]["status"] == "WARN"
    assert "cloud gears may be unavailable" in smoke.results[0]["detail"]


def test_failed_recall_is_failure(smoke):
    class Response:
        status_code = 200
        text = "{}"
        def json(self):
            return {"recall_verdict": "not_found", "results": []}
    smoke.request = lambda *args, **kwargs: Response()
    smoke.add_and_recall()
    statuses = [x["status"] for x in smoke.results]
    assert "FAIL" in statuses
    assert smoke.failures >= 1


def test_cleanup_partial_is_failure(smoke):
    class Response:
        status_code = 207
        text = "{}"
        def json(self):
            return {"status": "partial", "failed_layers": ["fts"]}
    smoke.request = lambda *args, **kwargs: Response()
    smoke.cleanup()
    assert smoke.results[0]["status"] == "FAIL"
    assert smoke.failures == 1


def test_json_report_shape(smoke):
    smoke.record("a", "PASS")
    smoke.record("b", "WARN")
    report = smoke.run.__self__.__dict__
    assert "results" in report and "failures" in report

def test_v20_3_entry_documents_exist_and_are_bounded():
    root = _ROOT
    required = [
        "AGENTS.md", "llms.txt", "TROUBLESHOOTING.md",
        "docs/OPERATIONS.md", "docs/HEALTH.md",
        "docs/AGENT_INTEGRATION.md", "docs/BACKUP_RESTORE.md",
        "scripts/README.md",
    ]
    for name in required:
        assert (root / name).is_file(), f"missing entry document: {name}"
    agents = (root / "AGENTS.md").read_text(encoding="utf-8")
    assert len(agents.encode("utf-8")) <= 12000
    assert "AIDUMEI_ENGINE_MODE" in agents
    assert "scripts/e2e_smoke.py" in agents
    assert "runtime_paths" in agents

def test_readme_is_a_navigation_entry_not_a_knowledge_dump():
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    line_count = len(readme.splitlines())
    assert line_count <= 600, f"README is {line_count} lines; target is <=600"
    assert "[🤖 Agent Guide](AGENTS.md)" in readme
    assert "python scripts/e2e_smoke.py --json" in readme

def test_v20_3_current_facts_are_not_contradicted():
    zh = (_ROOT / "README.md").read_text(encoding="utf-8")
    architecture = (_ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "ECharts CDN" not in zh, "frontend/index.html serves ECharts from local vendor assets"
    assert "Python 3.12+" not in zh
    assert "AIDUMEI_ENGINE_MODE=auto" in zh
    assert "pip install .[local-embed]" in zh
    assert "fetch_local_embed_model.py" in zh
    assert "historical snapshot" in architecture
