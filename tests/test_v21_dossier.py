"""
tests/test_v21_dossier.py — v21 F3 记忆档案导出守卫
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
验收（任务书 §五-4.3，红→绿）：
  1. render_markdown：七章齐备；reasoned 带「未经验证」标注；纯渲染零数据库
  2. build_dossier_data：按域收窄、按出身分区、跨域不可见
  3. GET /dossier：缺/空 bank_id 400；正域 200 且分区正确；download=1 带附件头
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp_dir = tempfile.mkdtemp(prefix="aidumem_v21_dossier_")

import ducky.utils as utils  # noqa: E402

utils.FACTS_DB = os.path.join(_tmp_dir, "facts.db")


@pytest.fixture(autouse=True)
def setup_test_db():
    fd, db_path = tempfile.mkstemp(prefix="facts_", suffix=".db", dir=_tmp_dir)
    os.close(fd)
    utils.FACTS_DB = db_path
    from ducky.schema_bootstrap import ensure_core_schema
    ensure_core_schema(force=True)
    yield


# ── 1. 纯渲染 ───────────────────────────────────────────────────────────────

def test_render_has_all_chapters_and_unverified_marks():
    from ducky.dossier import render_markdown
    md = render_markdown({
        "user_id": "dudu", "bank_id": "default",
        "sections": {
            "health": {"facts_active": 3, "facts_archived": 1,
                       "epistemic_dist": {"user_provided": 2, "reasoned": 1},
                       "top_categories": [("general", 3)], "evolution_rows": 2,
                       "reflection_candidates": {"pending": 1}},
            "profile": [("general", "口味", "喜欢拿铁", 100, "t")],
            "learned": [("general", "推测", "可能重视竞赛", 60, "t")],
            "opinions": [], "scenes": [], "crystals": [], "evolve": {},
        },
    })
    for chapter in ("一、记忆健康总览", "二、用户画像", "三、AI 自己学到的",
                    "四、立场与观点", "五、场景聚类", "六、技能结晶", "七、检索进化"):
        assert chapter in md, f"缺章节: {chapter}"
    assert "可能重视竞赛" in md and "未经验证" in md
    assert "user_provided 2" in md and "reasoned 1" in md


# ── 2. 数据聚合（域收窄）────────────────────────────────────────────────────

def _seed_facts():
    conn = utils.get_facts_conn()
    rows = [
        ("general", "口味", "喜欢拿铁", "dudu", "default", "user_provided"),
        ("general", "推测", "可能重视竞赛", "dudu", "default", "reasoned"),
        ("general", "别家", "alice 的秘密", "alice", "work", "user_provided"),
    ]
    for cat, k, v, u, b, epi in rows:
        conn.execute(
            "INSERT INTO facts (category, fact_key, fact_value, user_id, bank_id, agent_id, epistemic_mode) "
            "VALUES (?,?,?,?,?,?,?)", (cat, k, v, u, b, u, epi))
    conn.commit()
    conn.close()


def test_build_data_partitions_by_epistemic_and_scope():
    _seed_facts()
    from ducky.dossier import build_dossier_data
    data = build_dossier_data("dudu", "default")
    s = data["sections"]
    assert len(s["profile"]) == 1 and s["profile"][0][2] == "喜欢拿铁"
    assert len(s["learned"]) == 1 and s["learned"][0][2] == "可能重视竞赛"
    # 跨域零泄漏
    blob = str(s)
    assert "alice 的秘密" not in blob
    assert s["health"]["facts_active"] == 2


# ── 3. 端点 ─────────────────────────────────────────────────────────────────

def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ducky.routes_knowledge import register_knowledge_routes
    app = FastAPI()
    register_knowledge_routes(app)
    return TestClient(app)


def test_dossier_endpoint_requires_explicit_scope():
    _seed_facts()
    c = _client()
    assert c.get("/dossier").status_code == 400
    assert c.get("/dossier?user_id=dudu").status_code == 400
    assert c.get("/dossier?bank_id=default").status_code == 400


def test_dossier_endpoint_ok_and_download():
    _seed_facts()
    c = _client()
    r = c.get("/dossier?user_id=dudu&bank_id=default")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert "喜欢拿铁" in r.text and "alice 的秘密" not in r.text
    r2 = c.get("/dossier?user_id=dudu&bank_id=default&download=1")
    assert "attachment" in r2.headers.get("content-disposition", "")
    assert "dossier_dudu_default.md" in r2.headers["content-disposition"]


def test_scope_hint_returns_server_default_scope():
    c = _client()
    r = c.get("/dossier/scope-hint")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["user_id"] and body["bank_id"], "域提示必须给出非空默认域"
