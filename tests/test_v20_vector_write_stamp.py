"""v20 写入侧盖戳：bank_id 必须进 mem0 metadata，否则向量侧的域隔离不存在。

mem0 2.0.18 的 ``add(messages, *, user_id, agent_id, run_id, metadata, …)`` 里，
**metadata 是唯一能把自定义字段带进向量 payload 的通道**。不在写入时盖戳，
命名域的向量和默认域的向量在 payload 上就完全一样 —— 读取侧无论怎么过滤都
分不开它们。

所以这一组用例断言的不是「helper 会盖戳」（那是 bank_contract 的用例），而是
**三条真实写入路径都调用了它**：layer1 主链、speed 高速链、/add/raw 原味抽屉。
每条路径的多个出口（update / fastpath / llm）都要各查一次 —— 只在一个出口盖戳
是最典型的漏网方式。
"""
from __future__ import annotations

import pytest

from ducky.bank_contract import DEFAULT_BANK_ID


class _RecordingMemory:
    """记录每次 add/update 收到的 metadata，供断言检查 bank_id 是否盖上。

    ``candidate_bank`` 决定那条既有相似记忆属于哪个域：``None`` 表示 v19 存量
    （payload 里没有 bank_id 字段）。去重是否该命中，取决于它与写入域是否同域。
    """

    def __init__(self, dedup_hit: bool = False, candidate_bank: str | None = None):
        self.dedup_hit = dedup_hit
        self.candidate_bank = candidate_bank
        self.adds: list[dict] = []
        self.updates: list[dict] = []
        self.search_filters = None
        self.get_all_filters = None
        self.deleted: list[str] = []

    def _candidate(self, text):
        item = {"id": "old-1", "memory": text, "score": 0.99, "user_id": "alice"}
        if self.candidate_bank:
            item["metadata"] = {"bank_id": self.candidate_bank}
        return item

    def search(self, query, filters=None, **kw):
        self.search_filters = filters
        if not self.dedup_hit:
            return {"results": []}
        # 造一条高度相似的既有记忆，逼写入链走「去重更新」出口
        return {"results": [self._candidate(query)]}

    def get_all(self, filters=None, **kw):
        self.get_all_filters = filters
        return {"results": []}

    def delete(self, memory_id):
        self.deleted.append(memory_id)

    def add(self, messages, user_id=None, metadata=None, **kw):
        self.adds.append(dict(metadata or {}))
        return {"results": [{"id": "new-1", "memory": "写入内容", "event": "ADD"}]}

    def update(self, memory_id, text, metadata=None, **kw):
        self.updates.append(dict(metadata or {}))
        return {"id": memory_id}

    def get(self, memory_id):
        return {"id": memory_id, "memory": "写入内容"}


@pytest.fixture(autouse=True)
def _clear_extract_cache():
    """抽取缓存是模块级全局字典 —— 不清就会跨用例串味。

    本仓装了 pytest-randomly，用例顺序每次都不同；靠「谁先跑」来保证干净是
    在给自己埋随机红。
    """
    from ducky.speed.cache import _extract_cache
    _extract_cache.clear()
    yield
    _extract_cache.clear()


@pytest.fixture()
def isolate_side_effects(monkeypatch):
    """掐掉 FTS / salience / self-edit / 演化追踪，只观察 mem0 收到的 metadata。"""
    import ducky.layer1_selfcheck as l1
    import ducky.speed.pipeline as sp

    monkeypatch.setattr(l1, "_index_after_add", lambda *a, **k: None)
    monkeypatch.setattr(l1, "_sync_indexes_after_update", lambda *a, **k: None)
    monkeypatch.setattr(l1, "track_knowledge_evolution", lambda *a, **k: None)
    # self-edit 会短路返回，这里让它一律判「全新」，把执行流留给主写入路径
    monkeypatch.setattr(
        "ducky.self_edit.self_edit_on_add", lambda *a, **k: None
    )
    monkeypatch.setattr(sp, "load_speed_cfg", lambda: {})
    return l1, sp


# ══════════════════════════════════════════════════════════════════
# 写入路径 1：layer1_selfcheck.layer1_add_wrapper（主链）
# ══════════════════════════════════════════════════════════════════

def test_layer1_new_write_stamps_bank_id(isolate_side_effects):
    l1, _ = isolate_side_effects
    mem = _RecordingMemory()
    l1.layer1_add_wrapper(
        mem, [{"role": "user", "content": "季度目标是跑分"}],
        "alice", {"source": "chat"}, bank_id="work",
    )
    assert mem.adds, "没走到 mem0.add"
    assert mem.adds[0].get("bank_id") == "work", (
        "新增出口没盖域戳 —— 这条向量在 payload 上与默认域无从区分"
    )


def test_layer1_dedup_update_stamps_bank_id(isolate_side_effects):
    """去重更新出口同样要盖 —— update 会重写 payload，不盖就把域抹掉。"""
    l1, _ = isolate_side_effects
    mem = _RecordingMemory(dedup_hit=True, candidate_bank="work")
    l1.layer1_add_wrapper(
        mem, [{"role": "user", "content": "季度目标是跑分"}],
        "alice", {"source": "chat"}, bank_id="work",
    )
    assert mem.updates, "没走到去重更新出口"
    assert mem.updates[0].get("bank_id") == "work"


def test_layer1_default_bank_stamps_default_not_missing(isolate_side_effects):
    """默认域也要盖 —— 让 v20 之后的新点都带字段，存量点靠复筛兜底。"""
    l1, _ = isolate_side_effects
    mem = _RecordingMemory()
    l1.layer1_add_wrapper(
        mem, [{"role": "user", "content": "用户住在云城"}], "alice", {},
    )
    assert mem.adds[0].get("bank_id") == DEFAULT_BANK_ID


def test_layer1_stamp_does_not_mutate_caller_metadata(isolate_side_effects):
    l1, _ = isolate_side_effects
    caller_md = {"source": "chat"}
    l1.layer1_add_wrapper(
        _RecordingMemory(), [{"role": "user", "content": "x"}],
        "alice", caller_md, bank_id="work",
    )
    assert caller_md == {"source": "chat"}, "不得污染调用方传进来的 dict"


# ══════════════════════════════════════════════════════════════════
# 写入路径 2：speed.pipeline.run_add_pipeline（高速链）
# ══════════════════════════════════════════════════════════════════

def test_speed_llm_path_stamps_bank_id(isolate_side_effects, monkeypatch):
    _, sp = isolate_side_effects
    monkeypatch.setattr(sp, "try_fastpath_text", lambda t: None)   # 逼走 LLM 出口
    mem = _RecordingMemory()
    sp.run_add_pipeline(
        mem, [{"role": "user", "content": "季度目标是跑分"}],
        "alice", {"source": "chat"}, bank_id="work",
    )
    assert mem.adds and mem.adds[0].get("bank_id") == "work"


def test_speed_fastpath_stamps_bank_id(isolate_side_effects, monkeypatch):
    """快路径把 metadata 展开重建（{**metadata, "fastpath": True}），
    盖戳必须在展开之前完成，否则这条出口漏网。"""
    _, sp = isolate_side_effects
    monkeypatch.setattr(sp, "try_fastpath_text", lambda t: "用户的季度目标：跑分")
    mem = _RecordingMemory()
    sp.run_add_pipeline(
        mem, [{"role": "user", "content": "季度目标是跑分"}],
        "alice", {"source": "chat", "no_cache": True}, bank_id="work",
    )
    assert mem.adds and mem.adds[0].get("bank_id") == "work"
    assert mem.adds[0].get("fastpath") is True, "确认真的走的是快路径出口"


def test_speed_dedup_update_stamps_bank_id(isolate_side_effects, monkeypatch):
    _, sp = isolate_side_effects
    monkeypatch.setattr(sp, "try_fastpath_text", lambda t: None)
    mem = _RecordingMemory(dedup_hit=True, candidate_bank="work")
    sp.run_add_pipeline(
        mem, [{"role": "user", "content": "季度目标是跑分"}],
        "alice", {"source": "chat"}, bank_id="work",
    )
    assert mem.updates and mem.updates[0].get("bank_id") == "work"


# ══════════════════════════════════════════════════════════════════
# 写入路径 3：/add/raw 原味抽屉
# ══════════════════════════════════════════════════════════════════

@pytest.fixture()
def raw_client(monkeypatch):
    import ducky.hot.raw_drawer as rd
    import ducky.mem0_runtime as rt
    import ducky.text_fts as tf
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    mem = _RecordingMemory()
    monkeypatch.setattr(rt, "get_memory", lambda: mem)
    monkeypatch.setattr(tf, "_index_memory", lambda *a, **k: None)
    app = FastAPI()
    rd.register_raw_drawer_routes(app)
    return TestClient(app), mem


def test_raw_drawer_stamps_bank_id(raw_client):
    client, mem = raw_client
    resp = client.post("/add/raw", json={
        "content": "def run(): return 42",
        "user_id": "alice", "bank_id": "work", "dedup": False,
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["vector_stored"] is True, "向量没入库，盖戳断言无从谈起"
    assert mem.adds and mem.adds[0].get("bank_id") == "work", (
        "原文向量没盖域戳 —— 只有 FTS 那一半被隔离，向量侧混在一起"
    )


def test_raw_drawer_keeps_verbatim_tier_alongside_bank(raw_client):
    """盖戳不能把原有的 memory_tier / source 等字段挤掉。"""
    client, mem = raw_client
    client.post("/add/raw", json={
        "content": "def run(): return 42",
        "user_id": "alice", "bank_id": "work", "dedup": False,
        "metadata": {"category": "code"},
    })
    md = mem.adds[0]
    assert md.get("bank_id") == "work"
    assert md.get("memory_tier") == "verbatim"
    assert md.get("category") == "code"
