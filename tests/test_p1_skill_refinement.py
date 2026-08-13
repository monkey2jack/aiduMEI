"""
tests/test_p1_skill_refinement.py — P1-2 技能精炼淘汰测试（v19.0）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
覆盖（对齐调研报告 6.3 P1-2 step 5「技能精炼」）：
  1. record_skill_use 成功/失败计数正确
  2. 失败后 low_utility 判定（成功率 < 阈值）
  3. prune_low_utility_skills 低效用自动标记 archived，不物理删除
  4. draft 草稿不受淘汰影响
  5. /crystals/use 与 /crystals/prune 路由
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import ducky.utils as utils

_tmp_dir = tempfile.mkdtemp(prefix="aidumem_p1_refine_test_")
_TEST_DB = os.path.join(_tmp_dir, "facts.db")
utils.FACTS_DB = _TEST_DB


@pytest.fixture(autouse=True)
def _bind_test_db():
    utils.FACTS_DB = _TEST_DB
    yield


def _fresh():
    import ducky.skill_crystallizer as sc
    conn = sqlite3.connect(_TEST_DB)
    conn.execute("DROP TABLE IF EXISTS skill_crystals")
    conn.commit()
    conn.close()
    return sc


def _seed_crystal(skill_name: str, status: str = "approved"):
    import ducky.skill_crystallizer as sc
    sc.init_crystallizer_schema()
    conn = sqlite3.connect(_TEST_DB)
    conn.execute(
        "INSERT INTO skill_crystals (skill_name, trigger_rule, procedure, status) VALUES (?, '触发', '步骤', ?)",
        (skill_name, status),
    )
    conn.commit()
    conn.close()


# ── 1. 复用计数 ──────────────────────────────────────────
def test_record_skill_use_counts():
    sc = _fresh()
    _seed_crystal("deploy-app")
    r1 = sc.record_skill_use("deploy-app", success=True)
    r2 = sc.record_skill_use("deploy-app", success=False)
    assert r1["status"] == "ok" and r2["status"] == "ok"
    assert r2["use_count"] == 2
    assert r2["success_count"] == 1
    assert r2["fail_count"] == 1
    assert r2["low_utility"] is False  # 2 次复用未到最小观察轮数


# ── 2. low_utility 判定 ──────────────────────────────────
def test_record_skill_use_low_utility_flag():
    sc = _fresh()
    _seed_crystal("flaky-skill")
    for _ in range(3):
        sc.record_skill_use("flaky-skill", success=False)
    r = sc.record_skill_use("flaky-skill", success=True)
    assert r["use_count"] == 4
    assert r["success_count"] == 1
    assert r["low_utility"] is True  # 1/4 = 25% < 34%


# ── 3. 低效用自动淘汰（不物理删除）────────────────────────
def test_prune_low_utility_skills():
    sc = _fresh()
    _seed_crystal("bad-skill")
    _seed_crystal("good-skill")
    for _ in range(4):
        sc.record_skill_use("bad-skill", success=False)
    for _ in range(4):
        sc.record_skill_use("good-skill", success=True)

    archived = sc.prune_low_utility_skills()
    names = {a["skill_name"] for a in archived}
    assert "bad-skill" in names
    assert "good-skill" not in names

    rows = sc.list_crystals(status="all")
    bad = next(r for r in rows if r["skill_name"] == "bad-skill")
    assert bad["status"] == "archived"
    assert bad["use_count"] == 4  # 数据仍在，只切状态


# ── 4. draft 草稿不受淘汰 ────────────────────────────────
def test_prune_skips_drafts():
    sc = _fresh()
    _seed_crystal("draft-skill", status="draft")
    for _ in range(4):
        sc.record_skill_use("draft-skill", success=False)
    archived = sc.prune_low_utility_skills()
    assert not any(a["skill_name"] == "draft-skill" for a in archived)
    rows = sc.list_crystals(status="all")
    d = next(r for r in rows if r["skill_name"] == "draft-skill")
    assert d["status"] == "draft"


# ── 5. 路由 ──────────────────────────────────────────────
def test_refinement_routes():
    sc = _fresh()
    _seed_crystal("route-skill")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ducky.routes_octopus import register_octopus_routes

    app = FastAPI()
    register_octopus_routes(app)
    client = TestClient(app)

    r = client.post("/crystals/use", params={"skill_name": "route-skill", "success": False})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["fail_count"] == 1

    r = client.post("/crystals/prune")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert "archived" in r.json()
