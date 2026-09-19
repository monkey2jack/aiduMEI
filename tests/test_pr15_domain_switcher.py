"""PR #15 absorption guards."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


def _config_client(tmp_path, monkeypatch):
    import ducky.utils as utils

    monkeypatch.setattr(utils, "FACTS_DB", str(tmp_path / "facts.db"))
    from ducky.schema_bootstrap import ensure_core_schema

    ensure_core_schema(force=True)
    from ducky.utils import get_facts_conn

    conn = get_facts_conn()
    conn.executemany(
        "INSERT OR REPLACE INTO memory_banks "
        "(user_id, bank_id, display_name, status) VALUES (?, ?, ?, ?)",
        [("alice", "work", "Alice 工作", "active"),
         ("alice", "old", "Alice 旧域", "inactive"),
         ("bob", "home", "Bob 家庭", "active")],
    )
    conn.commit()
    conn.close()

    from ducky.routes_config import register_config_routes

    app = FastAPI()
    register_config_routes(app)
    return TestClient(app)


def test_domains_are_active_scoped_and_bounded(tmp_path, monkeypatch):
    client = _config_client(tmp_path, monkeypatch)
    body = client.get("/domains?limit=1").json()
    assert body["limit"] == 1
    assert len(body["domains"]) == 1
    assert body["domains"][0]["status"] == "active"
    assert "label" in body["domains"][0]
    assert body["default_domain"] == {"user_id": "default", "bank_id": "default"}


def test_domains_reject_unbounded_limit(tmp_path, monkeypatch):
    client = _config_client(tmp_path, monkeypatch)
    assert client.get("/domains?limit=0").status_code == 422
    assert client.get("/domains?limit=501").status_code == 422


def test_config_exposes_real_server_identity(tmp_path, monkeypatch):
    client = _config_client(tmp_path, monkeypatch)
    body = client.get("/config").json()
    from ducky.utils import DEFAULT_AGENT_ID, DEFAULT_USER_ID

    assert body["agent_id"] == DEFAULT_AGENT_ID
    assert body["user_id"] == DEFAULT_USER_ID


def test_frontend_has_no_static_or_synthetic_domain_mode():
    files = [ROOT / "frontend/index.html", ROOT / "frontend/js/api.js",
             ROOT / "frontend/js/main.js", ROOT / "frontend/js/panels.js"]
    text = "\n".join(path.read_text(encoding="utf-8") for path in files)
    for forbidden in ("hermes:default", "openclaw:default", 'value="all"',
                      "caller_agent_id: 'local'"):
        assert forbidden not in text, forbidden
    assert "default_domain" in text
    assert "Export current domain" in text
    assert "展示分组，不是记忆域" in text


def test_frontend_dossier_and_dynamic_user_styles_are_present():
    panels = (ROOT / "frontend/js/panels.js").read_text(encoding="utf-8")
    assert "/api/dossier?" in panels
    assert "API.parseDomain()" in panels
    assert 'style="font-weight:700' not in panels
    assert 'class="chip-user"' in panels


def test_domain_data_requests_are_not_global_evolve_requests():
    api = (ROOT / "frontend/js/api.js").read_text(encoding="utf-8")
    assert "evolve\\/(?:report|cycle)" in api
    assert "if (dom)" in api
    assert "await this.domainReady" in api
