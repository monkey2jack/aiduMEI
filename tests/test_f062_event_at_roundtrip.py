"""f0.6.2 · event_at 写读形态对称守卫。

读线（ducky/engine.py 三级时间优先）对 event_at 认两种形态：
metadata.event_at 与顶层 event_at（engine.search: _md.get("event_at")
or item.get("event_at")）。f0.6.1 写线只透传 metadata 形态——调用方
按读线形状传顶层 event_at 时被静默丢弃，时间窗过滤（before/after）
退化为 created_at 基准，补录旧事全部漂移。

断言落在**写线出口**：/add 组装出的最终 metadata 恰好是 mem.add、
verbatim 落库、向量 payload 三条下游共同的源（hot/add.py 的 md 被原样
透传给全部存储层），桩收到的 metadata 即是落库 metadata。每条配负向
对照证明判据不是永真。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

EVENT_AT = "2026-05-08T13:56:00+00:00"


@pytest.fixture()
def add_rig(tmp_path, monkeypatch):
    """真实 /add 路由 + 捕获型 verbatim 桩。

    缝位选 verbatim_vault.store_verbatim：local 挡下 mem.add 不被调用
    （local_only 早返回），md 的 durable 落库出口是 store_verbatim——
    它收到的 metadata 即 /add 组装出的最终 md（写线三条下游共同源头）。
    """
    monkeypatch.setenv("AIDUMEM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AIDUMEM_LOG_DIR", str(tmp_path / "logs"))

    captured: list[dict] = []

    import ducky.verbatim_vault as vv

    def _spy_store_verbatim(user_id, messages_json, metadata=None, **kw):
        captured.append(dict(metadata or {}))
        return {"stored": 1, "skipped": 0}

    monkeypatch.setattr(vv, "store_verbatim", _spy_store_verbatim)

    class _MemStub:
        @staticmethod
        def add(content, user_id=None, metadata=None, infer=False, **kw):
            return {"results": [{"id": "stub-1", "memory": str(content)}]}

        @staticmethod
        def search(query, user_id=None, limit=5, **kw):
            return {"results": []}

        @staticmethod
        def get_all(user_id=None, **kw):
            return {"results": []}

    import ducky.mem0_runtime as mr
    monkeypatch.setattr(mr, "get_memory", lambda: _MemStub(), raising=False)
    import ducky.hot.add as add_mod
    monkeypatch.setattr(add_mod, "get_memory", lambda: _MemStub(), raising=False)
    monkeypatch.setattr(add_mod, "patch_llm_for_speed", lambda mem: None,
                        raising=False)
    # local 挡：/add 走确定性通路（零云外呼）
    import ducky.engine_mode as em
    monkeypatch.setattr(em, "cloud_leg_enabled", lambda *a, **kw: False)

    from ducky.hot.add import register_add_routes
    app = FastAPI()
    register_add_routes(app)
    return TestClient(app), captured


def test_event_at_metadata_form_lands(add_rig):
    """metadata 形态（f0.6.1 原语义）不得被改坏。"""
    client, captured = add_rig
    r = client.post("/add", json={
        "messages": [{"role": "user", "content": "事件锚点甲乙丙"}],
        "user_id": "evat_md", "infer": False,
        "metadata": {"event_at": EVENT_AT},
    })
    assert r.status_code == 200, r.text
    assert captured, "mem.add 未被调到，断言落空"
    assert any(m.get("event_at") == EVENT_AT for m in captured), \
        f"metadata 形态 event_at 丢失: {captured!r}"


def test_event_at_top_level_form_lands(add_rig):
    """f0.6.2 修的主体：顶层 event_at 不再被静默丢弃。"""
    client, captured = add_rig
    r = client.post("/add", json={
        "messages": [{"role": "user", "content": "事件锚点甲乙丙"}],
        "user_id": "evat_top", "infer": False,
        "event_at": EVENT_AT,
    })
    assert r.status_code == 200, r.text
    assert any(m.get("event_at") == EVENT_AT for m in captured), \
        f"顶层 event_at 被丢弃（f0.6.1 旧病形态）: {captured!r}"


def test_event_at_metadata_wins_over_top_level(add_rig):
    """两种形态同传时 metadata 显式值优先——就近显式原则。"""
    other = "2026-01-01T00:00:00+00:00"
    client, captured = add_rig
    r = client.post("/add", json={
        "messages": [{"role": "user", "content": "事件锚点甲乙丙"}],
        "user_id": "evat_both", "infer": False,
        "event_at": other,
        "metadata": {"event_at": EVENT_AT},
    })
    assert r.status_code == 200, r.text
    assert captured and all(m.get("event_at") == EVENT_AT for m in captured), \
        f"metadata 优先被破坏: {captured!r}"


def test_no_event_at_stays_unfabricated(add_rig):
    """负向对照：不传 event_at 时不造默认值（recorded_at 兜底语义不变）。

    负向对照的意义：若写线把 event_at 无条件造出来（比如 copy recorded_at），
    上面的三条会全部假绿——本条证明「不传就没有」这个判据真的在挡。
    """
    client, captured = add_rig
    r = client.post("/add", json={
        "messages": [{"role": "user", "content": "事件锚点甲乙丙"}],
        "user_id": "evat_none", "infer": False,
    })
    assert r.status_code == 200, r.text
    assert captured and all(not m.get("event_at") for m in captured), \
        f"未传却造出了 event_at: {captured!r}"
    assert captured[0].get("recorded_at"), "recorded_at 自动时间戳兜底仍在"
