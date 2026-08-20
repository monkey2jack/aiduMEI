"""v20 五个向量读取点：真调函数，防止有人把「默认域下推 bank_id」改回去。

test_v20_vector_bank_contract.py 钉的是契约本身（helper 的语义）；这一组钉的是
**调用点真的用了那个契约**。两者缺一不可 —— 契约再对，读取点绕过去写死
``filters={"user_id":…, "bank_id":…}``，生产照样全盘归零。

每条用例都塞一条「v19 存量向量」（payload 无 bank_id）和一条命名域向量，
断言两个方向：存量在默认域必须还在，命名域的点必须不漏进默认域。
"""
from __future__ import annotations

import pytest

from ducky.bank_contract import DEFAULT_BANK_ID


class _MustFilterMemory:
    """按 Qdrant must 语义模拟 mem0 —— 缺字段的点被条件过滤掉，且不报错。

    这是整组用例的关键：如果读取点把 bank_id 下推给默认域，这个假 mem0 会
    像真 Qdrant 一样**静默**返回空，用例就红。
    """

    def __init__(self):
        self.search_filters = None
        self.get_all_filters = None

    @staticmethod
    def _points():
        return [
            # v19 存量：payload 里没有 bank_id 字段
            {"id": "legacy", "memory": "用户住在云城", "score": 0.91,
             "user_id": "alice", "created_at": "2025-01-01T00:00:00Z"},
            # v20 默认域
            {"id": "dflt", "memory": "用户喜欢拿铁", "score": 0.88,
             "user_id": "alice", "created_at": "2025-06-01T00:00:00Z",
             "metadata": {"bank_id": "default"}},
            # v20 命名域
            {"id": "work", "memory": "季度目标是跑分", "score": 0.85,
             "user_id": "alice", "created_at": "2025-06-02T00:00:00Z",
             "metadata": {"bank_id": "work"}},
        ]

    @classmethod
    def _apply(cls, filters):
        out = []
        for p in cls._points():
            md = p.get("metadata") or {}
            if all(p.get(k, md.get(k)) == v for k, v in (filters or {}).items()):
                out.append(dict(p))
        return out

    def search(self, query, filters=None, **kw):
        self.search_filters = filters
        return {"results": self._apply(filters)}

    def get_all(self, filters=None, **kw):
        self.get_all_filters = filters
        return {"results": self._apply(filters)}


def _ids(results):
    return [r.get("id") for r in results if isinstance(r, dict)]


# ══════════════════════════════════════════════════════════════════
# 读取点 1：ducky/engine.py · RecallEngine.search
# ══════════════════════════════════════════════════════════════════

def test_engine_search_default_bank_keeps_legacy_vectors():
    from ducky.engine import RecallEngine

    mem = _MustFilterMemory()
    out = RecallEngine(memory_instance=mem).search("用户", user_id="alice")

    assert "bank_id" not in (mem.search_filters or {}), \
        "engine 默认域下推了 bank_id —— 存量向量会在生产上整批消失"
    assert "legacy" in _ids(out), "v19 存量向量必须仍能被默认域召回"
    assert "work" not in _ids(out), "命名域的点不得漏进默认域结果"


def test_engine_search_named_bank_isolates():
    from ducky.engine import RecallEngine

    mem = _MustFilterMemory()
    out = RecallEngine(memory_instance=mem).search(
        "季度", user_id="alice", bank_id="work"
    )

    assert (mem.search_filters or {}).get("bank_id") == "work"
    assert _ids(out) == ["work"]


# ══════════════════════════════════════════════════════════════════
# 读取点 2：ducky/recall_funnel.py · funnel_search 候选池
# ══════════════════════════════════════════════════════════════════

def test_funnel_search_default_bank_keeps_legacy_vectors():
    from ducky.recall_funnel import funnel_search

    mem = _MustFilterMemory()
    out = funnel_search(mem, "用户", "alice", limit=10, enable_ignition=False)

    assert "bank_id" not in (mem.search_filters or {}), \
        "funnel 默认域下推了 bank_id —— 候选池会恒为空"
    assert out["trace"]["stages"][0]["count"] == 2, "候选池应含存量 + 默认域两条"
    assert "legacy" in _ids(out["results"])
    assert "work" not in _ids(out["results"])


def test_funnel_search_named_bank_isolates():
    from ducky.recall_funnel import funnel_search

    mem = _MustFilterMemory()
    out = funnel_search(mem, "季度", "alice", limit=10,
                        enable_ignition=False, bank_id="work")

    assert (mem.search_filters or {}).get("bank_id") == "work"
    assert _ids(out["results"]) == ["work"]


# ══════════════════════════════════════════════════════════════════
# 读取点 3：ducky/hot/search.py · 混合召回降级路径
# ══════════════════════════════════════════════════════════════════

def _degraded_search(monkeypatch, bank_id=DEFAULT_BANK_ID):
    """把 hybrid 召回打挂，逼 /search 走 mem0 裸搜降级分支。"""
    import ducky.hot.search as hs

    mem = _MustFilterMemory()
    monkeypatch.setattr(hs, "get_memory", lambda: mem)
    monkeypatch.setattr(
        hs, "lazy_import_hybrid",
        lambda: (_ for _ in ()).throw(RuntimeError("hybrid 不可用")),
    )
    monkeypatch.setattr(hs, "boost_salience_for_results", lambda r: None)
    monkeypatch.setattr(hs, "_annotate_memory_types", lambda r: None)
    return mem, hs


def test_hot_search_degraded_path_default_bank_keeps_legacy(monkeypatch):
    mem, hs = _degraded_search(monkeypatch)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    hs.register_search_routes(app)
    resp = TestClient(app).post(
        "/search", json={"query": "用户", "user_id": "alice", "limit": 10}
    )
    assert resp.status_code == 200, resp.text

    assert "bank_id" not in (mem.search_filters or {}), \
        "/search 降级路径默认域下推了 bank_id —— 存量向量搜不到且不报错"
    ids = _ids(resp.json().get("results") or [])
    assert "legacy" in ids
    assert "work" not in ids


def test_hot_search_degraded_path_named_bank_isolates(monkeypatch):
    mem, hs = _degraded_search(monkeypatch)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    hs.register_search_routes(app)
    resp = TestClient(app).post(
        "/search",
        json={"query": "季度", "user_id": "alice", "limit": 10, "bank_id": "work"},
    )
    assert resp.status_code == 200, resp.text
    assert (mem.search_filters or {}).get("bank_id") == "work"
    assert _ids(resp.json().get("results") or []) == ["work"]


# ══════════════════════════════════════════════════════════════════
# 读取点 4 / 5：ducky/hot/crud.py · /recent 与 /stats
# ══════════════════════════════════════════════════════════════════

@pytest.fixture()
def crud_client(monkeypatch):
    import ducky.hot.crud as crud
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    mem = _MustFilterMemory()
    monkeypatch.setattr(crud, "get_memory", lambda: mem)
    app = FastAPI()
    crud.register_crud_routes(app)
    return TestClient(app), mem


def test_recent_default_bank_keeps_legacy(crud_client):
    client, mem = crud_client
    resp = client.get("/recent", params={"user_id": "alice", "limit": 10})
    assert resp.status_code == 200, resp.text

    assert "bank_id" not in (mem.get_all_filters or {}), \
        "/recent 默认域下推了 bank_id —— 最近记忆会恒为空"
    ids = _ids(resp.json()["results"]["results"])
    assert "legacy" in ids
    assert "work" not in ids


def test_recent_named_bank_isolates(crud_client):
    client, mem = crud_client
    resp = client.get(
        "/recent", params={"user_id": "alice", "bank_id": "work", "limit": 10}
    )
    assert resp.status_code == 200, resp.text
    assert (mem.get_all_filters or {}).get("bank_id") == "work"
    assert _ids(resp.json()["results"]["results"]) == ["work"]


def test_stats_default_bank_counts_legacy_not_named(crud_client):
    client, mem = crud_client
    resp = client.get("/stats", params={"user_id": "alice"})
    assert resp.status_code == 200, resp.text

    assert "bank_id" not in (mem.get_all_filters or {}), \
        "/stats 默认域下推了 bank_id —— 统计会恒为 0"
    body = resp.json()
    total = body.get("total_memories", body.get("total"))
    assert total == 2, f"默认域应只数存量 + 默认域两条，实得 {total}：{body}"


def test_stats_named_bank_counts_only_that_bank(crud_client):
    client, mem = crud_client
    resp = client.get("/stats", params={"user_id": "alice", "bank_id": "work"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    total = body.get("total_memories", body.get("total"))
    assert total == 1, f"work 域应只有一条，实得 {total}：{body}"
