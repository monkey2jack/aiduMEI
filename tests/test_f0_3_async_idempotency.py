"""f0.3 C2 (S-2 / S-8 / H-10): async idempotency settles on the durable outcome.

Defect: the async /add path finalized the idempotency key with
{"status": "accepted", "durable": false} *before* its job ran.  A job that
failed never released the key and settled keys never expired, so the retry of
a failed async write (the distill hook uses deterministic keys + async) was
answered "accepted" forever and the write never happened.
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ducky.idempotency as idem


@pytest.fixture
def db(tmp_path, monkeypatch):
    import ducky.utils as utils
    path = tmp_path / "facts.db"
    monkeypatch.setattr(utils, "FACTS_DB", str(path))
    monkeypatch.setattr(idem, "_schema_ready_for", "")
    monkeypatch.setattr(idem, "_last_purge", {"at": 0.0, "db": ""})
    monkeypatch.delenv("AIDUMEI_IDEMPOTENCY_TTL_DAYS", raising=False)

    def rows():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM idempotency_keys ORDER BY idempotency_key")]
        finally:
            conn.close()
    return rows


class _Clock:
    def __init__(self, now: float):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    c = _Clock(1_800_000_000.0)
    monkeypatch.setattr(idem.time, "time", c)
    return c


PAYLOAD = {"messages": "summary text", "user_id": "alice", "bank_id": "work"}


# ── unit: claim / provisional / settle ─────────────────────────────────

def test_provisional_receipt_replays_only_within_the_job_lease(db, clock):
    st = idem.claim("k1", "alice", "work", PAYLOAD)
    assert st == {"action": "new", "key": "k1"}
    idem.finalize("k1", "alice", "work", {"status": "accepted", "durable": False,
                                         "job_id": "j1"},
                  claimed_at=idem.claim_token(st), provisional=True)
    again = idem.claim("k1", "alice", "work", PAYLOAD)
    assert again["action"] == "replay" and again["state"] == idem.STATE_ACCEPTED
    assert again["response"]["durable"] is False
    clock.now += idem._PENDING_TTL_SECONDS + 1          # job lost (restart/eviction)
    taken = idem.claim("k1", "alice", "work", PAYLOAD)
    assert taken["action"] == "new", "a lost async job must not be replayed as accepted forever"


def test_job_success_settles_durable_receipt(db, clock):
    st = idem.claim("k1", "alice", "work", PAYLOAD)
    binding = idem.job_binding(st, "k1", "alice", "work")
    idem.finalize("k1", "alice", "work", {"status": "accepted", "durable": False},
                  claimed_at=binding["claimed_at"], provisional=True)
    assert idem.settle_job(binding, ok=True, job_id="j1",
                           result={"status": "ok", "action": "direct"}) == "finalized"
    clock.now += idem._PENDING_TTL_SECONDS + 1           # well past the lease
    replay = idem.claim("k1", "alice", "work", PAYLOAD)
    assert replay["action"] == "replay" and replay["state"] == idem.STATE_DONE
    assert replay["response"]["durable"] is True and replay["response"]["job_id"] == "j1"


def test_job_failure_releases_the_key(db, clock):
    st = idem.claim("k1", "alice", "work", PAYLOAD)
    binding = idem.job_binding(st, "k1", "alice", "work")
    idem.finalize("k1", "alice", "work", {"status": "accepted", "durable": False},
                  claimed_at=binding["claimed_at"], provisional=True)
    assert idem.settle_job(binding, ok=False, job_id="j1") == "released"
    assert db() == []
    assert idem.claim("k1", "alice", "work", PAYLOAD)["action"] == "new"


def test_provisional_hand_off_never_overwrites_an_earlier_settlement(db, clock):
    """A coalesce worker may finish the job before /add writes its hand-off."""
    st = idem.claim("k1", "alice", "work", PAYLOAD)
    binding = idem.job_binding(st, "k1", "alice", "work")
    idem.settle_job(binding, ok=True, job_id="j1", result={"status": "ok"})
    idem.finalize("k1", "alice", "work", {"status": "accepted", "durable": False},
                  claimed_at=binding["claimed_at"], provisional=True)
    row, = db()
    assert row["state"] == idem.STATE_DONE and json.loads(row["response_json"])["durable"] is True


def test_stale_settlement_cannot_touch_a_later_takeover(db, clock):
    st = idem.claim("k1", "alice", "work", PAYLOAD)
    old = idem.job_binding(st, "k1", "alice", "work")
    idem.finalize("k1", "alice", "work", {"status": "accepted", "durable": False},
                  claimed_at=old["claimed_at"], provisional=True)
    clock.now += idem._PENDING_TTL_SECONDS + 1
    assert idem.claim("k1", "alice", "work", PAYLOAD)["action"] == "new"   # takeover
    idem.settle_job(old, ok=False, job_id="old")          # late failure of the old job
    idem.settle_job(old, ok=True, job_id="old", result={"status": "ok"})
    row, = db()
    assert row["response_json"] is None, "the old claim settled the new claim's row"


def test_release_with_token_never_deletes_a_done_receipt(db, clock):
    st = idem.claim("k1", "alice", "work", PAYLOAD)
    token = idem.claim_token(st)
    idem.finalize("k1", "alice", "work", {"status": "ok"}, claimed_at=token)
    idem.release("k1", "alice", "work", claimed_at=token)
    assert len(db()) == 1


# ── unit: TTL ───────────────────────────────────────────────────────────

def test_settled_receipts_expire_after_the_ttl(db, clock, monkeypatch):
    monkeypatch.setenv("AIDUMEI_IDEMPOTENCY_TTL_DAYS", "2")
    st = idem.claim("k1", "alice", "work", PAYLOAD)
    idem.finalize("k1", "alice", "work", {"status": "ok"}, claimed_at=idem.claim_token(st))
    clock.now += 1 * 86400
    assert idem.claim("k1", "alice", "work", PAYLOAD)["action"] == "replay"   # control
    clock.now += 1 * 86400 + 1
    assert idem.claim("k1", "alice", "work", PAYLOAD)["action"] == "new"


def test_opportunistic_purge_removes_expired_rows_of_other_keys(db, clock, monkeypatch):
    monkeypatch.setenv("AIDUMEI_IDEMPOTENCY_TTL_DAYS", "1")
    st = idem.claim("old", "alice", "work", PAYLOAD)
    idem.finalize("old", "alice", "work", {"status": "ok"}, claimed_at=idem.claim_token(st))
    clock.now += 2 * 86400
    idem.claim("fresh", "bob", "default", {"x": 1})       # any claim triggers the purge
    assert [r["idempotency_key"] for r in db()] == ["fresh"]


def test_expired_key_may_be_reused_with_a_new_payload(db, clock, monkeypatch):
    monkeypatch.setenv("AIDUMEI_IDEMPOTENCY_TTL_DAYS", "1")
    st = idem.claim("k1", "alice", "work", PAYLOAD)
    idem.finalize("k1", "alice", "work", {"status": "ok"}, claimed_at=idem.claim_token(st))
    assert idem.claim("k1", "alice", "work", {"other": 1})["action"] == "conflict"  # control
    clock.now += 2 * 86400
    assert idem.claim("k1", "alice", "work", {"other": 1})["action"] == "new"


@pytest.mark.parametrize("raw", ["0", "-3", "abc", "nan", "99999"])
def test_invalid_ttl_falls_back_to_the_default_and_is_reported(raw, monkeypatch):
    from ducky import env_config
    monkeypatch.setenv("AIDUMEI_IDEMPOTENCY_TTL_DAYS", raw)
    assert idem.ttl_days() == 7
    assert "AIDUMEI_IDEMPOTENCY_TTL_DAYS" in env_config.config_errors()
    monkeypatch.setenv("AIDUMEI_IDEMPOTENCY_TTL_DAYS", "3")
    assert idem.ttl_days() == 3


# ── unit: stored receipts carry no content ─────────────────────────────

def test_stored_and_replayed_receipts_are_redacted(db, clock):
    st = idem.claim("k1", "alice", "work", PAYLOAD)
    idem.finalize("k1", "alice", "work", {
        "status": "accepted", "durable": False, "job_id": "j1",
        "preview": "SECRET PREVIEW TEXT",
        "details": {"results": [{"id": "m1", "memory": "SECRET MEMORY", "event": "ADD"}]},
    }, claimed_at=idem.claim_token(st), provisional=True)
    row, = db()
    assert "SECRET" not in row["response_json"]
    assert json.loads(row["response_json"])["details"]["results"][0]["id"] == "m1"
    replay = idem.claim("k1", "alice", "work", PAYLOAD)
    assert "SECRET" not in json.dumps(replay["response"])


def test_legacy_receipt_content_is_not_replayed(db, clock):
    import ducky.utils as utils
    idem.claim("warm", "x", "default", {})
    conn = sqlite3.connect(utils.FACTS_DB)
    conn.execute("INSERT INTO idempotency_keys VALUES (?,?,?,?,?,?,NULL)",
                 ("legacy", "alice", "work", idem._fingerprint(PAYLOAD),
                  json.dumps({"status": "ok", "preview": "OLD SECRET"}), clock.now - 60))
    conn.commit()
    conn.close()
    replay = idem.claim("legacy", "alice", "work", PAYLOAD)
    assert replay["action"] == "replay" and "OLD SECRET" not in json.dumps(replay["response"])


@pytest.mark.parametrize("durable,expected", [(False, "new"), (None, "replay")])
def test_legacy_accepted_receipts_are_provisional(db, clock, durable, expected):
    """Rows written before f0.3 said accepted/durable:false forever."""
    import ducky.utils as utils
    idem.claim("warm", "x", "default", {})
    receipt = {"status": "accepted" if durable is False else "ok"}
    if durable is False:
        receipt["durable"] = False
    conn = sqlite3.connect(utils.FACTS_DB)
    conn.execute("INSERT INTO idempotency_keys VALUES (?,?,?,?,?,?,NULL)",
                 ("legacy", "alice", "work", idem._fingerprint(PAYLOAD),
                  json.dumps(receipt), clock.now - 3600))
    conn.commit()
    conn.close()
    assert idem.claim("legacy", "alice", "work", PAYLOAD)["action"] == expected


def test_schema_migration_adds_state_to_a_pre_f03_table(tmp_path, monkeypatch):
    import ducky.utils as utils
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE idempotency_keys (
        idempotency_key TEXT NOT NULL, user_id TEXT NOT NULL, bank_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL, response_json TEXT, created_at REAL NOT NULL,
        PRIMARY KEY (idempotency_key, user_id, bank_id))""")
    conn.commit()
    conn.close()
    monkeypatch.setattr(utils, "FACTS_DB", str(path))
    monkeypatch.setattr(idem, "_schema_ready_for", "")
    assert idem.claim("k", "u", "b", {})["action"] == "new"
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(idempotency_keys)")}
    assert "state" in cols


# ── route: the async /add path end to end ──────────────────────────────

class _Mem:
    """Signature-aligned with mem0.Memory.add / get_all / search."""

    def __init__(self):
        self.adds = []
        self.broken = False

    def add(self, messages, *, user_id=None, agent_id=None, run_id=None, metadata=None,
            timestamp=None, expiration_date=None, infer=True, memory_type=None, prompt=None):
        if self.broken:
            raise RuntimeError("vector store down (simulated)")
        self.adds.append(messages)
        return {"results": [{"id": f"m-{len(self.adds)}", "memory": str(messages),
                             "event": "ADD"}]}

    def get_all(self, *, filters=None, top_k=20, show_expired=False, **kwargs):
        return {"results": []}

    def search(self, query, *, top_k=20, filters=None, **kwargs):
        return {"results": []}


@pytest.fixture
def route(db, monkeypatch):
    import ducky.add_speed as speed
    import ducky.gear as gear
    import ducky.hot.add as hot_add
    import ducky.mem0_runtime as runtime
    from ducky.speed import coalesce

    monkeypatch.setenv("AIDUMEI_ENGINE_MODE", "cloud")
    monkeypatch.setattr(coalesce, "_coalesce_buf", {})
    monkeypatch.setattr(coalesce, "_coalesce_flush_cb", None)
    monkeypatch.setattr(coalesce, "ensure_coalesce_worker", lambda: None)
    monkeypatch.setattr(speed, "ensure_coalesce_worker", lambda: None)
    gear.reset_gear_for_tests()
    mem = _Mem()
    monkeypatch.setattr(runtime, "get_memory", lambda: mem)
    monkeypatch.setattr(hot_add, "get_memory", lambda: mem)
    monkeypatch.setattr(hot_add, "patch_llm_for_speed", lambda m: None, raising=False)

    def layer1(memory, messages_json, user_id, metadata, bank_id="default", infer=True):
        out = memory.add(messages_json, user_id=user_id, metadata=metadata, infer=infer)
        return {"status": "ok", "action": "indexed", "details": {"n": len(out["results"])}}

    monkeypatch.setattr(hot_add, "lazy_import_layer1", lambda: layer1)
    app = FastAPI()
    hot_add.register_add_routes(app)
    # A failing background job re-raises after marking its job (as in
    # production, where Starlette logs it); the response was already sent.
    yield TestClient(app, raise_server_exceptions=False), mem
    gear.reset_gear_for_tests()


BODY = {"messages": "distilled summary of the session", "user_id": "alice",
        "bank_id": "work", "async_mode": True, "infer": False,
        "idempotency_key": "shell-distill-abc", "metadata": {"no_coalesce": True}}


def test_failed_async_write_is_retried_not_replayed(route, db):
    client, mem = route
    mem.broken = True
    first = client.post("/add", json=BODY)
    assert first.status_code == 200 and first.json()["status"] == "accepted"
    # The background job ran inside the TestClient call and failed.
    assert db() == [], "the failed job kept its key -> every retry would replay 'accepted'"
    mem.broken = False
    second = client.post("/add", json=BODY)
    assert second.json().get("idempotency_replayed") is not True
    assert mem.adds == [BODY["messages"]], "the retry did not execute the write"


def test_successful_async_write_replays_the_durable_receipt(route, db):
    """Negative control: success keeps the key and replays, without re-writing."""
    client, mem = route
    first = client.post("/add", json=BODY)
    assert first.json()["status"] == "accepted"
    row, = db()
    assert row["state"] == idem.STATE_DONE and "preview" not in json.loads(row["response_json"])
    second = client.post("/add", json=BODY).json()
    assert second["idempotency_replayed"] is True
    assert second["durable"] is True and second["status"] == "ok"
    assert mem.adds == [BODY["messages"]], "a replay must not write twice"
