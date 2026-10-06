"""Enumeration must honor size even when unknown SDK keywords are ignored."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ducky.bank_contract import make_scope, vector_item_bank, vector_scope_filters
from ducky.mem0_compat import get_all_memories


class _Store:
    def __init__(self):
        self.calls = []
        self.items = [
            {"id": f"{user}-{bank}-{i}", "memory": f"record {i}",
             "user_id": user, "metadata": {"bank_id": bank}}
            for user in ("enum-a", "enum-b")
            for bank in ("default", "archive") for i in range(24)
        ]

    def select(self, filters, size, kwargs):
        self.calls.append((filters.copy(), size, kwargs.copy()))
        items = [item for item in self.items if item["user_id"] == filters["user_id"]
                 and ("bank_id" not in filters or vector_item_bank(item) == filters["bank_id"])]
        return {"results": items[:size], "sdk_marker": "preserved"}


class _Current(_Store):
    def get_all(self, *, filters=None, top_k=20, show_expired=False, **kwargs):
        return self.select(filters, top_k, kwargs)


class _Legacy(_Store):
    def get_all(self, filters=None, limit=20, **kwargs):
        return self.select(filters, limit, kwargs)


@pytest.mark.parametrize("sdk", [_Current, _Legacy])
def test_size_keyword_survives_silent_kwargs_with_scope_and_response(sdk):
    memory = sdk()
    filters = vector_scope_filters("enum-a", "archive")
    wrong = "limit" if sdk is _Current else "top_k"
    # Negative control: the old spelling silently returns only 20 of 24.
    assert len(memory.get_all(filters=filters, **{wrong: 10000})["results"]) == 20
    result = get_all_memories(memory, filters=filters, limit=10000)
    assert len(result["results"]) == 24
    assert result["sdk_marker"] == "preserved"
    assert memory.calls[-1] == (filters, 10000, {})
    assert filters == vector_scope_filters("enum-a", "archive")


@pytest.fixture
def client_for(monkeypatch):
    from ducky.hot import crud

    def build(memory):
        monkeypatch.setattr(crud, "get_memory", lambda: memory)
        app = FastAPI()
        crud.register_crud_routes(app)
        return TestClient(app)
    return build


@pytest.mark.parametrize("sdk", [_Current, _Legacy])
@pytest.mark.parametrize("bank", ["default", "archive"])
def test_recent_requested_limits_and_scope_beyond_twenty(sdk, bank, client_for):
    memory = sdk()
    client = client_for(memory)
    for limit in (10, 24, 10000):
        response = client.get("/recent", params={"user_id": "enum-a", "bank_id": bank, "limit": limit})
        assert response.status_code == 200
        result = response.json()["results"]
        items = result["results"]
        assert len(items) == min(limit, 24)
        assert result["sdk_marker"] == "preserved"
        assert all(item["user_id"] == "enum-a" and vector_item_bank(item) == bank for item in items)
        assert memory.calls[-1] == (vector_scope_filters("enum-a", bank), limit, {})


@pytest.mark.parametrize("sdk", [_Current, _Legacy])
@pytest.mark.parametrize("bank", ["default", "archive"])
def test_stats_counts_entire_requested_scope(sdk, bank, client_for):
    memory = sdk()
    response = client_for(memory).get("/stats", params={"user_id": "enum-a", "bank_id": bank})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == body["total_memories"] == 24
    assert memory.calls[-1] == (vector_scope_filters("enum-a", bank), 10000, {})


@pytest.mark.parametrize("sdk", [_Current, _Legacy])
def test_deletion_inventory_cannot_mistake_silent_cap_for_complete(sdk):
    from ducky.wal_engine import _scoped_vector_items, VECTOR_ENUM_LIMIT

    memory = sdk()
    scope = make_scope("enum-a", "archive")
    items, complete = _scoped_vector_items(memory, scope)
    assert complete and len(items) == 24
    assert memory.calls[-1] == (vector_scope_filters(scope.user_id, scope.bank_id), VECTOR_ENUM_LIMIT, {})


def test_backend_typeerror_propagates_without_retry_or_scope_relaxation():
    calls = []

    class Broken:
        def get_all(self, *, filters=None, top_k=20, **kwargs):
            calls.append((filters, top_k, kwargs))
            raise TypeError("backend decoder failed")

    filters = vector_scope_filters("enum-a", "archive")
    with pytest.raises(TypeError, match="backend decoder failed"):
        get_all_memories(Broken(), filters=filters, limit=10000)
    assert calls == [(filters, 10000, {})]


def test_current_explicit_size_wins_over_legacy_alias():
    class Both(_Current):
        def get_all(self, *, filters=None, top_k=20, limit=None, **kwargs):
            assert limit is None
            return self.select(filters, top_k, kwargs)

    result = get_all_memories(Both(), filters=vector_scope_filters("enum-a", "archive"), limit=24)
    assert len(result["results"]) == 24


def test_opaque_wrapper_keeps_canonical_keyword_and_filters():
    memory = _Current()

    class Wrapper:
        def get_all(self, **kwargs):
            return memory.get_all(**kwargs)

    result = get_all_memories(Wrapper(), filters=vector_scope_filters("enum-a", "archive"), limit=24)
    assert len(result["results"]) == 24
    assert memory.calls[-1] == (vector_scope_filters("enum-a", "archive"), 24, {})


@pytest.mark.parametrize("sdk", [_Current, _Legacy])
def test_fts_backfill_keeps_all_source_banks_past_twenty(sdk, monkeypatch):
    from ducky import mem0_runtime, text_fts

    memory = sdk()
    indexed = []
    monkeypatch.setattr(mem0_runtime, "get_memory", lambda: memory)
    monkeypatch.setattr(text_fts, "_index_memory", lambda mid, text, **kwargs: indexed.append((mid, text, kwargs)))
    assert text_fts._backfill_text_fts(limit=10000, user_id="enum-a") == 48
    assert {row[0] for row in indexed} == {item["id"] for item in memory.items if item["user_id"] == "enum-a"}
    assert all(row[2]["user_id"] == "enum-a" for row in indexed)
    assert sum(row[2]["bank_id"] == "archive" for row in indexed) == 24
    assert memory.calls[-1] == ({"user_id": "enum-a"}, 10000, {})


@pytest.mark.parametrize("sdk", [_Current, _Legacy])
def test_capacity_counts_whole_bank_past_twenty(sdk):
    from ducky.layer1_selfcheck import check_capacity

    memory = sdk()
    assert check_capacity(memory, "enum-a", "archive")["total"] == 24
    assert memory.calls[-1] == (vector_scope_filters("enum-a", "archive"), 10000, {})
