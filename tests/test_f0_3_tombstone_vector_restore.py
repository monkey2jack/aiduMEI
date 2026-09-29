"""f0.3 C7: restoring a memory tombstone restores its vector point.

Defect: restore_tombstone re-inserted facts and rebuilt FTS only.  A memory
deleted by the cascade (the consolidator did this every night) stayed absent
from vector recall after a "successful" restore.  Now the point is re-embedded
with the service's own embedder and written back with its ORIGINAL id and a
payload in mem0's write shape; every layer is idempotent and the tombstone is
stamped only when all required layers are back.
"""
from __future__ import annotations

import hashlib
import sqlite3
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_PROMOTED = {"user_id", "agent_id", "run_id", "actor_id", "role"}
_CORE = {"data", "hash", "created_at", "updated_at", "text_lemmatized"} | _PROMOTED


class _Point:
    def __init__(self, pid, payload):
        self.id = pid
        self.payload = payload


class _Store:
    """Signature-aligned with mem0.vector_stores.qdrant.Qdrant insert/get/delete."""

    def __init__(self):
        self.points: dict = {}
        self.inserts = 0

    def insert(self, vectors, payloads=None, ids=None):
        for i, pid in enumerate(ids or []):
            self.inserts += 1
            self.points[str(pid)] = dict((payloads or [{}])[i])

    def get(self, vector_id):
        p = self.points.get(str(vector_id))
        return _Point(str(vector_id), dict(p)) if p is not None else None

    def delete(self, vector_id):
        self.points.pop(str(vector_id), None)


class _Embedder:
    def __init__(self):
        self.calls: list = []
        self.broken = False

    def embed(self, text, memory_action=None):
        if self.broken:
            raise RuntimeError("embedding gateway down (simulated)")
        self.calls.append((text, memory_action))
        return [0.1, 0.2, 0.3]


class FakeMem0:
    def __init__(self):
        self.vector_store = _Store()
        self.embedding_model = _Embedder()

    def get_all(self, *, filters=None, top_k=20, show_expired=False, **kwargs):
        out = []
        for pid, p in self.vector_store.points.items():
            if any(p.get(k) != v for k, v in (filters or {}).items()):
                continue
            item = {"id": pid, "memory": p.get("data", ""), "hash": p.get("hash")}
            item.update({k: p[k] for k in _PROMOTED if k in p})
            extra = {k: v for k, v in p.items() if k not in _CORE}
            if extra:
                item["metadata"] = extra
            out.append(item)
        return {"results": out[:top_k]}

    def delete(self, memory_id):
        if str(memory_id) not in self.vector_store.points:
            raise ValueError(f"Memory with id {memory_id} not found")
        self.vector_store.delete(memory_id)
        return {"message": "Memory deleted successfully!"}


@pytest.fixture
def world(tmp_path, monkeypatch):
    import ducky.mem0_runtime as runtime
    import ducky.memory_types as mt
    import ducky.utils as utils
    from ducky.schema_bootstrap import ensure_core_schema
    from ducky.text_fts import _init_text_fts
    from ducky.tombstone import ensure_tombstone_schema

    facts_db = str(tmp_path / "facts.db")
    monkeypatch.setattr(utils, "FACTS_DB", facts_db)
    monkeypatch.setattr(utils, "TEXT_FTS_DB", str(tmp_path / "text_fts.db"))
    monkeypatch.setattr(mt, "_checked", False)
    monkeypatch.setenv("AIDUMEI_ENGINE_MODE", "cloud")
    ensure_core_schema(force=True)
    _init_text_fts()
    ensure_tombstone_schema()
    fake = FakeMem0()
    monkeypatch.setattr(runtime, "get_memory", lambda: fake)

    def q(sql, params=()):
        conn = sqlite3.connect(facts_db)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()
    return fake, q


TEXT = "the user moved the weekly review to thursday mornings"


def _memory(fake, user="alice", bank="work", text=TEXT):
    """A mem0 memory as the write path leaves it: vector point + FTS row."""
    from ducky.text_fts import _index_memory
    pid = str(uuid.uuid4())
    fake.vector_store.insert([[0.3]], payloads=[{
        "data": text, "hash": hashlib.md5(text.encode()).hexdigest(),
        "created_at": "2026-09-01T08:00:00+00:00", "updated_at": "2026-09-01T08:00:00+00:00",
        "text_lemmatized": text, "user_id": user, "bank_id": bank, "role": "user",
        "category": "schedule", "recorded_at": "2026-09-01T08:00:00+00:00",
        "_origin_session_id": "s-7", "_origin_agent": "hermes"}], ids=[pid])
    _index_memory(pid, text, user_id=user, bank_id=bank)
    return pid


def _delete(pid, user="alice", bank="work"):
    from ducky.wal_engine import cascade_delete_memory
    out = cascade_delete_memory(pid, user_id=user, bank_id=bank)
    assert out["status"] == "committed", out
    return out["details"]["tombstone_id"]


def _in_scope_ids(fake, user="alice", bank="work"):
    from ducky.bank_contract import vector_item_in_bank, vector_scope_filters
    items = fake.get_all(filters=vector_scope_filters(user, bank), top_k=100)["results"]
    return {i["id"] for i in items if vector_item_in_bank(i, bank)}


def test_restore_brings_the_point_back_with_its_original_id(world):
    fake, q = world
    from ducky.tombstone import restore_tombstone
    pid = _memory(fake)
    tid = _delete(pid)
    assert pid not in _in_scope_ids(fake), "negative control: the point is really gone"

    res = restore_tombstone(tid, user_id="alice", bank_id="work")
    assert res["status"] == "ok" and res["restored"] is True, res
    assert res["layers"]["vector"] == "restored" and "vector" in res["detail"]
    assert res["verification"] == {"facts_row": None, "fts_row": True, "vector_point": True}
    assert pid in _in_scope_ids(fake), "restored memory is still absent from vector recall"
    payload = fake.vector_store.points[pid]
    assert payload["data"] == TEXT
    assert payload["hash"] == hashlib.md5(TEXT.encode()).hexdigest()
    assert (payload["user_id"], payload["bank_id"]) == ("alice", "work")
    # metadata captured at delete time survives the round trip
    assert payload["category"] == "schedule" and payload["_origin_session_id"] == "s-7"
    assert payload["created_at"] == "2026-09-01T08:00:00+00:00"
    assert payload["restored_from_tombstone"] == tid and payload["text_lemmatized"]
    assert fake.embedding_model.calls == [(TEXT, "add")]
    assert q("SELECT restored_at FROM tombstones WHERE tombstone_id=?", (tid,))[0]["restored_at"]


def test_restore_is_idempotent_across_a_partial_first_attempt(world, monkeypatch):
    fake, q = world
    import ducky.text_fts as fts
    from ducky.tombstone import restore_tombstone
    pid = _memory(fake)
    tid = _delete(pid)
    real = fts._index_memory

    def broken(memory_id, content, user_id="default", category=None, bank_id="default",
               memory_type=None):
        raise sqlite3.OperationalError("database is locked (simulated)")

    monkeypatch.setattr(fts, "_index_memory", broken)
    first = restore_tombstone(tid, user_id="alice", bank_id="work")
    assert first["status"] == "partial" and first["restored"] is False
    assert q("SELECT restored_at FROM tombstones WHERE tombstone_id=?", (tid,))[0]["restored_at"] is None
    monkeypatch.setattr(fts, "_index_memory", real)
    second = restore_tombstone(tid, user_id="alice", bank_id="work")
    assert second["status"] == "ok", second
    assert second["layers"]["vector"] == "already_present"
    assert fake.vector_store.inserts == 2, "retry inserted the point a second time"  # seed + 1


def test_vector_failure_blocks_the_stamp_and_a_retry_completes(world):
    fake, q = world
    from ducky.tombstone import restore_tombstone
    pid = _memory(fake)
    tid = _delete(pid)
    fake.embedding_model.broken = True
    first = restore_tombstone(tid, user_id="alice", bank_id="work")
    assert first["status"] == "partial" and first["layers"]["vector"] == "failed:RuntimeError"
    assert first["verification"]["vector_point"] is False
    assert q("SELECT restored_at FROM tombstones WHERE tombstone_id=?", (tid,))[0]["restored_at"] is None
    fake.embedding_model.broken = False
    assert restore_tombstone(tid, user_id="alice", bank_id="work")["status"] == "ok"
    assert pid in _in_scope_ids(fake)


def test_a_point_owned_by_another_scope_is_never_overwritten(world):
    fake, _ = world
    from ducky.tombstone import restore_tombstone
    pid = _memory(fake)
    tid = _delete(pid)
    fake.vector_store.insert([[0.9]], payloads=[{"data": "bob's", "user_id": "bob",
                                                 "bank_id": "work"}], ids=[pid])
    res = restore_tombstone(tid, user_id="alice", bank_id="work")
    assert res["status"] == "partial"
    assert res["layers"]["vector"] == "failed:id_owned_by_another_scope"
    assert fake.vector_store.points[pid]["user_id"] == "bob"


def test_snapshot_never_captures_another_tenants_payload(world):
    fake, _ = world
    from ducky.bank_contract import make_scope
    from ducky.tombstone import _capture_vector_payload
    pid = _memory(fake, user="bob")
    assert _capture_vector_payload(pid, make_scope("alice", "work")) == {}
    assert _capture_vector_payload(pid, make_scope("bob", "work"))["category"] == "schedule"


def test_legacy_tombstone_without_vector_snapshot_restores_a_minimal_point(world):
    fake, q = world
    from ducky.tombstone import ensure_tombstone_schema, restore_tombstone
    from ducky.utils import get_facts_conn
    ensure_tombstone_schema()
    pid = str(uuid.uuid4())
    conn = get_facts_conn()
    cur = conn.execute(
        "INSERT INTO tombstones (target_id, target_type, user_id, bank_id, content_snapshot, "
        "facts_snapshot, reason, actor, tombstoned_at) VALUES (?, 'memory', 'alice', 'work', "
        "?, '', 'cascade_delete', 'wal_engine', '2026-09-20T18:30:05+00:00')", (pid, TEXT))
    conn.commit()
    res = restore_tombstone(cur.lastrowid, user_id="alice", bank_id="work")
    assert res["status"] == "ok", res
    payload = fake.vector_store.points[pid]
    assert payload["created_at"] == "2026-09-20T18:30:05+00:00"
    assert (payload["data"], payload["user_id"], payload["bank_id"]) == (TEXT, "alice", "work")


def test_local_engine_mode_does_not_require_a_cloud_point(world, monkeypatch):
    fake, _ = world
    from ducky.tombstone import restore_tombstone
    pid = _memory(fake)
    tid = _delete(pid)
    monkeypatch.setenv("AIDUMEI_ENGINE_MODE", "local")
    import ducky.dual_index as di
    monkeypatch.setattr(di, "upsert_local",
                        lambda point_id, text, payload, client=None, *, enqueue_on_fail=True: True)
    res = restore_tombstone(tid, user_id="alice", bank_id="work")
    assert res["status"] == "ok"
    assert res["layers"]["vector"] == "skipped:engine_mode_local"
    assert res["layers"]["local_vector"] == "restored"
    assert pid not in fake.vector_store.points


def test_non_point_targets_keep_the_facts_and_fts_semantics(world):
    fake, _ = world
    from ducky.tombstone import restore_tombstone
    from ducky.utils import get_facts_conn
    conn = get_facts_conn()
    cur = conn.execute(
        "INSERT INTO tombstones (target_id, target_type, user_id, bank_id, content_snapshot, "
        "facts_snapshot, reason, actor, tombstoned_at) VALUES ('verbatim:12', 'memory', "
        "'alice', 'work', 'raw words', '', 'cascade_delete_verbatim', 'wal_engine', "
        "'2026-09-20T18:30:05+00:00')")
    conn.commit()
    res = restore_tombstone(cur.lastrowid, user_id="alice", bank_id="work")
    assert res["status"] == "ok" and res["layers"]["vector"] == "not_applicable"
    assert fake.vector_store.inserts == 0


def test_route_reports_partial_instead_of_noop(world):
    fake, _ = world
    import ducky.hot.crud as crud
    pid = _memory(fake)
    tid = _delete(pid)
    app = FastAPI()
    crud.register_crud_routes(app)
    client = TestClient(app)
    fake.embedding_model.broken = True
    r = client.post("/tombstone/restore", json={"tombstone_id": tid, "user_id": "alice",
                                                "bank_id": "work"})
    assert r.json()["status"] == "partial", r.json()
    fake.embedding_model.broken = False
    r = client.post("/tombstone/restore", json={"tombstone_id": tid, "user_id": "alice",
                                                "bank_id": "work"})
    body = r.json()
    assert body["status"] == "ok" and body["details"]["verification"]["vector_point"] is True
    listing = client.get("/tombstones", params={"user_id": "alice", "bank_id": "work"}).json()
    row = next(t for t in listing["results"] if t["tombstone_id"] == tid)
    assert row["target_type"] == "memory" and row["restored_at"]
