"""Business arguments, cancellation and long-template safety regressions."""
from __future__ import annotations

import asyncio
import json

import pytest

from ducky.loop_guard import LoopGuard, fingerprint


def test_trace_variation_cannot_reset_failure_window():
    calls = []

    def read(query, request_id="", trace_id=""):
        calls.append(query)
        return {"error": "unavailable"}

    wrapped = LoopGuard().wrap(read)
    responses = [wrapped("same-query", request_id=str(i), trace_id=str(i)) for i in range(6)]
    assert len(calls) == 5
    assert json.loads(responses[-1])["error"] == "circuit_open"


def test_business_pagination_and_nested_identifiers_are_distinct():
    base = {"page": 1, "timestamp": 10, "payload": {"request_id": "first"}}
    for other in (
        {**base, "page": 2},
        {**base, "timestamp": 11},
        {**base, "payload": {"request_id": "second"}},
    ):
        assert fingerprint("read", base) != fingerprint("read", other)
    assert fingerprint("read", {**base, "trace_id": "a"}) == fingerprint("read", {**base, "trace_id": "b"})


@pytest.mark.parametrize("probe", [False, True])
def test_async_cancellation_is_not_failure_and_probe_can_be_retried(probe):
    async def scenario():
        now = [100.0]
        guard = LoopGuard(clock=lambda: now[0])
        mode = ["fail"]

        async def read():
            if mode[0] == "cancel":
                raise asyncio.CancelledError()
            return {"error": "unavailable"} if mode[0] == "fail" else {"status": "ok"}

        wrapped = guard.wrap(read)
        if probe:
            for _ in range(5):
                await wrapped()
            now[0] += 30
        mode[0] = "cancel"
        with pytest.raises(asyncio.CancelledError):
            await wrapped()
        state = next(iter(guard._states.values()))
        assert len(state.failures) == (5 if probe else 0)
        assert not state.probing
        mode[0] = "success"
        assert await wrapped() == {"status": "ok"}
        assert not state.failures
    asyncio.run(scenario())


def test_control_exception_keeps_native_semantics():
    guard = LoopGuard()

    def read():
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        guard.wrap(read)()
    assert not next(iter(guard._states.values())).failures


def test_long_shared_template_keeps_distinct_tail_and_merges_identical_text():
    from ducky.layer1_selfcheck import DEDUP_THRESHOLD, _cluster_by_similarity
    prefix = "This record contains the shared contract template. " * 15
    distinct = [{"id": str(i), "memory": prefix + f"Final agreed budget is {i + 10}."} for i in range(3)]
    assert len(_cluster_by_similarity(distinct, DEDUP_THRESHOLD)) == 3
    duplicates = [{"id": str(i), "memory": prefix + "Final agreed budget is 10."} for i in range(3)]
    assert len(_cluster_by_similarity(duplicates, DEDUP_THRESHOLD)) == 1


@pytest.mark.parametrize("silent", [False, True])
def test_snapshot_failure_blocks_automated_deletion(monkeypatch, silent):
    import ducky.layer1_selfcheck as layer
    import ducky.tombstone as tombstone
    import ducky.wal_engine as wal
    records = [{"id": str(i), "memory": "The agreed budget is 100 and the deadline is tomorrow.",
                "created_at": str(i), "metadata": {"source": "manual", "bank_id": "work"}} for i in range(3)]
    monkeypatch.setenv("AIDUMEI_AUTO_MERGE", "on")
    monkeypatch.setattr(layer, "get_all_memories", lambda *a, **k: {"results": records})

    def unavailable(*args, **kwargs):
        if silent:
            return None
        raise OSError("snapshot unavailable")

    calls = []
    monkeypatch.setattr(tombstone, "snapshot_before_delete", unavailable)
    monkeypatch.setattr(wal, "cascade_delete_memory", lambda *a, **k: calls.append(a))
    assert layer.auto_merge_similar(object(), "owner", bank_id="work")["deleted"] == 0
    assert not calls


def test_async_half_open_probe_timeout_releases_probe():
    async def scenario():
        now = [100.0]
        mode = ["fail"]
        guard = LoopGuard(clock=lambda: now[0], probe_timeout_s=0.01)

        async def read():
            if mode[0] == "slow":
                await asyncio.sleep(60)
            return {"error": "unavailable"} if mode[0] != "success" else {"status": "ok"}

        wrapped = guard.wrap(read)
        for _ in range(5):
            await wrapped()
        now[0] += 30
        mode[0] = "slow"
        with pytest.raises(RuntimeError, match="TimeoutError"):
            await wrapped()
        state = next(iter(guard._states.values()))
        assert not state.probing
        assert state.open_until == now[0] + 30
        now[0] += 30
        mode[0] = "success"
        assert await wrapped() == {"status": "ok"}
    asyncio.run(scenario())


@pytest.mark.parametrize("duplicates", [False, True])
def test_merge_real_enumeration_snapshot_and_delete_chain(monkeypatch, tmp_path, duplicates):
    """Only the external vector backend is an adapter; product flow and SQLite are real."""
    import sqlite3
    import uuid
    import ducky.layer1_selfcheck as layer
    import ducky.mem0_runtime as runtime
    import ducky.utils as utils
    from ducky.schema_bootstrap import ensure_core_schema

    monkeypatch.setattr(utils, "FACTS_DB", str(tmp_path / "facts.db"))
    monkeypatch.setattr(utils, "TEXT_FTS_DB", str(tmp_path / "text.db"))
    ensure_core_schema(force=True)
    conn = utils.get_text_conn()
    conn.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, content TEXT, user_id TEXT, bank_id TEXT)")
    conn.commit()
    prefix = "Shared contract template with standard terms. " * 12
    rows = []
    for bank in ("work", "home"):
        for i in range(3):
            text = prefix + ("Agreed budget: 10." if duplicates else f"Agreed budget: {i + 10}.")
            rows.append({"id": str(uuid.uuid4()), "memory": text, "created_at": str(i),
                         "user_id": "merge-owner", "metadata": {"source": "manual", "user_id": "merge-owner", "bank_id": bank}})

    class Memory:
        def __init__(self):
            self.items = {r["id"]: r for r in rows}
            self.calls = []
            self.vector_store = self
            from types import SimpleNamespace
            self.client = SimpleNamespace(get_collections=lambda: SimpleNamespace(collections=[]))

        def get_all(self, filters=None, top_k=None):
            self.calls.append((dict(filters), top_k))
            # Simulate an older backend ignoring the bank filter, so re-screening is tested.
            return {"results": list(self.items.values())}

        def get(self, vector_id):
            r = self.items.get(vector_id)
            return {"payload": {"data": r["memory"], **r["metadata"]}} if r else None

        def delete(self, mid):
            # This runs inside the real cascade. Snapshot must already be durable.
            count = utils.get_facts_conn().execute("SELECT count(*) FROM tombstones WHERE target_id=? AND content_snapshot=?", (mid, self.items[mid]["memory"])).fetchone()[0]
            assert count >= 1
            del self.items[mid]

    memory = Memory()
    monkeypatch.setattr(runtime, "get_memory", lambda: memory)
    monkeypatch.setenv("AIDUMEI_AUTO_MERGE", "on")
    before_home = {r["id"] for r in rows if r["metadata"]["bank_id"] == "home"}
    outcome = layer.auto_merge_similar(memory, "merge-owner", bank_id="work")
    assert memory.calls and all(call[0] == {"user_id": "merge-owner", "bank_id": "work"} for call in memory.calls)
    assert before_home <= set(memory.items)
    expected = 2 if duplicates else 0
    assert outcome["deleted"] == expected, outcome
    assert len(memory.items) == 6 - expected
    db = sqlite3.connect(utils.FACTS_DB)
    if expected:
        snapshots = db.execute("SELECT target_id, content_snapshot, user_id, bank_id FROM tombstones").fetchall()
        assert len({row[0] for row in snapshots}) == expected
        assert all(row[2:] == ("merge-owner", "work") for row in snapshots)
    db.close()
