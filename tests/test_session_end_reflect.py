"""
tests/test_session_end_reflect.py — P0-3 会话结束触发 Reflect 接线测试（v19.0）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
覆盖（对齐调研报告 6.2 P0-3 触发方式 c「会话结束时」）：
  1. pipeline.session_end 成功返回 user_id
  2. AIDUMEM_REFLECT_ON_SESSION_END=false 时不启动后台反思
  3. /session/end 路由成功时后台触发 run_reflect(source="session_end")
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import ducky.utils as utils

_tmp_dir = tempfile.mkdtemp(prefix="aidumem_session_end_test_")
_TEST_DB = os.path.join(_tmp_dir, "facts.db")
utils.FACTS_DB = _TEST_DB


@pytest.fixture(autouse=True)
def _bind_test_db():
    utils.FACTS_DB = _TEST_DB
    yield


# ── 1. session_end 返回 user_id ──────────────────────────
def test_pipeline_session_end_returns_user_id():
    from ducky.pipeline import memory_persistence as mp
    started = mp.session_start("u-42")
    ended = mp.session_end(started["session_id"])
    assert ended["status"] == "ok"
    assert ended["user_id"] == "u-42"
    # 不存在的 session 仍返回 error
    assert mp.session_end("no-such-session")["status"] == "error"


# ── 2. 开关关闭时不启动线程 ──────────────────────────────
def test_trigger_respects_off_switch(monkeypatch):
    import ducky.routes_v8 as rv8

    monkeypatch.setattr(rv8, "_REFLECT_ON_SESSION_END", False)
    spawned: list[bool] = []

    class _SpyThread(threading.Thread):
        def __init__(self, *a, **k):
            spawned.append(True)
            super().__init__(*a, **k)

    monkeypatch.setattr("ducky.routes_v8.threading.Thread", _SpyThread)
    rv8._trigger_session_end_reflect("u-off")
    assert spawned == []


# ── 3. /session/end 后台触发 run_reflect(source="session_end") ─
def test_session_end_route_triggers_reflect(monkeypatch):
    import ducky.routes_v8 as rv8
    from ducky.pipeline import memory_persistence as mp

    monkeypatch.setattr(rv8, "_REFLECT_ON_SESSION_END", True)

    calls: list[dict] = []

    def fake_run_reflect(**kwargs):
        calls.append(kwargs)
        return {"status": "ok", "insights": [], "saved": 0, "source": "session_end", "llm_used": False}

    monkeypatch.setattr("ducky.reflect.run_reflect", fake_run_reflect)

    started = mp.session_start("u-reflect")
    sid = started["session_id"]

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    rv8.register_v8_routes(app)
    client = TestClient(app)

    r = client.post("/session/end", params={"session_id": sid})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["user_id"] == "u-reflect"

    deadline = time.time() + 5.0
    while not calls and time.time() < deadline:
        time.sleep(0.05)
    assert calls, "会话结束未触发后台反思"
    assert calls[0].get("source") == "session_end"
    assert calls[0].get("user_id") == "u-reflect"
