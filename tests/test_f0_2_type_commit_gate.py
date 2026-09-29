"""A failed type-ledger commit must never publish that label to replicas."""
from __future__ import annotations

import sqlite3

import pytest


def test_failed_ledger_write_does_not_sync_vector_or_fts(monkeypatch):
    import ducky.memory_types as types
    import ducky.mem0_runtime as runtime
    import ducky.text_fts as fts

    calls = []
    monkeypatch.setattr(types, "ensure_memory_types_schema", lambda: None)
    monkeypatch.setattr(types, "get_facts_conn", lambda: sqlite3.connect(":memory:"))
    monkeypatch.setattr(types, "_write_storage_ref",
                        lambda *_: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    monkeypatch.setattr(runtime, "get_memory", lambda: calls.append("vector"))
    monkeypatch.setattr(fts, "_set_memory_type", lambda *a, **kw: calls.append("fts"))

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        types.classify_and_sync_memory("ref", "用户决定迁移", user_id="alice",
                                       bank_id="work")
    assert calls == []
