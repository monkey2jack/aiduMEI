"""f0.3 system-only endpoint families are default OFF behind explicit flags.

These families expose derived data that is not partitioned by the
(user_id, bank_id) tenant axis: skill crystals and the store-wide evolve
report.  Routes stay registered and answer ``404 feature_disabled`` until the
flag is set.  /evolve/feedback (console) stays on.

f0.6+++ WP2: persona / code_graph / skill_drafts families and the
evolve cycle were removed with their routes; the two remaining families are
console routes, registered only on the /api alias.
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
    # Hosts that run the suite with AIDUMEM_API_TOKEN set (production-shaped
    # sandboxes) have the auth gate on; carry the credential so these cases
    # measure the feature flags, not the auth gate.
    headers = {}
    token = os.environ.get("AIDUMEM_API_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return TestClient(server.app, headers=headers)


def _call(client: TestClient, method: str, path: str):
    if method == "GET":
        return client.get(path)
    return client.request(method, path, json={})


def test_family_table_matches_the_flags_the_owner_must_set() -> None:
    assert {f.name: f.flag for f in se.FAMILIES} == {
        "crystals": "AIDUMEI_CRYSTALS_ENABLED",
        "evolve_admin": "AIDUMEI_EVOLVE_ADMIN_ENABLED",
    }
    assert len(ALL_ROUTES) == 2


def test_every_listed_endpoint_is_registered_with_its_gate(server) -> None:
    status = se.gate_status(server._api_alias)
    assert set(status.values()) == {"gated"}, {k: v for k, v in status.items() if v != "gated"}
    # Console-only families are not exposed on the flat (hook) surface at all.
    assert set(se.gate_status(server.app).values()) == {"missing"}


@pytest.mark.parametrize("method,path", ALL_ROUTES)
def test_default_off_answers_feature_disabled(flags_off, client, method: str, path: str) -> None:
    response = _call(client, method, "/api" + path)

    assert response.status_code == 404, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "feature_disabled"
    assert detail["feature_flag"] == se.FAMILY_BY_ROUTE[(method, path)].flag


def test_enabling_one_family_leaves_the_others_off(flags_off, client) -> None:
    flags_off.setenv("AIDUMEI_CRYSTALS_ENABLED", "true")

    enabled = client.get("/api/crystals")
    assert enabled.status_code == 200 and enabled.json()["status"] == "ok"
    assert client.get("/api/evolve/report").status_code == 404


def test_evolve_report_opens_with_its_own_flag(flags_off, client) -> None:
    flags_off.setenv("AIDUMEI_EVOLVE_ADMIN_ENABLED", "1")
    response = client.get("/api/evolve/report")
    assert response.status_code == 200
    assert response.json().get("detail", {}) != {"code": "feature_disabled"}


def test_default_integration_endpoints_stay_on(flags_off, client) -> None:
    feedback = client.post("/api/evolve/feedback", json={"memory_id": "synthetic-id", "signal": "useful"})
    assert feedback.status_code == 200, feedback.text
    assert ("POST", "/evolve/feedback") not in se.FAMILY_BY_ROUTE


@pytest.mark.parametrize("value,expected", [
    ("true", True), ("1", True), ("yes", True), ("on", True), (" TRUE ", True),
    ("", False), ("false", False), ("0", False), ("no", False), ("enabled", False),
])
def test_flags_are_explicit_opt_in(monkeypatch, value: str, expected: bool) -> None:
    monkeypatch.setenv("AIDUMEI_CRYSTALS_ENABLED", value)
    assert se.feature_enabled("AIDUMEI_CRYSTALS_ENABLED") is expected


def test_openapi_marks_system_only_routes(server) -> None:
    schema = server._api_alias.openapi()
    for method, path in ALL_ROUTES:
        operation = schema["paths"][path][method.lower()]
        assert se.SYSTEM_ONLY_TAG in operation.get("tags", []), (method, path)
        assert operation["x-aidumei-tenant-isolated"] is False
        assert operation["x-aidumei-feature-flag"] == se.FAMILY_BY_ROUTE[(method, path)].flag
    feedback = schema["paths"]["/evolve/feedback"]["post"]
    assert se.SYSTEM_ONLY_TAG not in feedback.get("tags", [])
    assert "x-aidumei-tenant-isolated" not in feedback


def test_system_route_kwargs_gate_a_fresh_registration(flags_off) -> None:
    app = FastAPI()

    @app.get("/crystals", **se.system_route("crystals"))
    def _crystals():
        return {"status": "ok"}

    assert se.gate_status(app)[("GET", "/crystals")] == "gated"
    se.assert_all_gated(app)
    client = TestClient(app)
    assert client.get("/crystals").status_code == 404
    flags_off.setenv("AIDUMEI_CRYSTALS_ENABLED", "true")
    assert client.get("/crystals").status_code == 200


def test_negative_control_bare_registration_is_caught_at_startup() -> None:
    app = FastAPI()

    @app.get("/crystals")  # registered without system_route(): ungated
    def _crystals():
        return {"status": "ok"}

    assert se.gate_status(app)[("GET", "/crystals")] == "ungated"
    with pytest.raises(RuntimeError):
        se.assert_all_gated(app)
