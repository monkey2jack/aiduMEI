"""Real health/checker behavior for optional MCP sessions and host write loss."""
from datetime import datetime, timezone
import sqlite3
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from ducky import evolve_mem, utils
from ducky.degradation import DegradationTracker
from ducky.hot import health
from ducky.schema_bootstrap import ensure_core_schema
from scripts.check_ingest_wiring import diagnose


@pytest.fixture
def probe(monkeypatch, tmp_path):
    facts = tmp_path / "facts.db"
    reads = tmp_path / "evolve.db"
    monkeypatch.setenv("AIDUMEM_API_TOKEN", "synthetic-health-token")
    monkeypatch.setattr(utils, "FACTS_DB", str(facts))
    monkeypatch.setattr(health, "FACTS_DB", str(facts))
    monkeypatch.setattr(evolve_mem, "EVOLVE_DB_PATH", str(reads))
    ensure_core_schema(force=True)
    evolve_mem.ensure_evolve_schema()
    monkeypatch.setattr(health, "_INGEST_MIN_READS", 5)
    monkeypatch.setattr(DegradationTracker, "_degraded_map", {})
    monkeypatch.setattr(health, "_PUBLIC_CACHE", {"full": None, "ts": 0.0})
    app = FastAPI()
    health.register_health_routes(app)
    client = TestClient(app)

    def run(total, conversations, writes, *, background=0):
        with sqlite3.connect(reads) as db:
            db.execute("DELETE FROM evolve_queries")
            db.executemany("INSERT INTO evolve_queries (ts,origin_session_id,query) VALUES (?,?,'synthetic query')", [
                (time.time(), "test-session" if i < conversations else "") for i in range(total)])
        with sqlite3.connect(facts) as db:
            db.execute("DELETE FROM memory_epistemic")
            db.execute("DELETE FROM facts")
            db.executemany("INSERT INTO memory_epistemic (created_at,origin_session_id,epistemic_mode) VALUES (?,?,'user_provided')", [
                (datetime.now(timezone.utc).isoformat(), "test-session") for _ in range(writes)])
            db.executemany("INSERT INTO facts (created_at,fact_key,fact_value) VALUES (?,?,'synthetic-value')", [
                (datetime.now(timezone.utc).isoformat(), f"synthetic-key-{i}") for i in range(background)])
        response = client.get("/health", headers={"Authorization": "Bearer synthetic-health-token"})
        assert response.status_code == 200
        return response.json()

    yield run
    client.close()


@pytest.mark.parametrize("total,conversations,writes,state,strict_code", [
    (5, 0, 0, "unknown", 1),
    (5, 0, 1, "unknown", 1),
    (4, 4, 0, "unknown", 2),
    (0, 0, 0, "unknown", 2),
    (5, 5, 0, "missing_writes", 1),
    (5, 5, 1, "observed", 0),
])
def test_actual_health_and_strict_wiring_agree(probe, total, conversations, writes, state, strict_code):
    body = probe(total, conversations, writes, background=9)
    p = body["probes"]
    assert p["ingest_liveness_state"] == state
    assert p["ingest_reads_24h"] == total
    assert p["ingest_conv_reads_24h"] == conversations
    assert p["ingest_turn_writes_24h"] == writes
    assert p["ingest_liveness_ok"] is (state != "missing_writes")
    assert ("ingest_liveness" in body["degraded"]) is (state == "missing_writes")
    assert diagnose(body, require_judgment=True)[0] == strict_code
    if total >= 5 and conversations == 0:
        assert any("ingest_liveness:" in w and "MCP/REST" in w for w in body["warnings"])
        assert "无法判断" in " ".join(diagnose(body)[1])


@pytest.mark.parametrize("threshold", [2, 8])
def test_checker_uses_actual_server_threshold(probe, monkeypatch, threshold):
    monkeypatch.setattr(health, "_INGEST_MIN_READS", threshold)
    before = probe(threshold - 1, threshold - 1, 1)
    assert diagnose(before, require_judgment=True)[0] == 2
    after = probe(threshold, threshold, 1)
    assert after["probes"]["ingest_min_reads"] == threshold
    assert diagnose(after, require_judgment=True)[0] == 0


@pytest.mark.parametrize("conversations,writes,state", [(5, 1, "observed"), (0, 0, "unknown")])
def test_current_evidence_clears_only_ingest_tracker(probe, conversations, writes, state):
    broken = probe(5, 5, 0)
    assert "ingest_liveness" in broken["degraded"]
    DegradationTracker.record_degradation("other_component", "negative control")
    current = probe(5, conversations, writes)
    assert current["probes"]["ingest_liveness_state"] == state
    assert "ingest_liveness" not in current["degraded"]
    assert "other_component" in current["degraded"]
    assert current["health_status"] == "degraded"


def test_unknown_state_never_passes_strict_judgment():
    body = {"probes": {"ingest_reads_24h": 8, "ingest_conv_reads_24h": 8,
            "ingest_liveness_ok": True, "ingest_liveness_state": "unknown"}}
    assert diagnose(body, require_judgment=True)[0] == 2


def test_probe_failure_stays_degraded_and_checker_refuses(probe, monkeypatch):
    # Force a probe computation error without replacing a shared DB accessor.
    monkeypatch.setattr(health, "_INGEST_MIN_READS", None)
    body = probe(5, 0, 0)
    assert body["probes"]["ingest_liveness_state"] == "error"
    assert "ingest_liveness" in body["degraded"]
    assert diagnose(body, require_judgment=True)[0] == 2
