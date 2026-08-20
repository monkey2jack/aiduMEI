"""aiduMEI v20 P0-2 — Obsidian 同步路由的记忆库作用域测试

覆盖点：
1. /api/obsidian/sync 传 bank_id → mem.add 的 metadata 盖上 bank 戳
   （metadata 是 bank_id 进向量 payload 的唯一通道）
2. 不传 bank_id = default 域（v19 行为零改动）；user_id 仍从
   metadata 读取并透传
3. 非法 bank_id → 400（不许 500，更不许静默落 default），且不落库
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ducky.routes_obsidian as obsidian_mod
from ducky.routes_obsidian import register_obsidian_routes


class _FakeMem:
    def __init__(self):
        self.calls = []

    def add(self, text, user_id=None, metadata=None):
        self.calls.append({"text": text, "user_id": user_id, "metadata": metadata})
        return {"results": []}


@pytest.fixture()
def client_and_mem(monkeypatch):
    fake = _FakeMem()
    monkeypatch.setattr(obsidian_mod, "get_memory", lambda: fake)
    monkeypatch.setattr(obsidian_mod, "_is_obsidian_enabled", lambda: True)
    app = FastAPI()
    register_obsidian_routes(app)
    return TestClient(app), fake


def test_obsidian_sync_stamps_bank_metadata(client_and_mem):
    client, fake = client_and_mem
    resp = client.post("/api/obsidian/sync", json={
        "title": "甲库笔记",
        "content": "没有双链的纯文本内容",
        "metadata": {"user_id": "user_x"},
        "bank_id": "bank_a",
    })
    assert resp.status_code == 200, resp.text
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["user_id"] == "user_x"
    assert call["metadata"]["bank_id"] == "bank_a"
    assert call["metadata"]["source"] == "obsidian"


def test_obsidian_sync_default_bank_when_omitted(client_and_mem):
    client, fake = client_and_mem
    resp = client.post("/api/obsidian/sync", json={
        "title": "无库笔记",
        "content": "老调用方不带 bank_id",
    })
    assert resp.status_code == 200, resp.text
    call = fake.calls[0]
    assert call["metadata"]["bank_id"] == "default"
    assert call["user_id"] == "default"


def test_obsidian_sync_invalid_bank_id_returns_400_and_no_write(client_and_mem):
    client, fake = client_and_mem
    resp = client.post("/api/obsidian/sync", json={
        "title": "越权笔记",
        "content": "内容",
        "bank_id": "../etc",
    })
    assert resp.status_code == 400, resp.text
    assert "bank_id" in resp.json()["detail"]
    assert fake.calls == [], "非法 bank_id 不许落库（负向对照）"
