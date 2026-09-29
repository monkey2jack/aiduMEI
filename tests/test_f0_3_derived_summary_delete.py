"""f0.3 C1 (S-1) + C9: deleting a source also deletes the session summaries
derived from it; delete_all reports how much it could not clear.

Defect: session summaries are derived records (fallback mode copies up to 120
characters of the two longest source turns verbatim), but their metadata only
carried a fingerprint and single deletes cascaded by exact content hash only
-- deleting a source left its summary behind (right to delete broken).
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

    def insert(self, vectors, payloads=None, ids=None):
        for i, pid in enumerate(ids or []):
            self.points[str(pid)] = dict((payloads or [{}])[i])

    def get(self, vector_id):
        p = self.points.get(str(vector_id))
        return _Point(str(vector_id), dict(p)) if p is not None else None

    def delete(self, vector_id):
        self.points.pop(str(vector_id), None)


class _Embedder:
    def embed(self, text, memory_action=None):
        return [0.1, 0.2, 0.3]


class FakeMem0:
    """mem0.Memory surface used by the delete chain (get_all/delete shapes)."""

    def __init__(self):
        self.vector_store = _Store()
        self.embedding_model = _Embedder()
        self.deleted: list = []

    def get_all(self, *, filters=None, top_k=20, show_expired=False, **kwargs):
        out = []
        for pid, p in self.vector_store.points.items():
            if any(p.get(k) != v for k, v in (filters or {}).items()):
                continue
            item = {"id": pid, "memory": p.get("data", ""), "hash": p.get("hash"),
                    "created_at": p.get("created_at"), "updated_at": p.get("updated_at")}
            item.update({k: p[k] for k in _PROMOTED if k in p})
            extra = {k: v for k, v in p.items() if k not in _CORE}
            if extra:
                item["metadata"] = extra
            out.append(item)
        return {"results": out[:top_k]}

    def delete(self, memory_id):
        if str(memory_id) not in self.vector_store.points:
            raise ValueError(f"Memory with id {memory_id} not found")
        self.deleted.append(str(memory_id))
        self.vector_store.delete(memory_id)
        return {"message": "Memory deleted successfully!"}


@pytest.fixture
def world(tmp_path, monkeypatch):
    import ducky.checkpoint as checkpoint
    import ducky.mem0_runtime as runtime
    import ducky.memory_types as mt
    import ducky.utils as utils
    from ducky.dual_index import ensure_pending_schema
    from ducky.schema_bootstrap import ensure_core_schema
    from ducky.text_fts import _init_text_fts
    from ducky.tombstone import ensure_tombstone_schema
    from ducky.verbatim_vault import ensure_verbatim_schema

    assert "aidumei_test_data_" in __import__("os").environ.get("AIDUMEM_DATA_DIR", "")
    facts_db = str(tmp_path / "facts.db")
    monkeypatch.setattr(utils, "FACTS_DB", facts_db)
    monkeypatch.setattr(utils, "TEXT_FTS_DB", str(tmp_path / "text_fts.db"))
    monkeypatch.setattr(mt, "_checked", False)
    monkeypatch.setattr(checkpoint, "_table_checked", False)
    monkeypatch.setenv("AIDUMEI_ENGINE_MODE", "cloud")
    ensure_core_schema(force=True)
    _init_text_fts()
    ensure_verbatim_schema()
    ensure_tombstone_schema()
    ensure_pending_schema()
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


def _turns(user, bank, session, texts):
    """Store verbatim turns the way /add does; return their verbatim:<id> refs."""
    from ducky.utils import get_facts_conn
    from ducky.verbatim_vault import store_verbatim
    for text in texts:
        store_verbatim(user, [{"role": "user", "content": text}], {"session_id": session},
                       bank_id=bank)
    rows = get_facts_conn().execute(
        "SELECT id FROM verbatim_turns WHERE user_id=? AND bank_id=? AND session_id=? "
        "ORDER BY id", (user, bank, session)).fetchall()
    return [f"verbatim:{r[0]}" for r in rows]


def _summary(fake, user, bank, session, text, refs, *, mode="llm", legacy=False):
    """All replicas a summary write creates: vector point, FTS row, verbatim
    replica (session distill:<sid>) and -- f0.3 -- the source ledger."""
    from ducky.session_distill import record_summary_sources
    from ducky.text_fts import _index_memory
    from ducky.verbatim_vault import store_verbatim
    pid = str(uuid.uuid4())
    md = {"kind": "session_distill", "lane": "distill", "_origin_agent": "session-distill",
          "_origin_session_id": session, "distill_mode": mode, "bank_id": bank}
    if not legacy:
        md["distill_source_refs"] = list(refs)
    fake.vector_store.insert([[0.1]], payloads=[{
        "data": text, "hash": hashlib.md5(text.encode()).hexdigest(),
        "created_at": "2026-09-29T00:00:00+00:00", "user_id": user, **md}], ids=[pid])
    _index_memory(pid, text, user_id=user, bank_id=bank)
    store_verbatim(user, text, {**md, "session_id": f"distill:{session}"}, bank_id=bank)
    if not legacy:
        assert record_summary_sources(user, bank, md, text) == len(refs)
    return pid


def _fts_ids(user, bank):
    """Public ids of the FTS rows (named banks store a scoped internal key)."""
    from ducky.bank_contract import make_scope, raw_storage_key
    from ducky.utils import get_text_conn
    scope = make_scope(user, bank)
    return {raw_storage_key(r[0], scope) for r in get_text_conn().execute(
        "SELECT id FROM memories WHERE user_id=? AND bank_id=?", (user, bank)).fetchall()}


SUMMARY = "Alice decided to ship the blue design after the long review"


def test_distill_metadata_names_its_sources(world, monkeypatch):
    import ducky.llm_client as llm
    import ducky.session_distill as distill
    monkeypatch.setattr(llm, "call_llm", lambda *a, **kw: None)     # fallback mode
    refs = _turns("alice", "work", "s-1", ["turn one text", "turn two text", "turn three"])
    out = distill.distill_session("s-1", user_id="alice", bank_id="work")
    assert out["status"] == "ok"
    assert out["metadata"]["distill_source_refs"] == refs
    assert out["source_refs"] == refs


def test_deleting_a_source_turn_cascades_to_its_summary(world):
    fake, q = world
    from ducky.wal_engine import cascade_delete_memory
    refs = _turns("alice", "work", "s-1", ["first turn words", "second turn words",
                                           "third turn words"])
    pid = _summary(fake, "alice", "work", "s-1", SUMMARY, refs)
    assert pid in fake.vector_store.points and pid in _fts_ids("alice", "work")

    out = cascade_delete_memory(refs[1], user_id="alice", bank_id="work")
    assert out["status"] == "committed", out
    derived = out["details"]["derived_summaries"]
    assert [d["id"] for d in derived] == [pid] and derived[0]["status"] == "committed"
    assert pid not in fake.vector_store.points and fake.deleted == [pid]
    assert pid not in _fts_ids("alice", "work")
    assert q("SELECT 1 FROM verbatim_turns WHERE content=?", (SUMMARY,)) == []
    assert q("SELECT 1 FROM distill_sources WHERE user_id='alice'") == []
    tomb = q("SELECT reason, actor, vector_snapshot FROM tombstones WHERE target_id=?", (pid,))
    assert tomb and tomb[0]["reason"] == "cascade_delete_derived", tomb
    assert tomb[0]["actor"] == f"derived_of:{refs[1]}"
    assert tomb[0]["vector_snapshot"], "summary tombstone lost its vector metadata"


def test_negative_control_unrelated_delete_keeps_the_summary(world):
    fake, _ = world
    from ducky.wal_engine import cascade_delete_memory
    refs = _turns("alice", "work", "s-1", ["alpha words", "beta words", "gamma words"])
    other = _turns("alice", "work", "s-2", ["unrelated session words"])
    pid = _summary(fake, "alice", "work", "s-1", SUMMARY, refs)
    out = cascade_delete_memory(other[0], user_id="alice", bank_id="work")
    assert out["status"] == "committed"
    assert "derived_summaries" not in out["details"]
    assert pid in fake.vector_store.points and fake.deleted == []


def test_mem0_delete_with_identical_source_content_cascades(world):
    """A direct (infer=False) memory equal to a turn takes that turn with it,
    so the summary built from the turn must go too."""
    fake, _ = world
    from ducky.text_fts import _index_memory
    from ducky.wal_engine import cascade_delete_memory
    refs = _turns("alice", "work", "s-1", ["the secret number is 42", "turn b", "turn c"])
    pid = _summary(fake, "alice", "work", "s-1", SUMMARY, refs)
    mid = str(uuid.uuid4())
    fake.vector_store.insert([[0.1]], payloads=[{"data": "the secret number is 42",
                                                 "user_id": "alice", "bank_id": "work"}],
                             ids=[mid])
    _index_memory(mid, "the secret number is 42", user_id="alice", bank_id="work")
    out = cascade_delete_memory(mid, user_id="alice", bank_id="work")
    assert out["status"] == "committed", out
    assert out["details"]["verbatim"] == 1
    assert [d["id"] for d in out["details"]["derived_summaries"]] == [pid]
    assert pid not in fake.vector_store.points


def test_other_scopes_are_never_touched(world):
    fake, _ = world
    from ducky.wal_engine import cascade_delete_memory
    refs = _turns("alice", "work", "s-1", ["one", "two", "three"])
    mine = _summary(fake, "alice", "work", "s-1", SUMMARY, refs)
    # A summary in another bank that (maliciously or by mistake) names the same refs.
    theirs = _summary(fake, "alice", "home", "s-1", "home summary", refs)
    bobs = _summary(fake, "bob", "work", "s-1", "bob summary", refs)
    cascade_delete_memory(refs[0], user_id="alice", bank_id="work")
    assert mine not in fake.vector_store.points
    assert theirs in fake.vector_store.points and bobs in fake.vector_store.points


def test_legacy_summaries_fallback_is_cascaded_llm_is_only_reported(world):
    fake, _ = world
    from ducky.wal_engine import cascade_delete_memory
    long_turn = "a long turn the fallback summary quoted almost verbatim, about the move"
    refs = _turns("alice", "work", "s-1", [long_turn, "short b", "short c"])
    fallback = _summary(fake, "alice", "work", "s-1",
                        long_turn[:120] + "; short b", refs, mode="fallback", legacy=True)
    llm = _summary(fake, "alice", "work", "s-1", "an llm paraphrase", refs, legacy=True)
    out = cascade_delete_memory(refs[0], user_id="alice", bank_id="work")
    assert [d["id"] for d in out["details"]["derived_summaries"]] == [fallback]
    assert out["details"]["derived_unverified"] == [llm]
    assert llm in fake.vector_store.points, "an unattributable summary must not be deleted"


def test_deferred_lite_summary_in_the_pending_ledger_is_dropped(world):
    fake, q = world
    import json
    from ducky.dual_index import enqueue_cloud_add
    from ducky.wal_engine import cascade_delete_memory
    refs = _turns("alice", "work", "s-1", ["p one", "p two", "p three"])
    md = {"kind": "session_distill", "lane": "distill", "_origin_agent": "session-distill",
          "_origin_session_id": "s-1", "distill_source_refs": refs, "bank_id": "work"}
    enqueue_cloud_add({"messages": "deferred summary", "metadata": md}, "alice", "work")
    enqueue_cloud_add({"messages": "ordinary deferred write",
                       "metadata": {"bank_id": "work"}}, "alice", "work")
    out = cascade_delete_memory(refs[2], user_id="alice", bank_id="work")
    assert out["details"]["derived_pending_deleted"] == 1
    left = [json.loads(r["payload"])["messages"] for r in
            q("SELECT payload FROM pending_embeddings WHERE side='cloud'")]
    assert left == ["ordinary deferred write"]


def test_local_mode_summary_written_by_add_is_cascaded_through_the_ledger(world, monkeypatch):
    """End to end through /add (local gear: no vector point, no metadata store):
    the ledger written by the route is the only link to the summary."""
    fake, q = world
    import ducky.hot.add as hot_add
    import ducky.llm_client as llm
    import ducky.session_distill as distill
    from ducky.wal_engine import cascade_delete_memory
    monkeypatch.setenv("AIDUMEI_ENGINE_MODE", "local")
    monkeypatch.setattr(hot_add, "get_memory", lambda: fake)
    monkeypatch.setattr(hot_add, "patch_llm_for_speed", lambda m: None, raising=False)
    monkeypatch.setattr(llm, "call_llm", lambda *a, **kw: "Alice picked blue")
    refs = _turns("alice", "work", "s-9", ["local one", "local two", "local three"])
    out = distill.distill_session("s-9", user_id="alice", bank_id="work")
    app = FastAPI()
    hot_add.register_add_routes(app)
    r = TestClient(app).post("/add", json={
        "messages": out["summary"], "user_id": "alice", "bank_id": "work",
        "metadata": {**out["metadata"], "session_id": "distill:s-9"}})
    assert r.status_code == 200 and r.json()["action"] == "local_only", r.text
    assert len(q("SELECT 1 FROM distill_sources WHERE user_id='alice'")) == 3
    replica = q("SELECT id FROM verbatim_turns WHERE session_id='distill:s-9'")
    assert replica, "precondition: the summary's raw replica exists"

    res = cascade_delete_memory(refs[1], user_id="alice", bank_id="work")
    assert res["status"] == "committed", res
    assert [d["id"] for d in res["details"]["derived_summaries"]] == [
        f"verbatim:{replica[0]['id']}"]
    assert q("SELECT 1 FROM verbatim_turns WHERE session_id='distill:s-9'") == []
    assert q("SELECT reason FROM tombstones WHERE target_id=?",
             (f"verbatim:{replica[0]['id']}",))[0]["reason"] == "cascade_delete_derived"


def test_ordinary_write_records_no_sources(world):
    from ducky.session_distill import record_summary_sources
    assert record_summary_sources("alice", "work", {"distill_source_refs": ["verbatim:1"]},
                                  "not a summary") == 0


def test_delete_all_clears_ledger_and_receipts_and_counts_what_it_left(world):
    fake, q = world
    import ducky.idempotency as idem
    from ducky.checkpoint import write_checkpoint
    from ducky.skill_crystallizer import init_crystallizer_schema
    from ducky.wal_engine import cascade_delete_all
    refs = _turns("alice", "work", "s-1", ["x one", "x two", "x three"])
    pid = _summary(fake, "alice", "work", "s-1", SUMMARY, refs)
    for key, user, bank in (("ka", "alice", "work"), ("kb", "bob", "default")):
        st = idem.claim(key, user, bank, {"k": key})
        idem.finalize(key, user, bank, {"status": "ok"}, claimed_at=idem.claim_token(st))
    write_checkpoint("sess-alice", {"cp_active_intent": "ship the design"},
                     user_id="alice", bank_id="work")
    write_checkpoint("sess-bob-1", {"cp_active_intent": "bob's own work"},
                     user_id="bob", bank_id="work")
    init_crystallizer_schema()
    from ducky.utils import get_facts_conn
    conn = get_facts_conn()
    for name in ("crystallized-a", "crystallized-b"):
        conn.execute("INSERT INTO skill_crystals (skill_name, trigger_rule, procedure) "
                     "VALUES (?, 'r', 'p')", (name,))
    conn.commit()

    out = cascade_delete_all("alice", bank_id="work", confirm=True)
    det = out["details"]
    assert pid in fake.deleted
    assert det["distill_sources_deleted"] == 3
    assert det["idempotency_keys_deleted"] == 1
    assert [r["user_id"] for r in q("SELECT user_id FROM idempotency_keys")] == ["bob"]

    nc = out["not_cleared"]
    assert all(isinstance(v, dict) and v["reason"] for v in nc.values())
    assert nc["checkpoints"]["rows_scope"] == "tenant"
    assert nc["checkpoints"]["rows"] == len(
        q("SELECT 1 FROM checkpoints WHERE user_id='alice' AND bank_id='work'")) > 0
    assert nc["skill_crystals"] == {**nc["skill_crystals"], "rows": 2, "rows_scope": "table"}
    assert nc["store:persona"]["rows_scope"] in ("absent", "table")


def test_single_delete_keeps_the_plain_exemption_mapping(world):
    """not_cleared of a single delete stays name -> reason (unchanged contract)."""
    from ducky.wal_engine import cascade_delete_memory, delete_chain_exemptions
    out = cascade_delete_memory("verbatim:999999", user_id="alice", bank_id="work")
    assert out["not_cleared"] == delete_chain_exemptions()
