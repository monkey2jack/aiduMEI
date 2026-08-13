"""
tests/test_audit_fixes_v19_1.py — v19.1 审计修复回归测试
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
锁死社区审计指出、本版已修的若干问题，防回归：
  🟢20 session_unpin 判空逻辑
  🟢21 session_search context_used 不再恒真
  🔴9  SQLite REGEXP 已注册
  🔴8  /crystals/approve 端点存在且生效
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import ducky.utils as utils

_tmp = tempfile.mkdtemp(prefix="aidumem_audit_fix_")
_TEST_DB = os.path.join(_tmp, "facts.db")
utils.FACTS_DB = _TEST_DB


@pytest.fixture(autouse=True)
def _bind_db():
    utils.FACTS_DB = _TEST_DB
    yield


# ── 🔴9 REGEXP 已注册 ─────────────────────────────────────
def test_sqlite_regexp_registered():
    conn = utils.get_facts_conn()
    assert conn.execute("SELECT 1 WHERE 'hello world' REGEXP 'wor'").fetchone() is not None
    assert conn.execute("SELECT 1 WHERE 'abc' REGEXP 'xyz'").fetchone() is None
    # NULL 安全
    assert conn.execute("SELECT 1 WHERE NULL REGEXP 'x'").fetchone() is None


# ── 🟢20 session_unpin 判空逻辑 ───────────────────────────
def test_session_unpin_logic():
    from ducky.pipeline import memory_persistence as mp

    started = mp.session_start("u-unpin")
    sid = started["session_id"]
    mp.session_pin(sid, "mem-1")
    # 不存在的 session 应报 error，不 AttributeError
    assert mp.session_unpin("no-such", "mem-1")["status"] == "error"
    # 存在的 session：unpin 真正移除
    r = mp.session_unpin(sid, "mem-1")
    assert r["status"] == "ok"
    report = mp.session_report(sid) if hasattr(mp, "session_report") else None
    # 直接查内部状态确认已移除
    assert "mem-1" not in mp._sessions[sid]["pinned_ids"]


# ── 🟢21 context_used 不再恒真 ────────────────────────────
def test_context_used_not_always_true():
    from ducky.pipeline import memory_persistence as mp

    class _FakeMem:
        def search(self, *a, **k):
            return {"results": []}

    started = mp.session_start("u-ctx")
    sid = started["session_id"]
    # 首次搜索：历史为空，不该因为 query 和自己比中而恒真
    res = mp.session_search(_FakeMem(), sid, "第一条全新查询", use_context=True)
    assert res["context_used"] is False, "首查历史为空，context_used 不该为真"


# ── 🔴8 /crystals/approve 端点 ────────────────────────────
def test_crystals_approve_endpoint():
    import ducky.skill_crystallizer as sc
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ducky.routes_octopus import register_octopus_routes

    sc.init_crystallizer_schema()
    conn = sqlite3.connect(_TEST_DB)
    conn.execute(
        "INSERT INTO skill_crystals (skill_name, trigger_rule, procedure, status) "
        "VALUES ('approve-me', '触发', '步骤', 'candidate')"
    )
    conn.commit()
    cid = conn.execute("SELECT crystal_id FROM skill_crystals WHERE skill_name='approve-me'").fetchone()[0]
    conn.close()

    app = FastAPI()
    register_octopus_routes(app)
    client = TestClient(app)

    r = client.post("/crystals/approve", params={"crystal_id": cid})
    assert r.status_code == 200
    assert r.json()["new_status"] == "approved"

    rows = sc.list_crystals(status="approved")
    assert any(x["skill_name"] == "approve-me" for x in rows)
