"""
tests/test_v20_scenes_bank_scope.py — v20 P0-2 场景聚类 bank 作用域回归
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

v19 的场景管线全程无作用域：
  _cluster_scenes_impl 全库混跑相似度聚类 → 乙库一条与甲库相似的事实被
  卷进同一场景，scene.summary 直接把甲库的 fact_value 泄给乙库读者；
  scenes 表 member_keys 还是全局行内 UNIQUE——乙库场景会被甲库同键吞掉。

v20 修复（本文件守住的四件事）：
  ① scenes 表迁移：补 (user_id, bank_id) 列，唯一约束改为
     (user_id, bank_id, member_keys) 分域去重，旧行归 default 域零丢失；
  ② 聚类永远按 (user_id, bank_id) 分域跑，场景成员绝不跨库；
     负向对照：同库相似事实照常聚出场景（聚类机器是活的）；
  ③ /scene/cluster 可传作用域只聚该域，非法 bank_id 报错不 500；
  ④ /scene 列表可按作用域过滤，不传 = v19 全量视图（存量零改动）。

跑法：cd <仓库根> && .venv/bin/pytest tests/test_v20_scenes_bank_scope.py -v
测试全部在临时 DB 上跑，绝不碰生产库。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── 关键：在导入任何业务模块之前把 DB 指向临时库 ──
_tmp_dir = tempfile.mkdtemp(prefix="aidumem_v20_scenescope_")
_TEST_DB = os.path.join(_tmp_dir, "facts.db")
_SCENES_DB = os.path.join(_tmp_dir, "scenes.db")

import ducky.utils as utils  # noqa: E402

utils.FACTS_DB = _TEST_DB
utils.SCENES_DB = _SCENES_DB
utils.TEXT_FTS_DB = os.path.join(_tmp_dir, "text_fts.db")

import ducky.hot.legacy_helpers as legacy_helpers  # noqa: E402

_FACTS_DDL = """
CREATE TABLE facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL DEFAULT 'general',
    fact_key TEXT NOT NULL,
    fact_value TEXT NOT NULL,
    agent_id TEXT DEFAULT 'local',
    user_id TEXT NOT NULL DEFAULT 'default',
    bank_id TEXT NOT NULL DEFAULT 'default',
    archived INTEGER DEFAULT 0,
    archived_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# v19 形状的 scenes 表：member_keys 全局行内 UNIQUE、无作用域列
_V19_SCENES_DDL = """
CREATE TABLE scenes (
    scene_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    category    TEXT DEFAULT '',
    summary     TEXT DEFAULT '',
    member_keys TEXT DEFAULT '' UNIQUE,
    member_count INTEGER DEFAULT 0,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""


@pytest.fixture(scope="module", autouse=True)
def _bootstrap_db():
    utils.FACTS_DB = _TEST_DB
    conn = sqlite3.connect(_TEST_DB)
    conn.executescript(_FACTS_DDL)
    conn.commit()
    conn.close()
    yield


@pytest.fixture(autouse=True)
def _bind_test_db(monkeypatch):
    utils.FACTS_DB = _TEST_DB
    utils.SCENES_DB = _SCENES_DB
    # legacy_helpers 在 import 期定格了 DB 路径，这里指回本文件临时库
    monkeypatch.setattr(legacy_helpers, "FACTS_DB", _TEST_DB)
    monkeypatch.setattr(legacy_helpers, "SCENES_DB", _SCENES_DB)
    yield


def _facts_rows(sql: str, params=()):
    conn = sqlite3.connect(_TEST_DB)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _scene_rows(sql: str, params=()):
    conn = sqlite3.connect(_SCENES_DB)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _insert_fact(key: str, value: str, user: str, bank: str, category: str = "场区"):
    conn = sqlite3.connect(_TEST_DB)
    try:
        conn.execute(
            """INSERT INTO facts (category, fact_key, fact_value, user_id, bank_id)
               VALUES (?, ?, ?, ?, ?)""",
            (category, key, value, user, bank),
        )
        conn.commit()
    finally:
        conn.close()


def _wipe():
    conn = sqlite3.connect(_TEST_DB)
    conn.execute("DELETE FROM facts")
    conn.commit()
    conn.close()
    conn = sqlite3.connect(_SCENES_DB)
    try:
        conn.execute("DELETE FROM scenes")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # 表还没建
    finally:
        conn.close()


# ═══════════════ ① v19 表迁移（先跑：会重建 scenes 表） ═══════════════
def test_scenes_migration_rebuilds_v19_table():
    conn = sqlite3.connect(_SCENES_DB)
    try:
        conn.execute("DROP TABLE IF EXISTS scenes")
        conn.execute(_V19_SCENES_DDL)
        conn.execute(
            "INSERT INTO scenes (category, summary, member_keys, member_count) "
            "VALUES ('存量区', '升级前就在库里的场景', 'lk1|lk2', 2)"
        )
        conn.commit()

        legacy_helpers._ensure_scenes_table(conn)

        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='scenes'"
        ).fetchone()[0]
        assert "bank_id" in sql, "迁移没补作用域列"
        assert "UNIQUE(user_id, bank_id, member_keys)" in sql, \
            "唯一约束必须分域——全局 UNIQUE 会让乙库场景被甲库同键吞掉"
        assert not conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='scenes_v19_migrating'"
        ).fetchone(), "迁移中间表没清理"

        row = conn.execute(
            "SELECT user_id, bank_id, summary FROM scenes WHERE member_keys='lk1|lk2'"
        ).fetchone()
        assert row == ("default", "default", "升级前就在库里的场景"), \
            "v19 存量场景必须零丢失地归入 default 域"

        # 分域唯一：同 member_keys 在另一 bank 可共存，同域重复被 OR IGNORE 收敛
        conn.execute(
            "INSERT OR IGNORE INTO scenes (category, summary, member_keys, member_count, user_id, bank_id) "
            "VALUES ('存量区', '乙库同键场景', 'lk1|lk2', 2, 'u1', 'bank_b')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO scenes (category, summary, member_keys, member_count, user_id, bank_id) "
            "VALUES ('存量区', '乙库同键重复', 'lk1|lk2', 2, 'u1', 'bank_b')"
        )
        conn.commit()
        n = conn.execute(
            "SELECT COUNT(*) FROM scenes WHERE member_keys='lk1|lk2'"
        ).fetchone()[0]
        assert n == 2, f"应为 default 域 1 行 + bank_b 1 行，实得 {n}"
    finally:
        conn.close()

    # 幂等：再跑一次不得重复迁移或报错
    conn = sqlite3.connect(_SCENES_DB)
    try:
        legacy_helpers._ensure_scenes_table(conn)
        assert conn.execute("SELECT COUNT(*) FROM scenes").fetchone()[0] == 2
    finally:
        conn.close()


# ═══════════════ ② 聚类分域 + 同库负向对照 ═══════════════
def test_cluster_scenes_never_mixes_banks():
    _wipe()
    same_value = "用户每天早上先看部署面板再处理告警队列"
    _insert_fact("a_k1", same_value, "u1", "bank_a")
    _insert_fact("a_k2", same_value, "u1", "bank_a")
    # 乙库同内容、但库内无相似伙伴：v19 会把它卷进甲库的场景
    _insert_fact("b_k1", same_value, "u1", "bank_b")

    res = legacy_helpers._cluster_scenes_impl(dry_run=False)
    assert res["status"] == "ok", res

    rows = _scene_rows("SELECT member_keys, user_id, bank_id, summary FROM scenes")
    assert rows, "同库相似事实没聚出场景——聚类机器死了，后面的断言是假绿灯"
    for member_keys, uid, bid, _summary in rows:
        assert "b_k1" not in member_keys, \
            f"乙库事实被卷进 {bid} 的场景 {member_keys}——跨库泄漏复活"
        assert (uid, bid) == ("u1", "bank_a"), f"场景作用域错标: {(uid, bid)}"

    # 负向对照：乙库补一条相似事实 → 乙库自己的场景必须聚得出来
    _insert_fact("b_k2", same_value, "u1", "bank_b")
    legacy_helpers._cluster_scenes_impl(dry_run=False)
    b_rows = _scene_rows(
        "SELECT member_keys FROM scenes WHERE user_id='u1' AND bank_id='bank_b'"
    )
    assert b_rows, "乙库同库相似事实没聚出场景——分域把聚类做死了"
    assert all("a_k" not in r[0] for r in b_rows), "乙库场景混入甲库成员"


# ═══════════════ ③+④ 端点全链路 ═══════════════
def test_scene_endpoints_scoped():
    _wipe()
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from ducky.hot.legacy_routes import register_legacy_routes

    app = FastAPI()
    register_legacy_routes(app)
    client = TestClient(app)

    pair = "助手在周末会把记忆库整理归档一遍"
    _insert_fact("ep_a1", pair, "u1", "bank_a")
    _insert_fact("ep_a2", pair, "u1", "bank_a")
    _insert_fact("ep_b1", pair, "u1", "bank_b")
    _insert_fact("ep_b2", pair, "u1", "bank_b")

    # ③ 只聚乙库：甲库不得产出场景
    r = client.post("/scene/cluster", params={
        "dry_run": "false", "user_id": "u1", "bank_id": "bank_b",
    })
    assert r.status_code == 200 and r.json()["status"] == "ok", r.text
    assert not _scene_rows("SELECT 1 FROM scenes WHERE bank_id='bank_a'"), \
        "指定只聚 bank_b 却写出了 bank_a 的场景"
    assert _scene_rows("SELECT 1 FROM scenes WHERE bank_id='bank_b'"), \
        "指定聚 bank_b 却没产出场景"

    # 非法 bank_id：报错 dict，不许 500
    r_bad = client.post("/scene/cluster", params={
        "dry_run": "false", "bank_id": "../etc",
    })
    assert r_bad.status_code == 200, r_bad.text
    assert r_bad.json()["status"] == "error", "非法 bank_id 未被作用域契约拦下"

    # 不传作用域 = 各域独立聚类，甲库这次要聚出来
    r_all = client.post("/scene/cluster", params={"dry_run": "false"})
    assert r_all.status_code == 200 and r_all.json()["status"] == "ok"
    assert _scene_rows("SELECT 1 FROM scenes WHERE bank_id='bank_a'"), \
        "全量聚类没覆盖 bank_a——后台 12h 任务会漏库"

    # ④ 列表过滤：传 bank 只看本库；不传 = v19 全量视图
    r_list = client.get("/scene", params={"bank_id": "bank_b", "limit": 50})
    assert r_list.status_code == 200
    scenes_b = r_list.json()["scenes"]
    assert scenes_b and all(s["bank_id"] == "bank_b" for s in scenes_b), \
        "/scene 的 bank 过滤失效——场景摘要跨库可读"
    r_full = client.get("/scene", params={"limit": 50})
    banks = {s["bank_id"] for s in r_full.json()["scenes"]}
    assert {"bank_a", "bank_b"} <= banks, "全量视图丢场景——过滤污染了默认行为"
