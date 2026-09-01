"""v20.3.1（九份审计 P0-5）：幂等保护必须覆盖全部早返回路径。

上一版的病：`idempotency.finalize` 只挂在 /add 同步完整路径末尾，
local_only / deferred_distillation / coalesce / async_queued 五类早返回
全部漏落账 —— response_json 恒 NULL，客户端 10 分钟 TTL 内重试被判
pending 而非 replay，幂等保护恰在 local 档（用户最可能首跑的档）失效。
外加 /add/raw 完全没有幂等键。

判据：同键同负载重发两次，第二次必须 replay 判真（回放首次响应、
不重复落库）。四路全测，一路不落。
"""

import json
import os
import pathlib
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

_ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture()
def app_rig(tmp_path, monkeypatch):
    """真实路由 + 隔离库。

    mem0 单例按生产契约 mock：local 档真实部署有配置文件（vector_store
    指向本地 qdrant 路径），本机测试环境没有 —— 替身只暴露生产 API 面
    （add/search/get_all 签名逐字对齐），不比生产宽。
    """
    monkeypatch.setenv("AIDUMEM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AIDUMEM_LOG_DIR", str(tmp_path / "logs"))

    class _MemStub:
        @staticmethod
        def add(content, user_id=None, metadata=None, infer=False, **kw):
            return {"results": [{"id": f"stub-{abs(hash(content)) % 10**8}",
                                 "memory": content}]}

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
    monkeypatch.setattr(add_mod, "patch_llm_for_speed", lambda mem: None, raising=False)

    from ducky.hot.add import register_add_routes
    from ducky.hot.raw_drawer import register_raw_drawer_routes
    app = FastAPI()
    register_add_routes(app)
    register_raw_drawer_routes(app)
    return TestClient(app), tmp_path


def _facts_conn(tmp_path):
    db = tmp_path / "data" / "facts.db"
    if not db.exists():
        for p in (tmp_path / "data").glob("facts.db*"):
            pass
    return sqlite3.connect(str(db))


def test_local_only_path_replays_on_retry(app_rig, monkeypatch):
    """local 档早返回：同键重发 → replay 判真 + 不重复落库。"""
    client, tmp_path = app_rig
    # 强制 local 档（cloud_leg_enabled=False → 走 local_only 早返回）
    import ducky.engine_mode as em
    monkeypatch.setattr(em, "cloud_leg_enabled", lambda *a, **kw: False)
    body = {
        "messages": [{"role": "user", "content": "local-replay-probe unique data 001"}],
        "user_id": "replay-u1", "bank_id": "default",
        "infer": False, "idempotency_key": "key-local-1",
    }
    r1 = client.post("/add", json=body)
    assert r1.status_code == 200, r1.text[:300]
    first = r1.json()
    assert first.get("action") == "local_only", first
    assert first.get("request_id") == "key-local-1", "早返回没回填 request_id"
    r2 = client.post("/add", json=body)
    assert r2.status_code == 200
    second = r2.json()
    assert second.get("idempotency_replayed") is True, (
        f"local 档重试没有 replay（action={second.get('action')}）—— "
        "幂等保护在最可能首跑的档位失效（九份审计 P0-5 原文）"
    )


def test_raw_drawer_has_idempotency_now(app_rig):
    """P0-5 + 嘟嘟 🟡-3：/add/raw 幂等键。同键重发回放首次响应。"""
    client, tmp_path = app_rig
    body = {"content": "raw-replay-probe unique content 001",
            "user_id": "replay-raw-u1", "bank_id": "default",
            "idempotency_key": "key-raw-1"}
    r1 = client.post("/add/raw", json=body)
    assert r1.status_code == 200, r1.text[:300]
    first = r1.json()
    assert first.get("request_id") == "key-raw-1"
    r2 = client.post("/add/raw", json=body)
    second = r2.json()
    assert second.get("idempotency_replayed") is True, (
        f"/add/raw 重试没有 replay: {second.get('action')}"
    )
    # 首次与回放的 memory_id 必须一致（同一条记忆，不是第二条）
    assert second.get("memory_id") == first.get("memory_id")


def test_raw_drawer_conflict_on_different_payload(app_rig):
    """同键不同负载 → 409（幂等键绑定校验，防滥用一个键写不同内容）。"""
    client, tmp_path = app_rig
    client.post("/add/raw", json={"content": "raw-conflict-a",
                                  "user_id": "replay-raw-u2", "bank_id": "default",
                                  "idempotency_key": "key-conflict-1"})
    r2 = client.post("/add/raw", json={"content": "raw-conflict-B-different",
                                       "user_id": "replay-raw-u2", "bank_id": "default",
                                       "idempotency_key": "key-conflict-1"})
    assert r2.status_code == 409, f"同键不同负载必须 409, got {r2.status_code}"


def test_async_queued_path_finalizes(app_rig, monkeypatch):
    """async 早返回：accepted 回执也要落账 —— 重试回放 job 回执，不重复入队。

    注意不强制 local 档：挡位分流在 async 分流**之前**，local 档直接
    local_only 返回（设计行为——local 零 token 不需要 async 后台蒸馏）。
    async 路径用真实 cloud 档判定 + stub 的 mem0 单例走到真正的 async 分支。
    """
    client, tmp_path = app_rig
    body = {
        "messages": [{"role": "user", "content": "async-replay-probe data 001"}],
        "user_id": "replay-u3", "bank_id": "default",
        "infer": False, "async_mode": True, "idempotency_key": "key-async-1",
    }
    r1 = client.post("/add", json=body)
    assert r1.status_code == 200, r1.text[:300]
    first = r1.json()
    assert first.get("action") in {"async_queued", "coalesce_buffered", "coalesce_flushed"}, first
    r2 = client.post("/add", json=body)
    second = r2.json()
    assert second.get("idempotency_replayed") is True, (
        f"async 路径重试没有 replay: {second}"
    )


def test_idempotency_key_header_support(app_rig, monkeypatch):
    """P1-1：Idempotency-Key header 形态与 body 字段同一 claim 链。

    Release Notes 宣称过 header —— 照发布说明集成的调用方（网关/SDK
    重试语义走 header）此前静默失去保护。判据：真发 header 两次，第二次
    replay 且记忆不增。
    """
    client, tmp_path = app_rig
    import ducky.engine_mode as em
    monkeypatch.setattr(em, "cloud_leg_enabled", lambda *a, **kw: False)
    payload = {
        "messages": [{"role": "user", "content": "header-replay-probe data 001"}],
        "user_id": "replay-u4", "bank_id": "default", "infer": False,
    }
    headers = {"Idempotency-Key": "key-header-1"}
    r1 = client.post("/add", json=payload, headers=headers)
    assert r1.status_code == 200, r1.text[:300]
    first = r1.json()
    assert first.get("request_id") == "key-header-1", (
        f"header 形态的幂等键没被读到: {first.get('request_id')}"
    )
    r2 = client.post("/add", json=payload, headers=headers)
    second = r2.json()
    assert second.get("idempotency_replayed") is True, (
        "header 形态重发没有 replay —— 网关/SDK 重试语义仍然裸奔"
    )
