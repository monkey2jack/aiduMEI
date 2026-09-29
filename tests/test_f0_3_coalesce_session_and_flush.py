"""f0.3 C3 + C4: coalesce session axis and the manual flush endpoint.

C3 (S-3): the production shell hook sends only `_origin_session_id`.  The
coalesce key used to read session_id/session/chat_id/conversation_id only,
so turns of different sessions were merged into one batch and the batch's
provenance was the first turn's.

C4 (S-4): POST /add/coalesce/flush ran layer1 without the batch's infer flag,
had no LLM-failure fallback, and answered 200 {"status": "ok"} even when a
batch failed after its buffer had already been drained.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ducky.speed import coalesce


@pytest.fixture
def queue(monkeypatch):
    monkeypatch.setattr(coalesce, "_coalesce_buf", {})
    monkeypatch.setattr(coalesce, "load_speed_cfg", lambda: {
        "coalesce_max_parts": 100, "coalesce_max_chars": 100_000,
    })
    monkeypatch.setattr(coalesce.time, "time", lambda: 1_700_000_040.0)
    monkeypatch.setattr(coalesce, "record_coalesce_enqueue", lambda *a, **kw: None)
    monkeypatch.setattr(coalesce, "_record_wave_from_batch", lambda *a, **kw: None)
    return coalesce


def _hook_md(session: str, turn: int, agent: str = "hermes") -> dict:
    # Exactly what integrations/aidumem-ingest.sh sends: no `session_id` key.
    return {"_origin_session_id": session, "_origin_agent": agent, "_origin_turn": turn}


# ── C3 ──────────────────────────────────────────────────────────────────

def test_origin_session_only_turns_of_two_sessions_never_share_a_batch(queue):
    queue.coalesce_enqueue("alice", "turn from session one", _hook_md("s-1", 1),
                           bank_id="work", job_id="j1")
    queue.coalesce_enqueue("alice", "turn from session two", _hook_md("s-2", 1),
                           bank_id="work", job_id="j2")
    batches = queue.coalesce_flush_due(force=True)
    assert len(batches) == 2, "turns of different sessions were merged into one batch"
    by_session = {b["metadata"]["_origin_session_id"]: b for b in batches}
    assert set(by_session) == {"s-1", "s-2"}
    one, two = by_session["s-1"], by_session["s-2"]
    assert "session one" in str(one["messages"]) and "session two" not in str(one["messages"])
    assert "session two" in str(two["messages"]) and "session one" not in str(two["messages"])
    assert one["job_ids"] == ["j1"] and two["job_ids"] == ["j2"]


def test_negative_control_same_origin_session_still_coalesces(queue):
    """Discriminating power: the queue must still merge one session's turns."""
    queue.coalesce_enqueue("alice", "first turn", _hook_md("s-1", 1), bank_id="work")
    queue.coalesce_enqueue("alice", "second turn", _hook_md("s-1", 2), bank_id="work")
    batches = queue.coalesce_flush_due(force=True)
    assert len(batches) == 1 and batches[0]["count"] == 2
    assert batches[0]["metadata"]["_origin_session_id"] == "s-1"


def test_legacy_session_id_axis_is_kept_next_to_the_origin_axis(queue):
    """Same `_origin_session_id`, different legacy `session_id`: still apart."""
    queue.coalesce_enqueue("alice", "a", {"session_id": "chat-a", **_hook_md("s-1", 1)},
                           bank_id="work")
    queue.coalesce_enqueue("alice", "b", {"session_id": "chat-b", **_hook_md("s-1", 2)},
                           bank_id="work")
    assert len(queue.coalesce_flush_due(force=True)) == 2


def test_origin_agent_is_an_axis_too(queue):
    queue.coalesce_enqueue("alice", "a", _hook_md("s-1", 1, agent="hermes"), bank_id="work")
    queue.coalesce_enqueue("alice", "b", _hook_md("s-1", 2, agent="cursor"), bank_id="work")
    batches = queue.coalesce_flush_due(force=True)
    assert sorted(b["metadata"]["_origin_agent"] for b in batches) == ["cursor", "hermes"]


# ── route rig for C3 (end to end) and C4 ────────────────────────────────

class LLMError(RuntimeError):
    """Same type *name* the pipeline recognizes as an LLM-leg failure."""


class _Mem:
    """Signature-aligned with mem0.Memory.add / get_all / search."""

    def __init__(self):
        self.adds = []
        self.fail_direct = False

    def add(self, messages, *, user_id=None, agent_id=None, run_id=None, metadata=None,
            timestamp=None, expiration_date=None, infer=True, memory_type=None, prompt=None):
        if self.fail_direct:
            raise RuntimeError("vector store down (simulated)")
        self.adds.append({"messages": messages, "user_id": user_id,
                          "metadata": metadata, "infer": infer})
        return {"results": [{"id": f"m-{len(self.adds)}", "memory": str(messages),
                             "event": "ADD"}]}

    def get_all(self, *, filters=None, top_k=20, show_expired=False, **kwargs):
        return {"results": []}

    def search(self, query, *, top_k=20, filters=None, **kwargs):
        return {"results": []}


@pytest.fixture
def rig(monkeypatch, tmp_path):
    import ducky.add_speed as speed
    import ducky.hot.add as hot_add
    import ducky.mem0_runtime as runtime
    import ducky.utils as utils

    monkeypatch.setattr(utils, "FACTS_DB", str(tmp_path / "facts.db"))
    monkeypatch.setattr(utils, "TEXT_FTS_DB", str(tmp_path / "text_fts.db"))
    monkeypatch.setenv("AIDUMEI_ENGINE_MODE", "cloud")   # no local leg in this rig
    monkeypatch.setattr(coalesce, "_coalesce_buf", {})
    monkeypatch.setattr(coalesce, "_coalesce_flush_cb", None)
    monkeypatch.setattr(coalesce, "ensure_coalesce_worker", lambda: None)
    monkeypatch.setattr(speed, "ensure_coalesce_worker", lambda: None)
    mem = _Mem()
    monkeypatch.setattr(runtime, "get_memory", lambda: mem)
    monkeypatch.setattr(hot_add, "get_memory", lambda: mem)
    monkeypatch.setattr(hot_add, "patch_llm_for_speed", lambda m: None, raising=False)
    import ducky.gear as gear
    gear.reset_gear_for_tests()
    monkeypatch.setattr(gear, "should_try_llm", lambda *, now=None: True)
    calls = []

    def layer1(memory, messages_json, user_id, metadata, bank_id="default", infer=True):
        calls.append({"infer": infer, "bank_id": bank_id, "metadata": dict(metadata or {})})
        if rig_state["layer1_error"] is not None:
            raise rig_state["layer1_error"]
        return {"status": "ok", "action": "indexed", "details": {}}

    rig_state = {"layer1_error": None}
    monkeypatch.setattr(hot_add, "lazy_import_layer1", lambda: layer1)
    app = FastAPI()
    hot_add.register_add_routes(app)
    yield TestClient(app), mem, calls, rig_state
    gear.reset_gear_for_tests()   # an LLMError case records a failure on the gear


def _buffer(client, text, *, infer=True, session="s-1", user="alice"):
    r = client.post("/add", json={
        "messages": text, "user_id": user, "bank_id": "work",
        "async_mode": True, "infer": infer,
        "metadata": {**_hook_md(session, 1), "no_fastpath": True},
    })
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "coalesce_buffered", r.json()
    return r.json()


def test_hook_shaped_add_requests_of_two_sessions_buffer_separately(rig):
    client, _, _, _ = rig
    _buffer(client, "hook turn of session one", session="s-1")
    _buffer(client, "hook turn of session two", session="s-2")
    status = client.get("/add/coalesce", params={"user_id": "alice"}).json()
    assert status["buffer_count"] == 2, status


@pytest.mark.parametrize("infer", [False, True])
def test_manual_flush_keeps_the_batch_infer_flag(rig, infer):
    client, _, calls, _ = rig
    _buffer(client, "short turn waiting for its batch", infer=infer)
    r = client.post("/add/coalesce/flush", params={"user_id": "alice", "bank_id": "work"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok" and r.json()["n"] == 1
    assert [c["infer"] for c in calls] == [infer], (
        "manual flush re-inferred (or dropped) the batch's own infer flag")
    assert calls[0]["bank_id"] == "work"


def test_manual_flush_falls_back_to_direct_write_on_llm_error(rig):
    client, mem, calls, state = rig
    state["layer1_error"] = LLMError("llm gateway timeout (simulated)")
    queued = _buffer(client, "turn whose distillation will fail")
    r = client.post("/add/coalesce/flush", params={"user_id": "alice", "bank_id": "work"})
    assert r.status_code == 200, r.text
    entry, = r.json()["flushed"]
    assert entry["ok"] is True and entry["distillation"] == "skipped_llm_error", entry
    assert len(mem.adds) == 1 and mem.adds[0]["infer"] is False, (
        "LLM failure must fall back to the deterministic direct write")
    job = client.get(f"/add/job/{queued['job_id']}",
                     params={"user_id": "alice", "bank_id": "work"}).json()["job"]
    assert job["status"] == "done"


def test_manual_flush_reports_failure_instead_of_ok(rig):
    client, mem, _, state = rig
    state["layer1_error"] = RuntimeError("layer1 exploded (simulated, not an LLM error)")
    mem.fail_direct = True
    queued = _buffer(client, "turn that cannot be stored")
    r = client.post("/add/coalesce/flush", params={"user_id": "alice", "bank_id": "work"})
    assert r.status_code == 500, r.text
    body = r.json()
    assert body["status"] == "failed" and body["failed"] == 1
    assert body["flushed"][0]["ok"] is False and body["flushed"][0]["error"]
    job = client.get(f"/add/job/{queued['job_id']}",
                     params={"user_id": "alice", "bank_id": "work"}).json()["job"]
    assert job["status"] == "error"


def test_manual_flush_partial_is_207(rig, monkeypatch):
    client, mem, _, state = rig
    _buffer(client, "session one turn", session="s-1")
    _buffer(client, "session two turn", session="s-2")
    seen = {"n": 0}

    def flaky(memory, messages_json, user_id, metadata, bank_id="default", infer=True):
        seen["n"] += 1
        if "session two" in str(messages_json):
            raise RuntimeError("second batch broken (simulated)")
        return {"status": "ok", "action": "indexed", "details": {}}

    import ducky.hot.add as hot_add
    monkeypatch.setattr(hot_add, "lazy_import_layer1", lambda: flaky)
    mem.fail_direct = True
    r = client.post("/add/coalesce/flush", params={"user_id": "alice", "bank_id": "work"})
    assert r.status_code == 207, r.text
    body = r.json()
    assert body["status"] == "partial" and body["n"] == 2 and body["failed"] == 1


def test_manual_flush_without_executor_does_not_drain_the_queue(monkeypatch, queue):
    import ducky.hot.add as hot_add
    monkeypatch.setattr(coalesce, "_coalesce_flush_cb", None)
    queue.coalesce_enqueue("alice", "buffered turn", _hook_md("s-1", 1), bank_id="work")
    app = FastAPI()
    hot_add.register_add_routes(app)
    r = TestClient(app).post("/add/coalesce/flush", params={"user_id": "alice"})
    assert r.status_code == 503 and r.json()["status"] == "unavailable"
    assert len(queue._coalesce_buf) == 1, "queue drained with nobody to run the batch"
