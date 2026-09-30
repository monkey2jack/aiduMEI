"""Confirmed core refresh preserves provenance, scope and concurrent edits."""
from datetime import datetime, timedelta, timezone

import pytest

import ducky.core_memory as core
import ducky.core_maintenance as maintenance
import ducky.utils as utils
from ducky import schema_bootstrap


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(utils, "FACTS_DB", str(tmp_path / "facts.db"))
    monkeypatch.setattr(schema_bootstrap, "_done", False)
    monkeypatch.setattr(core, "_initialized", False)
    monkeypatch.setattr(core, "_initialized_scopes", set())
    monkeypatch.setattr(core, "_index_core_block", lambda *a: None)
    monkeypatch.setattr(core, "_vector_index_core_block", lambda *a: None)
    schema_bootstrap.ensure_core_schema()
    core.init_core_memory("alice", "work")
    core.put_block("core_current_project", "Project Atlas is preparing its first release.", "alice", "work")
    conn = utils.get_facts_conn()
    conn.execute("UPDATE core_memory SET last_verified_at='2020-01-01T00:00:00' WHERE user_id='alice'")
    conn.commit()
    yield conn


def fact(db, *, user="alice", bank="work", mode="user_provided", value="Project Atlas release is deployed and verified.", category="core_memory", **extra):
    cols = dict(category=category, fact_key="core_current_project", fact_value=value,
                user_id=user, bank_id=bank, agent_id=user, source="user_confirmation",
                epistemic_mode=mode, updated_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), confidence=100, **extra)
    cur = db.execute("INSERT INTO facts (" + ",".join(cols) + ") VALUES (" + ",".join("?" for _ in cols) + ")", tuple(cols.values()))
    db.commit()
    return cur.lastrowid


def test_confirmed_project_refresh_archives_old_value_and_evidence(db):
    fid = fact(db)
    out = maintenance.project_refresh("alice", "work")
    assert out["status"] == "updated" and out["fact_id"] == fid
    assert "deployed" in core.get_block("core_current_project", "alice", "work")["content"]
    history = maintenance.revision_history("core_current_project", "alice", "work")
    assert "preparing" in history[0]["old_content"]
    assert '"fact_id": ' + str(fid) in history[0]["evidence_json"]
    assert maintenance.project_refresh("alice", "work")["status"] == "not_newer"
    assert maintenance.revision_history("core_current_project", "bob", "work") == []


@pytest.mark.parametrize("mode", ["reasoned", "fuzzy", "referenced"])
def test_unconfirmed_evidence_does_not_refresh_timestamp(db, mode):
    fact(db, mode=mode)
    before = core.get_block("core_current_project", "alice", "work")
    assert maintenance.project_refresh("alice", "work")["status"] == "no_confirmed_evidence"
    assert core.get_block("core_current_project", "alice", "work") == before


def test_no_evidence_does_not_blindly_refresh(db):
    before = core.get_block("core_current_project", "alice", "work")
    assert maintenance.project_refresh("alice", "work")["status"] == "no_confirmed_evidence"
    assert core.get_block("core_current_project", "alice", "work") == before


@pytest.mark.parametrize("values", [{"archived": 1}, {"superseded_by": 999},
                                    {"expires_at": "2020-01-01"}, {"valid_from": "2999-01-01"}])
def test_retracted_expired_or_future_state_is_not_applied(db, values):
    fact(db, **values)
    assert maintenance.project_refresh("alice", "work")["status"] == "no_confirmed_evidence"


def test_other_scope_cannot_refresh_owner(db):
    fact(db, user="bob")
    fact(db, bank="home")
    assert maintenance.project_refresh("alice", "work")["status"] == "no_confirmed_evidence"


def test_concurrent_core_edit_is_preserved(db):
    old = core.get_block("core_current_project", "alice", "work")["content"]
    core.put_block("core_current_project", "Project Atlas now has a different confirmed scope.", "alice", "work")
    with pytest.raises(ValueError, match="concurrently"):
        core.put_block("core_current_project", "Project Atlas release is deployed and verified.", "alice", "work", expected_content=old)
    assert "different" in core.get_block("core_current_project", "alice", "work")["content"]


def test_evidence_rechecked_inside_write_transaction(db):
    fid = fact(db)
    evidence = dict(db.execute("SELECT * FROM facts WHERE id=?", (fid,)).fetchone())
    db.execute("UPDATE facts SET archived=1 WHERE id=?", (fid,))
    db.commit()
    with pytest.raises(ValueError, match="evidence"):
        core.put_block("core_current_project", evidence["fact_value"], "alice", "work", evidence_fact=evidence)
    assert "preparing" in core.get_block("core_current_project", "alice", "work")["content"]


def test_daily_runner_processes_explicit_evidence(db):
    fact(db)
    assert maintenance.refresh_project_blocks() == {"updated": 1}


def test_conflicting_confirmed_facts_preserve_existing_block(db):
    fact(db)
    db.execute("UPDATE facts SET agent_id='other-agent'")
    db.commit()
    fact(db, value="Project Atlas is cancelled until further notice.")
    before = core.get_block("core_current_project", "alice", "work")
    assert maintenance.project_refresh("alice", "work")["status"] == "conflicting_evidence"
    assert core.get_block("core_current_project", "alice", "work") == before


@pytest.mark.parametrize("stamp, status", [("invalid", "invalid_evidence_time"),
                                           ("2999-01-01T00:00:00Z", "future_evidence_time")])
def test_invalid_evidence_clock_cannot_refresh(db, stamp, status):
    fid = fact(db)
    db.execute("UPDATE facts SET updated_at=? WHERE id=?", (stamp, fid))
    db.commit()
    assert maintenance.project_refresh("alice", "work")["status"] == status


def test_timestamp_comparison_uses_instant_not_lexical_order(db):
    fid = fact(db)
    db.execute("UPDATE facts SET updated_at='2021-01-01T08:00:00+08:00' WHERE id=?", (fid,))
    db.execute("UPDATE core_memory SET last_verified_at='2021-01-01T01:00:00Z'")
    db.commit()
    assert maintenance.project_refresh("alice", "work")["status"] == "not_newer"


def test_failed_core_write_rolls_back_history_together(db):
    before = maintenance.revision_history("core_current_project", "alice", "work")
    db.execute("CREATE TRIGGER fail_core BEFORE UPDATE ON core_memory WHEN NEW.content <> OLD.content BEGIN SELECT RAISE(ABORT,'test rollback'); END")
    db.commit()
    with pytest.raises(Exception, match="test rollback"):
        core.put_block("core_current_project", "Project Atlas release is deployed and verified.", "alice", "work")
    assert maintenance.revision_history("core_current_project", "alice", "work") == before
    assert not db.in_transaction


def test_refresh_and_history_routes_enforce_read_and_write_grants(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import ducky.routes_clotho as routes
    import ducky.pantheon as pantheon
    import ducky.security.auth as auth
    monkeypatch.setattr(auth, "current_request_auth_kind", lambda: "bearer")
    calls = []
    grants = set()
    monkeypatch.setattr(pantheon, "check_hall_access", lambda target, caller, action="read", bank_id=None:
                        (target, caller, action, bank_id) in grants)
    monkeypatch.setattr(maintenance, "project_refresh", lambda user_id, bank_id:
                        calls.append((user_id, bank_id)) or {"status": "updated"})
    monkeypatch.setattr(maintenance, "revision_history", lambda block_key, user_id, bank_id:
                        [{"scope": [user_id, bank_id]}])
    app = FastAPI()
    routes.register_clotho_routes(app)
    client = TestClient(app)
    params = {"user_id": "alice", "bank_id": "work", "caller_user_id": "bob"}
    history = "/api/core-memory/core_current_project/history"
    refresh = "/api/core-memory/core_current_project/refresh"
    assert client.get(history, params=params).status_code == 403
    assert client.post(refresh, params=params).status_code == 403
    grants.add(("alice", "bob", "read", "work"))
    assert client.get(history, params=params).json()["revisions"] == [{"scope": ["alice", "work"]}]
    assert client.post(refresh, params=params).status_code == 403
    assert calls == []
    params["caller_user_id"] = "alice"
    assert client.post(refresh, params=params).json()["status"] == "updated"
    assert calls == [("alice", "work")]
