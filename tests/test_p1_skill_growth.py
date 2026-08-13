"""
tests/test_p1_skill_growth.py — P1-2 自动 Skill 生长测试
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
覆盖：
  1. 轨迹步骤不足 → 跳过
  2. 规则降级生成草稿（不启用 LLM）
  3. 草稿落库为 draft 状态，不自动 commit
  4. /skill/grow 路由
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import ducky.utils as utils

_tmp_dir = tempfile.mkdtemp(prefix="aidumem_p1_skill_test_")
_TEST_DB = os.path.join(_tmp_dir, "facts.db")
utils.FACTS_DB = _TEST_DB


@pytest.fixture(autouse=True)
def _bind_test_db():
    utils.FACTS_DB = _TEST_DB
    yield


def _fresh():
    import ducky.skill_growth as sg
    import ducky.skill_crystallizer as sc
    conn = sqlite3.connect(_TEST_DB)
    conn.execute("DROP TABLE IF EXISTS skill_crystals")
    conn.commit()
    conn.close()
    # skill_growth 通过 crystallizer 的 DDL 建表；无状态缓存，可直接初始化
    return sg


def test_too_few_steps_skipped():
    sg = _fresh()
    res = sg.grow_skill_from_trajectory(["步骤1", "步骤2"], task_name="demo", use_llm=False)
    assert res["status"] == "skipped"
    assert "有效步骤不足" in res["reason"]


def test_rule_based_draft_generated():
    sg = _fresh()
    res = sg.grow_skill_from_trajectory(
        ["检查服务状态", "备份数据库", "执行升级", "验证健康检查", "回滚预案"],
        task_name="deploy-dashboard",
        use_llm=False,
    )
    assert res["status"] == "ok"
    assert res["state"] == "draft"
    assert "## 步骤" in res["skill_md"]
    assert res["llm_used"] is False


def test_draft_recorded_as_draft():
    sg = _fresh()
    res = sg.grow_skill_from_trajectory(
        ["a", "b", "c", "d"], task_name="t", use_llm=False
    )
    assert res["status"] == "ok"
    drafts = sg.list_skill_drafts(status="draft")
    assert any(s["skill_name"] == res["skill_name"] for s in drafts)


def test_skill_grow_route():
    sg = _fresh()
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ducky.routes_p1 import register_p1_routes

    app = FastAPI()
    register_p1_routes(app)
    client = TestClient(app)

    r = client.post(
        "/skill/grow",
        json={"trajectory": ["a", "b", "c", "d"], "task_name": "demo", "use_llm": False},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["state"] == "draft"

    r = client.get("/skill/drafts")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
