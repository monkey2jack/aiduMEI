"""
tests/test_v20_autodream_bank_scope.py — v20 P0-2 AutoDream 蒸馏 bank 作用域回归
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

v19 的蒸馏管线三步全部无作用域：
  _get_recent_facts 扫全库 → _cluster_by_prefix 只按 fact_key 前缀聚类 →
  _simple_merge 按内容前 30 字符分组、把「输家」置 archived=1。
后果：乙库一条与甲库内容相同的事实会被当「重复」跨库归档——静默数据销毁。

v20 修复（本文件守住的四件事）：
  ① 扫描带出 (user_id, bank_id)，旧库缺列时退回 v19 查询并统一盖 default 戳
     （迁移未跑不哑掉、行为与 v19 一致）；
  ② 聚类键为 (user_id, bank_id, prefix)，合并组绝不跨域；
  ③ 全链路 trigger_dream：跨 bank 同内容事实两边都幸存；
  ④ 负向对照：同库重复照常合并归档（证明合并机器是活的，③不是假绿灯），
     且 autodream_log 的 reason 记下作用域可回溯。

跑法：cd <仓库根> && .venv/bin/pytest tests/test_v20_autodream_bank_scope.py -v
测试全部在临时 facts.db 上跑，绝不碰生产库。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from datetime import datetime

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── 关键：在导入任何业务模块之前把 DB 指向临时库 ──
_tmp_dir = tempfile.mkdtemp(prefix="aidumem_v20_dreamscope_")
_TEST_DB = os.path.join(_tmp_dir, "facts.db")

import ducky.utils as utils  # noqa: E402

utils.FACTS_DB = _TEST_DB
utils.TEXT_FTS_DB = os.path.join(_tmp_dir, "text_fts.db")

import ducky.autodream as autodream  # noqa: E402

# facts 最小必要结构：含作用域列（生产由联邦迁移补齐）与 archived/archived_at
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

# 内容前 30 字符是 _simple_merge 的分组键——两条内容必须共享同一 30 字符前缀
_P30 = "prefix-0123456789-abcdefghij-x"  # 恰好 30 字符
assert len(_P30) == 30


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
    # autodream_log 建表标记是全局的：全量合跑时别的模块可能已在自己的
    # 库上置过 True，这里强制在本文件的库上重建
    monkeypatch.setattr(autodream, "_table_checked", False)
    # 蒸馏报告写进临时目录，不污染仓库 logs/
    monkeypatch.setattr(
        autodream, "REPORT_FILE", os.path.join(_tmp_dir, "autodream_report.json")
    )
    yield


def _rows(sql: str, params=()):
    conn = sqlite3.connect(_TEST_DB)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _insert(key: str, value: str, user: str, bank: str, category: str = "梦区") -> int:
    conn = sqlite3.connect(_TEST_DB)
    try:
        cur = conn.execute(
            """INSERT INTO facts (category, fact_key, fact_value, user_id, bank_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (category, key, value, user, bank, datetime.now().isoformat()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _wipe_facts():
    conn = sqlite3.connect(_TEST_DB)
    try:
        conn.execute("DELETE FROM facts")
        conn.commit()
    finally:
        conn.close()


# ═══════════════ ① 扫描带作用域 ═══════════════
def test_recent_facts_carry_bank_scope():
    _wipe_facts()
    _insert("scan:k1", "甲库扫描内容", "u1", "bank_a")
    _insert("scan:k2", "乙库扫描内容", "u1", "bank_b")

    facts = autodream._get_recent_facts(days=7)
    assert len(facts) == 2
    scopes = {(f["user_id"], f["bank_id"]) for f in facts}
    assert scopes == {("u1", "bank_a"), ("u1", "bank_b")}, \
        "扫描没带出作用域列——下游聚类必然跨库"


def test_v19_db_without_scope_columns_falls_back_to_default(monkeypatch):
    """旧库缺作用域列：退回 v19 查询、统一盖 default 戳，蒸馏不哑掉。"""
    old_db = os.path.join(_tmp_dir, "facts_v19.db")
    conn = sqlite3.connect(old_db)
    try:
        conn.execute(
            """CREATE TABLE facts (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   category TEXT, fact_key TEXT, fact_value TEXT,
                   archived INTEGER DEFAULT 0, archived_at TIMESTAMP,
                   created_at TIMESTAMP)"""
        )
        conn.execute(
            "INSERT INTO facts (category, fact_key, fact_value, created_at) VALUES (?,?,?,?)",
            ("旧梦区", "old:k1", "升级前的旧事实", datetime.now().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(utils, "FACTS_DB", old_db)
    facts = autodream._get_recent_facts(days=7)
    assert len(facts) == 1, "旧库回退查询失败——迁移未跑时蒸馏整体哑掉"
    assert facts[0]["user_id"] == "default" and facts[0]["bank_id"] == "default", \
        "旧库事实必须归 default 域（与 facts 作用域回填口径一致）"


# ═══════════════ ② 聚类键含作用域（纯函数） ═══════════════
def test_cluster_key_never_spans_banks():
    same_prefix_key = "dream:cluster"
    facts = [
        {"id": 1, "fact_key": same_prefix_key, "content": "x",
         "user_id": "u1", "bank_id": "bank_a"},
        {"id": 2, "fact_key": same_prefix_key, "content": "x",
         "user_id": "u1", "bank_id": "bank_b"},
        {"id": 3, "fact_key": same_prefix_key, "content": "x",
         "user_id": "u2", "bank_id": "bank_a"},
    ]
    clusters = autodream._cluster_by_prefix(facts)
    assert len(clusters) == 3, \
        f"同前缀不同 (user, bank) 必须是三个独立簇，实得 {len(clusters)}"
    for (uid, bid, _prefix), members in clusters.items():
        assert all(m["user_id"] == uid and m["bank_id"] == bid for m in members), \
            "簇内混入了其他作用域的事实"

    # 负向对照：同域同前缀仍归同簇，聚类机器没被拆坏
    facts_same = [
        {"id": 4, "fact_key": "dream:a", "content": "x",
         "user_id": "u1", "bank_id": "bank_a"},
        {"id": 5, "fact_key": "dream:b", "content": "y",
         "user_id": "u1", "bank_id": "bank_a"},
    ]
    same = autodream._cluster_by_prefix(facts_same)
    assert len(same) == 1 and len(next(iter(same.values()))) == 2


# ═══════════════ ③+④ 全链路：跨库幸存 + 同库照常合并 ═══════════════
def test_dream_does_not_archive_across_banks():
    _wipe_facts()
    # 注意：不能复用 _P30——同库合并对也以它开头，30 字符分组会把
    # 跨库对卷进甲库的合并组，测的就不再是跨库问题了
    identical = "两库一字不差的同一句话用于跨库幸存判定"

    # 跨库同内容对：v19 会把其中一条当重复归档
    fid_a = _insert("dream:x1", identical, "u1", "bank_a")
    fid_b = _insert("dream:x2", identical, "u1", "bank_b")
    # 同库可合并对（共享 30 字符前缀，长者为胜者）：④ 的负向对照
    fid_short = _insert("dream:m1", _P30 + "-short", "u1", "bank_a")
    fid_long = _insert("dream:m2", _P30 + "-this-is-the-much-longer-one", "u1", "bank_a")
    # 凑够 trigger_dream 的最低事实数（<5 会 skip）
    _insert("filler:f1", "无关填充内容一", "u9", "bank_z")
    _insert("filler:f2", "完全不同的另一句填充", "u9", "bank_z")

    res = autodream.trigger_dream()
    assert res["status"] == "completed", f"蒸馏没跑起来: {res}"

    # ③ 跨库同内容两边都必须幸存
    for fid, tag in ((fid_a, "甲库"), (fid_b, "乙库")):
        archived = _rows("SELECT archived FROM facts WHERE id=?", (fid,))[0][0]
        assert archived == 0, f"{tag}的事实被跨库归档——v19 静默数据销毁复活"

    # ④ 负向对照：同库重复必须照常合并（否则上面的断言是假绿灯）
    assert _rows("SELECT archived FROM facts WHERE id=?", (fid_long,))[0][0] == 0, \
        "同库合并的胜者（更长内容）不该被归档"
    short_row = _rows(
        "SELECT archived, archived_at FROM facts WHERE id=?", (fid_short,)
    )[0]
    assert short_row[0] == 1, "同库重复未合并归档——合并机器死了"
    assert short_row[1], "归档行缺 archived_at 时间戳"
    assert res["stats"]["merged"] == 1, f"应恰好合并 1 条，实得 {res['stats']}"

    # 溯源：autodream_log 记下胜者与作用域
    log = _rows(
        "SELECT source_ids, target_id, reason FROM autodream_log WHERE action='merge'"
    )
    assert len(log) == 1
    assert json.loads(log[0][0]) == [fid_short] and log[0][1] == fid_long
    assert "u1/bank_a" in log[0][2], "合并溯源没记作用域"
