"""Committed eviction evidence and the memory/SQLite reinsert boundary."""
import hashlib
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest


@pytest.fixture
def workspace(tmp_path, monkeypatch, caplog):
    import ducky.pipeline.memory_workspace as w
    monkeypatch.setattr(w, "WORKSPACE_DB", str(tmp_path / "workspace.db"))
    monkeypatch.setattr(w, "_db_initialized", False)
    monkeypatch.setattr(w, "_workspace", {})
    monkeypatch.setattr(w, "_last_cleanup", time.time())
    monkeypatch.setenv("AIDUMEI_BUILD_SHA", "b" * 40)
    caplog.set_level("INFO", logger=w.logger.name)
    return w


def events(caplog):
    return [json.loads(r.getMessage().split("workspace_eviction_receipt ", 1)[1])
            for r in caplog.records if r.name == "aiduMEM.workspace"
            and r.getMessage().startswith("workspace_eviction_receipt ")]


def cold(w):
    w.ws_push("synthetic", "old-id", "private body never logged", metadata={"note": "private metadata"}, bank_id="work")
    data = w._workspace[w._scope_key("synthetic", "work")]["old-id"]
    data["last_accessed"] = time.time() - w.WORKSPACE_TTL_SECONDS - 30
    w._db_upsert("synthetic", "old-id", data, "work")
    w._last_cleanup = 0


@pytest.mark.parametrize("reason", ["ttl", "lru"])
def test_real_eviction_has_exact_source_scope_and_body_hash(workspace, monkeypatch, caplog, reason):
    w = workspace
    cold(w)
    if reason == "ttl":
        w._maybe_cleanup(time.time())
    else:
        monkeypatch.setattr(w, "WORKSPACE_CAPACITY", 1)
        w.ws_push("synthetic", "new-id", "replacement", bank_id="work")
    with sqlite3.connect(w.WORKSPACE_DB) as c:
        assert c.execute("SELECT count(*) FROM workspace WHERE memory_id='old-id'").fetchone()[0] == 0
    [event] = events(caplog)
    assert event["reason"] == reason and event["schema"] == 2
    assert event["sha"] == "b" * 40
    assert (event["user_id"], event["bank_id"], event["memory_id"]) == ("synthetic", "work", "old-id")
    assert event["body_sha256"] == hashlib.sha256(b"private body never logged").hexdigest()
    assert event["source_sha256"] == hashlib.sha256(__import__("pathlib").Path(w.__file__).read_bytes()).hexdigest()
    assert event["committed_epoch"] >= event["observed_epoch"]
    assert "private body never logged" not in caplog.text and "private metadata" not in caplog.text
    if reason == "lru":
        assert event["ordered_ids_before"] == ["old-id", "new-id"]
        assert event["ordered_ids_after"] == ["new-id"]


def test_failed_sql_delete_never_emits_committed_receipt(workspace, caplog):
    w = workspace
    cold(w)
    with sqlite3.connect(w.WORKSPACE_DB) as c:
        c.execute("CREATE TRIGGER blocked BEFORE DELETE ON workspace BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    w._maybe_cleanup(time.time())
    with sqlite3.connect(w.WORKSPACE_DB) as c:
        assert c.execute("SELECT count(*) FROM workspace").fetchone()[0] == 1
    assert events(caplog) == []


@pytest.mark.parametrize("kind", ["ttl", "lru", "evict", "clear"])
def test_concurrent_reinsert_survives_eviction_sql_boundary(workspace, monkeypatch, kind):
    w = workspace
    cold(w)
    entered, release, push_started, pushed = (threading.Event() for _ in range(4))
    original = w._db_delete_user if kind == "clear" else w._db_delete

    def held(*a, **kw):
        entered.set()
        assert release.wait(5)
        return original(*a, **kw)

    monkeypatch.setattr(w, "_db_delete_user" if kind == "clear" else "_db_delete", held)
    monkeypatch.setattr(w, "WORKSPACE_CAPACITY", 1)

    def evict():
        if kind == "ttl":w._maybe_cleanup(time.time())
        elif kind == "lru":w.ws_push("synthetic", "new-id", "new body", bank_id="work")
        elif kind == "evict":w.ws_evict("synthetic", "old-id", bank_id="work")
        else:w.ws_clear("synthetic", bank_id="work")

    def reinsert():
        push_started.set()
        w.ws_push("synthetic", "old-id", "newer durable body", bank_id="work")
        pushed.set()

    with ThreadPoolExecutor(2) as pool:
        eviction = pool.submit(evict)
        try:
            assert entered.wait(5)
            insert = pool.submit(reinsert)
            assert push_started.wait(5)
            assert not pushed.wait(.1), "reinsertion crossed the locked disk-deletion boundary"
        finally:release.set()
        eviction.result(timeout=5)
        insert.result(timeout=5)
    with sqlite3.connect(w.WORKSPACE_DB) as c:
        assert c.execute("SELECT text FROM workspace WHERE memory_id='old-id'").fetchone()[0] == "newer durable body"
    assert w._workspace[w._scope_key("synthetic", "work")]["old-id"]["text"] == "newer durable body"
