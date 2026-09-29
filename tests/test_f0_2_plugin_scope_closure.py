"""f0.2 maintenance: the Hermes provider must preserve scope and lifecycle order."""
from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
import sys
import threading
import time
import types
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


_ROOT = Path(__file__).resolve().parents[1]
_PLUGIN = _ROOT / "integrations/hermes-plugin/aidumem/__init__.py"


@pytest.fixture()
def plugin(monkeypatch):
    """Load the distributed plugin without requiring a Hermes checkout."""
    agent = types.ModuleType("agent")
    agent.__path__ = []
    provider = types.ModuleType("agent.memory_provider")
    provider.MemoryProvider = type("MemoryProvider", (), {})
    tool_pkg = types.ModuleType("tools")
    tool_pkg.__path__ = []
    registry = types.ModuleType("tools.registry")
    registry.tool_error = lambda message: json.dumps({"error": message})
    for name, module in (("agent", agent), ("agent.memory_provider", provider),
                         ("tools", tool_pkg), ("tools.registry", registry)):
        monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location("_aidumem_scope_test_plugin", _PLUGIN)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _qs(path: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(path).query).items()}


def test_memory_mirror_and_all_reads_keep_both_scope_axes(plugin, monkeypatch):
    monkeypatch.setenv("AIDUMEI_BANK_ID", "work")
    calls = []
    p = plugin.AiduMemProvider({"user_id": "alice"})
    p._spawn = lambda fn, name: fn()

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs.get("body")))
        return {"status": "ok", "context": "alice-work-core"} if "inject" in path else {"status": "ok", "results": []}

    p._client.try_request = request
    p.initialize("session-1")
    p.prefetch("where is it?", session_id="session-1")
    p.on_memory_write("replace", "user", "Alice private profile")
    p.handle_tool_call("aidumem_search", {"query": "prior decision"})

    by_path = {urlsplit(path).path: (path, body) for _, path, body in calls}
    for endpoint in ("/session/start", "/api/core-memory/inject", "/facts/add"):
        scope = _qs(by_path[endpoint][0])
        assert scope["user_id"] == "alice"
        assert scope["bank_id"] == "work"
    assert by_path["/facts/add"][0].count("fact_key=hermes%2Fuser") == 1
    search_bodies = [body for _, path, body in calls if path == "/search"]
    assert len(search_bodies) == 2
    assert all(body["user_id"] == "alice" and body["bank_id"] == "work" for body in search_bodies)
    assert len({body["session_id"] for body in search_bodies}) == 1
    assert re.fullmatch(r"hermes_seg_[0-9a-f]{32}", search_bodies[0]["session_id"])


def test_two_bots_mirror_same_key_into_separate_facts(plugin, monkeypatch, tmp_path):
    """Exercise the actual /facts/add route and its scope-keyed upsert."""
    import ducky.utils as utils
    import ducky.hot.legacy_routes as legacy
    from ducky.schema_bootstrap import ensure_core_schema

    db = tmp_path / "facts.db"
    monkeypatch.setattr(utils, "FACTS_DB", str(db))
    monkeypatch.setattr(legacy, "_get_facts_conn", utils.get_facts_conn)
    ensure_core_schema(force=True)
    app = FastAPI()
    legacy.register_legacy_routes(app)
    client = TestClient(app)

    def bridge(method, path, **kwargs):
        result = client.request(method, path)
        assert result.status_code == 200, result.text
        return result.json()

    for uid, value in (("alice", "Alice profile"), ("bob", "Bob profile")):
        p = plugin.AiduMemProvider({"user_id": uid, "bank_id": "work"})
        p._spawn = lambda fn, name: fn()
        p._client.try_request = bridge
        p.on_memory_write("replace", "user", value)

    rows = client.get("/facts", params={"category": "user_profile", "bank_id": "work",
                                       "user_id": "alice"}).json()["facts"]
    assert [r["fact_value"] for r in rows if r["fact_key"] == "hermes/user"] == ["Alice profile"]
    rows = client.get("/facts", params={"category": "user_profile", "bank_id": "work",
                                       "user_id": "bob"}).json()["facts"]
    assert [r["fact_value"] for r in rows if r["fact_key"] == "hermes/user"] == ["Bob profile"]


def test_session_end_waits_for_durable_turn_before_distill(plugin):
    pending = threading.Event()
    release = threading.Event()
    order = []
    p = plugin.AiduMemProvider({"user_id": "alice", "bank_id": "work"})
    p._session_id = "session-1"

    def request(method, path, **kwargs):
        if path == "/add" and kwargs.get("body", {}).get("metadata", {}).get("source") == "hermes_turn":
            order.append("turn-start")
            pending.set()
            assert release.wait(3)
            order.append("turn-durable")
            assert kwargs["body"].get("async_mode") is not True
            assert kwargs["body"]["metadata"].get("force_sync") is True
            return {"status": "ok", "action": "direct"}
        if path.startswith("/session/end"):
            order.append("end")
            return {"status": "ok", "session_id": "session-1", "user_id": "alice", "bank_id": "work"}
        if path.startswith("/session/distill"):
            order.append("distill")
            return {"status": "skipped", "reason": "too_short", "source_count": 1}
        return {"status": "ok"}

    p._client.try_request = request
    p.sync_turn("One substantive user turn", "assistant reply", session_id="session-1")
    assert pending.wait(3)
    p.on_session_end([])
    release.set()
    for t in p._threads:
        t.join(timeout=3)
    assert order.index("turn-durable") < order.index("end") < order.index("distill")


def test_delayed_third_turn_produces_one_distill_after_commit(plugin):
    third_started = threading.Event()
    release_third = threading.Event()
    order = []
    p = plugin.AiduMemProvider({"user_id": "alice", "bank_id": "work"})
    p._session_id = "three-turns"

    def request(method, path, **kwargs):
        body = kwargs.get("body") or {}
        if path == "/add" and body.get("metadata", {}).get("source") == "hermes_turn":
            if "third" in body["messages"]:
                third_started.set()
                assert release_third.wait(3)
                order.append("third-committed")
            return {"status": "ok", "action": "direct"}
        if path.startswith("/session/end"):
            order.append("end")
            return {"status": "ok"}
        if path.startswith("/session/distill"):
            order.append("distill")
            return {"status": "ok", "summary": "Three turns mattered.",
                    "user_id": "alice", "bank_id": "work", "metadata": {}}
        if path == "/add" and body.get("messages") == "Three turns mattered.":
            order.append("summary-committed")
            assert body["metadata"]["force_sync"] is True
            assert body["metadata"]["session_id"].startswith("distill:hermes_seg_")
            return {"status": "ok", "action": "direct"}
        raise AssertionError(path)

    p._client.try_request = request
    p.sync_turn("first", "reply", session_id="three-turns")
    p.sync_turn("second", "reply", session_id="three-turns")
    p.sync_turn("third", "reply", session_id="three-turns")
    assert third_started.wait(3)
    p.on_session_end([])
    assert "end" not in order and "distill" not in order
    release_third.set()
    for t in p._threads:
        t.join(timeout=3)
    assert order == ["third-committed", "end", "distill", "summary-committed"]
    p.on_session_end([])
    assert order.count("distill") == 1


def test_unconfirmed_turn_retries_same_key_before_ending(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_TURN_RETRY_DELAYS", (0, 0))
    order = []
    keys = []
    source_ids = []
    p = plugin.AiduMemProvider({"user_id": "alice", "bank_id": "work"})
    p._session_id = "retry-turn"
    p._spawn = lambda fn, name: fn()

    def request(method, path, **kwargs):
        if path == "/add":
            key = kwargs["body"]["idempotency_key"]
            keys.append(key)
            source_ids.append(kwargs["body"]["metadata"]["session_id"])
            order.append("add")
            return None if len(keys) == 1 else {"status": "ok"}
        if path.startswith("/session/end"):
            order.append("end")
            return {"status": "ok"}
        if path.startswith("/session/distill"):
            order.append("distill")
            return {"status": "skipped", "reason": "too_short", "source_count": 1}
        raise AssertionError(path)

    p._client.try_request = request
    p.sync_turn("one", "reply", session_id="retry-turn")
    p.on_session_end([])
    assert len(keys) == 2 and keys[0] == keys[1]
    assert len(set(source_ids)) == 1
    assert order == ["add", "add", "end", "distill"]


def test_unconfirmed_turn_never_reports_a_false_short_session(plugin, monkeypatch, caplog):
    monkeypatch.setattr(plugin, "_TURN_RETRY_DELAYS", (0, 0))
    calls = []
    ready = False
    p = plugin.AiduMemProvider({"user_id": "alice", "bank_id": "work"})
    p._session_id = "uncertain-turn"
    p._spawn = lambda fn, name: fn()

    def request(method, path, **kwargs):
        calls.append(path)
        if path == "/add":
            return {"status": "ok"} if ready else None
        if path.startswith("/session/end"):
            return {"status": "ok"}
        if path.startswith("/session/distill"):
            return {"status": "skipped", "reason": "too_short", "source_count": 1}
        raise AssertionError(path)

    p._client.try_request = request
    p.sync_turn("one", "reply", session_id="uncertain-turn")
    p.on_session_end([])
    assert calls == ["/add", "/add", "/add"]
    assert "unconfirmed" in caplog.text
    assert "uncertain-turn" in p._pending_turns
    ready = True
    p.on_session_end([])
    assert any(path.startswith("/session/end") for path in calls)
    assert any(path.startswith("/session/distill") for path in calls)


def test_distill_store_retry_reuses_summary_and_key(plugin):
    p = plugin.AiduMemProvider({"user_id": "alice", "bank_id": "work"})
    p._session_id = "summary-retry"
    p._spawn = lambda fn, name: fn()
    paths = []
    bodies = []

    def request(method, path, **kwargs):
        paths.append(urlsplit(path).path)
        if path.startswith("/session/end"):
            return {"status": "ok"}
        if path.startswith("/session/distill"):
            return {"status": "ok", "summary": "A useful session insight.",
                    "user_id": "alice", "bank_id": "work", "metadata": {}}
        if path == "/add":
            bodies.append(kwargs["body"])
            return None if len(bodies) == 1 else {"status": "ok"}
        raise AssertionError(path)

    p._client.try_request = request
    p.on_session_end([])
    p.on_session_end([])
    p.on_session_end([])
    assert paths == ["/session/end", "/session/distill", "/add", "/add"]
    assert bodies[0] == bodies[1]
    assert bodies[0]["idempotency_key"]


def test_distill_and_background_write_timeouts_cover_server_llm_budget(plugin):
    p = plugin.AiduMemProvider({"user_id": "alice", "bank_id": "work"})
    p._session_id = "slow-llm"
    p._spawn = lambda fn, name: fn()
    timeouts = {}

    def request(method, path, **kwargs):
        endpoint = urlsplit(path).path
        timeouts.setdefault(endpoint, []).append(kwargs["timeout"])
        if endpoint == "/session/distill":
            return {"status": "ok", "summary": "LLM took 30 seconds.", "metadata": {}}
        return {"status": "ok"}

    p._client.try_request = request
    p.sync_turn("one", "reply", session_id="slow-llm")
    p.on_session_end([])
    assert timeouts["/add"][0] > 30  # synchronous turn extraction
    assert timeouts["/session/distill"][0] > 30
    assert timeouts["/add"][1] > 30  # synchronous summary store


def test_shutdown_waits_past_five_seconds_for_finalizer(plugin):
    p = plugin.AiduMemProvider({"user_id": "alice", "bank_id": "work"})
    p._session_id = "slow-finalizer"
    started = threading.Event()
    release = threading.Event()

    def request(method, path, **kwargs):
        if path.startswith("/session/end"):
            return {"status": "ok"}
        if path.startswith("/session/distill"):
            started.set()
            assert release.wait(8)
            return {"status": "skipped", "reason": "too_short", "source_count": 2}
        raise AssertionError(path)

    p._client.try_request = request
    p.on_session_end([])
    assert started.wait(1)
    timer = threading.Timer(5.2, release.set)
    timer.start()
    try:
        p.shutdown()
        assert "slow-finalizer" in p._completed_sessions
    finally:
        release.set()
        timer.join(timeout=1)
        for thread in p._threads:
            thread.join(timeout=1)


def test_shutdown_is_bounded_and_reports_unconfirmed_finalization(plugin, monkeypatch, caplog):
    monkeypatch.setattr(plugin, "_SHUTDOWN_GRACE_SECONDS", 0.06, raising=False)
    p = plugin.AiduMemProvider({"user_id": "alice", "bank_id": "work"})
    p._session_id = "stuck-finalizer"
    started = threading.Event()
    release = threading.Event()

    def request(method, path, **kwargs):
        if path.startswith("/session/end"):
            return {"status": "ok"}
        if path.startswith("/session/distill"):
            started.set()
            assert release.wait(8)
            return {"status": "skipped", "reason": "too_short", "source_count": 2}
        raise AssertionError(path)

    p._client.try_request = request
    p.on_session_end([])
    assert started.wait(1)
    started_at = time.monotonic()
    try:
        p.shutdown()
        assert time.monotonic() - started_at < 0.5
        assert "stuck-finalizer" in caplog.text
        assert "unconfirmed" in caplog.text
    finally:
        release.set()
        for thread in p._threads:
            thread.join(timeout=1)


def test_session_switch_rebinds_plugin_to_new_hermes_session(plugin):
    p = plugin.AiduMemProvider({"user_id": "alice", "bank_id": "work"})
    p._session_id = "old"
    p._spawn = lambda fn, name: fn()
    started = []

    def request(method, path, **kwargs):
        if path.startswith("/session/end"):
            return {"status": "ok"}
        if path.startswith("/session/distill"):
            return {"status": "skipped", "reason": "too_short", "source_count": 2}
        if path.startswith("/session/start"):
            started.append(_qs(path)["session_id"])
            return {"status": "ok", "session_id": "new"}
        raise AssertionError(path)

    p._client.try_request = request
    p.on_session_end([])
    p.on_session_switch("new", reset=True, reason="new_session")
    assert p._session_id == "new"
    assert started == ["new"]


def test_resumed_same_session_id_gets_new_end_and_distill(plugin):
    """A completed id can be resumed without inheriting its old end state."""
    p = plugin.AiduMemProvider({"user_id": "alice", "bank_id": "work"})
    p._spawn = lambda fn, name: fn()
    calls = []
    summaries = []

    def request(method, path, **kwargs):
        endpoint = urlsplit(path).path
        calls.append(endpoint)
        if endpoint == "/session/start":
            return {"status": "ok", "session_id": "resumed"}
        if endpoint == "/session/end":
            return {"status": "ok"}
        if endpoint == "/session/distill":
            return {"status": "ok", "summary": f"summary-{len(summaries) + 1}",
                    "metadata": {}}
        if endpoint == "/add":
            body = kwargs["body"]
            if body["messages"].startswith("summary-"):
                summaries.append(body)
            return {"status": "ok"}
        raise AssertionError(path)

    p._client.try_request = request
    p.initialize("resumed")
    p.sync_turn("first conversation turn", "reply", session_id="resumed")
    p.on_session_end([])
    p.on_session_switch("resumed", reason="resume")
    p.sync_turn("new conversation turn", "reply", session_id="resumed")
    p.on_session_end([])

    assert calls.count("/session/start") == 2
    assert calls.count("/session/end") == 2
    assert calls.count("/session/distill") == 2
    assert [body["messages"] for body in summaries] == ["summary-1", "summary-2"]
    assert summaries[0]["idempotency_key"] != summaries[1]["idempotency_key"]


@pytest.mark.parametrize("restart_provider", [False, True])
def test_resumed_sources_are_disjoint_in_real_collector(plugin, monkeypatch, tmp_path,
                                                        restart_provider):
    """Three old turns cannot turn a one-turn /resume into another summary."""
    import ducky.session_distill as distill
    import ducky.utils as utils

    db = tmp_path / "facts.db"
    monkeypatch.setattr(utils, "FACTS_DB", str(db))
    with sqlite3.connect(db) as conn:
        conn.execute("""CREATE TABLE verbatim_turns (
            id INTEGER PRIMARY KEY, user_id TEXT, bank_id TEXT, session_id TEXT,
            content TEXT, recorded_at TEXT, created_at TEXT)""")

    calls = []
    collected = []

    def request(method, path, **kwargs):
        endpoint = urlsplit(path).path
        params = _qs(path)
        calls.append((endpoint, params.get("session_id"), kwargs.get("body")))
        if endpoint == "/session/start":
            return {"status": "ok", "session_id": params["session_id"]}
        if endpoint == "/session/end":
            return {"status": "ok"}
        if endpoint == "/api/core-memory/inject":
            return {"status": "ok"}
        if endpoint == "/search":
            return {"status": "ok", "results": []}
        if endpoint == "/add":
            body = kwargs["body"]
            if body["metadata"].get("source") == "hermes_turn":
                with sqlite3.connect(db) as conn:
                    conn.execute(
                        "INSERT INTO verbatim_turns (user_id, bank_id, session_id, "
                        "content, recorded_at, created_at) VALUES (?,?,?,?,?,?)",
                        (body["user_id"], body["bank_id"],
                         body["metadata"]["session_id"], body["messages"],
                         "2026-09-29", "2026-09-29"),
                    )
            return {"status": "ok"}
        if endpoint == "/session/distill":
            rows = distill.collect_session_memories(
                params["session_id"], user_id=params["user_id"],
                bank_id=params["bank_id"],
            )
            collected.append((params["session_id"], [row["text"] for row in rows]))
            if len(rows) < distill.MIN_MEMORIES:
                return {"status": "skipped", "reason": "too_short",
                        "source_count": len(rows)}
            return {"status": "ok", "summary": "Old segment summary.", "metadata": {}}
        raise AssertionError(path)

    p = plugin.AiduMemProvider({"user_id": "alice", "bank_id": "work"})
    p._spawn = lambda fn, name: fn()
    p._client.try_request = request
    p.initialize("resumed")
    for index in range(3):
        p.sync_turn(f"old conversation turn {index}", "reply", session_id="resumed")
    p.prefetch("old memory query", session_id="resumed")
    p.on_session_end([])

    if restart_provider:
        p = plugin.AiduMemProvider({"user_id": "alice", "bank_id": "work"})
        p._spawn = lambda fn, name: fn()
        p._client.try_request = request
        p.initialize("resumed")
    else:
        p.on_session_switch("resumed", reason="resume")
    p.prefetch("new memory query", session_id="resumed")
    p.sync_turn("new conversation turn", "reply", session_id="resumed")
    p.on_session_end([])

    assert [len(rows) for _, rows in collected] == [3, 1]
    assert "old conversation" not in collected[1][1][0]
    assert collected[0][0] != collected[1][0]
    assert all(re.fullmatch(r"hermes_seg_[0-9a-f]{32}", source)
               for source, _ in collected)
    assert {sid for endpoint, sid, _ in calls if endpoint in
            {"/session/start", "/session/end"}} == {"resumed"}
    turn_sources = [body["metadata"]["session_id"] for endpoint, _, body in calls
                    if endpoint == "/add" and body["metadata"].get("source") == "hermes_turn"]
    assert turn_sources == [collected[0][0]] * 3 + [collected[1][0]]
    search_sources = [body["session_id"] for endpoint, _, body in calls
                      if endpoint == "/search"]
    assert search_sources == [collected[0][0], collected[1][0]]


def test_resumed_turn_waits_for_prior_finalizer(plugin):
    """An old finalizer must not end or distill the newly resumed turn."""
    p = plugin.AiduMemProvider({"user_id": "alice", "bank_id": "work"})
    p._session_id = "reused"
    old_distill_started = threading.Event()
    release_old = threading.Event()
    order = []

    def request(method, path, **kwargs):
        endpoint = urlsplit(path).path
        if endpoint == "/session/end":
            order.append("end")
            return {"status": "ok"}
        if endpoint == "/session/distill":
            order.append("distill")
            if order.count("distill") == 1:
                old_distill_started.set()
                assert release_old.wait(3)
            return {"status": "skipped", "reason": "too_short"}
        if endpoint == "/session/start":
            order.append("restart")
            return {"status": "ok", "session_id": "reused"}
        if endpoint == "/add":
            order.append("new-turn")
            return {"status": "ok"}
        raise AssertionError(path)

    p._client.try_request = request
    p.on_session_end([])
    assert old_distill_started.wait(1)
    p.sync_turn("new substantive conversation", "reply", session_id="reused")
    p.on_session_end([])
    assert order == ["end", "distill"]

    release_old.set()
    for thread in list(p._threads):
        thread.join(timeout=3)
    assert order == ["end", "distill", "restart", "new-turn", "end", "distill"]
    assert "reused" in p._completed_sessions


def test_provider_availability_requires_full_healthy_authorized_view(plugin):
    p = plugin.AiduMemProvider({"user_id": "alice"})
    for body in (
        {"status": "ok", "health_status": "fatal", "probes": {}},
        {"status": "ok", "health_status": "ok", "probes": {"_redacted": "authenticate"}},
        {"status": "error", "health_status": "ok", "probes": {}},
    ):
        p._client.try_request = lambda *a, **kw: body
        assert not p.is_available(), body
    p._client.try_request = lambda *a, **kw: {"status": "ok", "health_status": "ok", "probes": {"auth_ok": True}}
    assert p.is_available()


def test_turn_category_controls_coalesce_and_fts_label(plugin):
    from ducky.speed.coalesce import resolve_coalesce_profile

    p = plugin.AiduMemProvider({"user_id": "alice"})
    p._spawn = lambda fn, name: fn()
    bodies = []
    p._client.try_request = lambda method, path, **kw: bodies.append(kw.get("body")) or {"status": "ok"}
    p.sync_turn("Please fix the deployment command", "Done")
    md = bodies[0]["metadata"]
    assert md["category"] == "tech"
    assert resolve_coalesce_profile(md) == "tech"


def test_remember_tool_distinguishes_committed_queued_and_failed(plugin):
    p = plugin.AiduMemProvider({"user_id": "alice", "bank_id": "work"})
    responses = [
        {"status": "ok", "durable": True},
        {"status": "accepted", "durable": False},
        {"status": "error", "detail": "private server detail"},
        None,
    ]
    bodies = []

    def request(method, path, **kwargs):
        assert (method, path) == ("POST", "/add")
        bodies.append(kwargs["body"])
        return responses.pop(0)

    p._client.try_request = request
    results = [json.loads(p.handle_tool_call("aidumem_remember", {"content": "A decision"}))
               for _ in range(4)]
    assert results[0]["result"] == "Stored in aiduMEI."
    assert "not yet confirmed" in results[1]["result"]
    assert results[2]["error"] == results[3]["error"] == "aiduMEI write not confirmed"
    assert all(body["user_id"] == "alice" and body["bank_id"] == "work"
               for body in bodies)
