"""f0.3 system-only endpoint families are default OFF behind explicit flags.

These families expose derived data that is not partitioned by the
(user_id, bank_id) tenant axis: persona banks (enumerable autoincrement ids),
skill crystals, the server's own code graph, store-wide evolve report/cycle
and skill drafts.  Routes stay registered (route table, OpenAPI, MCP contract
are stable) and answer ``404 feature_disabled`` until the flag is set.
Endpoints on the default integration paths (/evolve/feedback,
/evolve/episode/feedback) stay on.
"""

from __future__ import annotations

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ducky import system_endpoints as se

ALL_ROUTES = sorted(se.FAMILY_BY_ROUTE)
ALL_FLAGS = sorted({f.flag for f in se.FAMILIES})


@pytest.fixture
def flags_off(monkeypatch):
    for flag in ALL_FLAGS:
        monkeypatch.delenv(flag, raising=False)
    return monkeypatch


@pytest.fixture(scope="module")
def server():
    import api_server
    return api_server


@pytest.fixture
def client(server):
    return TestClient(server.app)


def _call(client: TestClient, method: str, path: str):
    if method == "GET":
        return client.get(path)
    return client.request(method, path, json={})


def test_family_table_matches_the_flags_the_owner_must_set() -> None:
    assert {f.name: f.flag for f in se.FAMILIES} == {
        "persona": "AIDUMEM_PERSONA_ENABLED",
        "crystals": "AIDUMEI_CRYSTALS_ENABLED",
        "code_graph": "AIDUMEI_CODE_GRAPH_ENABLED",
        "evolve_admin": "AIDUMEI_EVOLVE_ADMIN_ENABLED",
        "skill_drafts": "AIDUMEI_SKILL_DRAFTS_ENABLED",
    }
    assert len(ALL_ROUTES) == 17


@pytest.mark.parametrize("app_name", ["app", "_api_alias"])
def test_every_listed_endpoint_is_registered_with_its_gate(server, app_name: str) -> None:
    status = se.gate_status(getattr(server, app_name))
    assert set(status.values()) == {"gated"}, {k: v for k, v in status.items() if v != "gated"}


@pytest.mark.parametrize("method,path", ALL_ROUTES)
def test_default_off_answers_feature_disabled(flags_off, client, method: str, path: str) -> None:
    response = _call(client, method, path)

    assert response.status_code == 404, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "feature_disabled"
    assert detail["feature_flag"] == se.FAMILY_BY_ROUTE[(method, path)].flag


def test_persona_bank_ids_are_not_enumerable_by_default(flags_off, client) -> None:
    for bank_id in (1, 2, 3):
        response = client.get("/persona/detail", params={"bank_id": bank_id})
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "feature_disabled"
    # The console reaches the same routes through the /api alias.
    assert client.get("/api/persona/banks").status_code == 404


def test_enabling_one_family_leaves_the_others_off(flags_off, client) -> None:
    flags_off.setenv("AIDUMEI_CRYSTALS_ENABLED", "true")

    enabled = client.get("/crystals")
    assert enabled.status_code == 200 and enabled.json()["status"] == "ok"
    assert client.get("/api/crystals").status_code == 200
    assert client.get("/evolve/report").status_code == 404
    assert client.get("/skill/drafts").status_code == 404


def test_evolve_report_opens_with_its_own_flag(flags_off, client) -> None:
    flags_off.setenv("AIDUMEI_EVOLVE_ADMIN_ENABLED", "1")
    response = client.get("/evolve/report")
    assert response.status_code == 200
    assert response.json().get("detail", {}) != {"code": "feature_disabled"}


def test_default_integration_endpoints_stay_on(flags_off, client) -> None:
    feedback = client.post("/evolve/feedback", json={"memory_id": "synthetic-id", "signal": "useful"})
    episode = client.post("/evolve/episode/feedback",
                          json={"session_id": "synthetic-session", "reward": 0.5})
    assert feedback.status_code == 200, feedback.text
    assert episode.status_code == 200, episode.text
    for path in ("/evolve/feedback", "/evolve/episode/feedback"):
        assert ("POST", path) not in se.FAMILY_BY_ROUTE


@pytest.mark.parametrize("value,expected", [
    ("true", True), ("1", True), ("yes", True), ("on", True), (" TRUE ", True),
    ("", False), ("false", False), ("0", False), ("no", False), ("enabled", False),
])
def test_flags_are_explicit_opt_in(monkeypatch, value: str, expected: bool) -> None:
    monkeypatch.setenv("AIDUMEI_CRYSTALS_ENABLED", value)
    assert se.feature_enabled("AIDUMEI_CRYSTALS_ENABLED") is expected


def test_negative_control_old_persona_default_was_on(monkeypatch) -> None:
    monkeypatch.delenv("AIDUMEM_PERSONA_ENABLED", raising=False)
    # Previous rule (persona_memory.py before f0.3): anything but 0/false/no/off,
    # with "true" as the default -> an unset variable meant "enabled".
    old = os.environ.get("AIDUMEM_PERSONA_ENABLED", "true").strip().lower() not in {
        "0", "false", "no", "off"}
    assert old is True
    assert se.feature_enabled("AIDUMEM_PERSONA_ENABLED") is False


def test_openapi_marks_system_only_routes(server) -> None:
    schema = server.app.openapi()
    for method, path in ALL_ROUTES:
        operation = schema["paths"][path][method.lower()]
        assert se.SYSTEM_ONLY_TAG in operation.get("tags", []), (method, path)
        assert operation["x-aidumei-tenant-isolated"] is False
        assert operation["x-aidumei-feature-flag"] == se.FAMILY_BY_ROUTE[(method, path)].flag
    feedback = schema["paths"]["/evolve/feedback"]["post"]
    assert se.SYSTEM_ONLY_TAG not in feedback.get("tags", [])
    assert "x-aidumei-tenant-isolated" not in feedback


def test_registrar_gates_modules_that_do_not_gate_themselves(flags_off) -> None:
    from ducky.routes_p1 import register_p1_routes

    app = FastAPI()
    register_p1_routes(se.GatedRegistrar(app))

    status = se.gate_status(app)
    assert status[("GET", "/skill/drafts")] == status[("POST", "/skill/grow")] == "gated"
    client = TestClient(app)
    assert client.get("/skill/drafts").status_code == 404
    assert client.get("/memory/types").status_code == 200  # unlisted routes pass through
    flags_off.setenv("AIDUMEI_SKILL_DRAFTS_ENABLED", "true")
    assert client.get("/skill/drafts").status_code == 200


def test_negative_control_bare_registration_is_caught_at_startup() -> None:
    from ducky.routes_p1 import register_p1_routes

    app = FastAPI()
    register_p1_routes(app)  # the pre-f0.3 registration: /skill/* ungated

    assert se.gate_status(app)[("GET", "/skill/drafts")] == "ungated"
    with pytest.raises(RuntimeError):
        se.assert_all_gated(app)
