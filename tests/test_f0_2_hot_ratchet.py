"""The direct-write helper keeps batch scope and independent index legs."""
from __future__ import annotations

import sqlite3


def test_direct_write_results_keep_batch_bank_and_survive_fts_failure(monkeypatch):
    from ducky.hot import add as hot_add
    import ducky.text_fts as text_fts
    import ducky.memory_types as memory_types

    indexed = []
    typed = []
    failed = []

    def index(memory_id, content, **scope):
        indexed.append((memory_id, content, scope))
        if memory_id == "one":
            raise sqlite3.OperationalError("temporary FTS lock")

    monkeypatch.setattr(text_fts, "_index_memory", index)
    monkeypatch.setattr(memory_types, "classify_and_sync_memory",
                        lambda memory_id, content, **scope:
                        typed.append((memory_id, content, scope)))
    monkeypatch.setattr(hot_add, "feature_failed",
                        lambda name, exc: failed.append((name, type(exc))))

    hot_add._index_direct_results(
        [{"id": "one", "memory": "first"},
         {"id": "two", "memory": "second"},
         {"memory": "no id"}],
        user_id="alice", bank_id="work", category="tech",
    )

    assert [row[0] for row in indexed] == ["one", "two"]
    assert [row[0] for row in typed] == ["one", "two"]
    assert all(row[2]["bank_id"] == "work" for row in indexed + typed)
    assert all(row[2]["user_id"] == "alice" for row in indexed + typed)
    assert all(row[2]["category"] == "tech" for row in indexed)
    assert failed == [("index_memory", sqlite3.OperationalError)]
