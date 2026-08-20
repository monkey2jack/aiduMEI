"""aiduMEI v20 P0-2 — 信念层 opinions 的记忆库作用域测试

覆盖点：
1. opinions 表迁移补 (user_id, bank_id) 列，存量行回填 default/default、可重入
2. set_opinion 同事务内从所属 facts 行继承作用域戳（信念跟着事实走，
   调用方无权另报）；upsert 时刷新戳；事实不存在落 default/default
3. 老库 facts 没有作用域列时回退 default/default，写入不失败（v19 兼容）
"""
from __future__ import annotations

import os
import tempfile

import pytest

_TMPDIR = tempfile.mkdtemp(prefix="aidumem_v20_opinionscope_")

from ducky import utils

utils.FACTS_DB = os.path.join(_TMPDIR, "facts.db")

from ducky import opinion as opinion_mod  # noqa: E402

# v20 形状：facts 已完成 bank 迁移（有作用域列）
_FACTS_DDL_V20 = """
CREATE TABLE facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL DEFAULT 'general',
    fact_key TEXT NOT NULL,
    fact_value TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT 'default',
    bank_id TEXT NOT NULL DEFAULT 'default'
)
"""

# v19 形状：facts 还没有作用域列
_FACTS_DDL_V19 = """
CREATE TABLE facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL DEFAULT 'general',
    fact_key TEXT NOT NULL,
    fact_value TEXT NOT NULL
)
"""

# v19.4.0 原版 opinions 形状（无作用域列），迁移测试用
_OPINIONS_DDL_V19 = """
CREATE TABLE opinions (
    opinion_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_id     INTEGER NOT NULL,
    stance      TEXT NOT NULL CHECK(stance IN ('support', 'oppose', 'neutral')),
    confidence  REAL DEFAULT 0.5,
    evidence_ids TEXT DEFAULT '',
    source      TEXT NOT NULL,
    owner       TEXT NOT NULL,
    created_at  TEXT,
    updated_at  TEXT,
    UNIQUE(fact_id, source)
)
"""


def _conn():
    return utils.get_facts_conn()


def _opinion_row(fact_id: int, source: str):
    return _conn().execute(
        "SELECT * FROM opinions WHERE fact_id=? AND source=?", (fact_id, source)
    ).fetchone()


@pytest.fixture(autouse=True)
def _fresh_tables(monkeypatch):
    monkeypatch.setattr(utils, "FACTS_DB", os.path.join(_TMPDIR, "facts.db"))
    conn = _conn()
    conn.execute("DROP TABLE IF EXISTS opinions")
    conn.execute("DROP TABLE IF EXISTS facts")
    conn.execute(_FACTS_DDL_V20)
    conn.commit()
    yield


def test_opinions_migration_adds_scope_columns_and_backfills():
    """迁移放最前：v19 形状老表 + 存量行 → ensure 后补列、回填 default、可重入。"""
    conn = _conn()
    conn.execute(_OPINIONS_DDL_V19)
    conn.execute(
        "INSERT INTO opinions (fact_id, stance, confidence, source, owner) "
        "VALUES (1, 'support', 0.9, 'legacy-src', 'default')"
    )
    conn.commit()

    opinion_mod.ensure_opinion_schema()

    cols = [r[1] for r in _conn().execute("PRAGMA table_info(opinions)").fetchall()]
    assert "user_id" in cols and "bank_id" in cols

    row = _opinion_row(1, "legacy-src")
    assert row is not None, "迁移不许弄丢存量行"
    assert (row["user_id"], row["bank_id"]) == ("default", "default")
    assert row["confidence"] == pytest.approx(0.9)

    opinion_mod.ensure_opinion_schema()
    count = _conn().execute("SELECT COUNT(*) FROM opinions").fetchone()[0]
    assert count == 1


def test_set_opinion_inherits_scope_from_fact():
    conn = _conn()
    conn.execute(
        "INSERT INTO facts (fact_key, fact_value, user_id, bank_id) "
        "VALUES ('喜好', '甲库事实', 'user_x', 'bank_a')"
    )
    fact_a = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO facts (fact_key, fact_value, user_id, bank_id) "
        "VALUES ('喜好', '乙库事实', 'user_y', 'bank_b')"
    )
    fact_b = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()

    res = opinion_mod.set_opinion(fact_a, "support", confidence=0.8, source="obs-1")
    assert res["ok"] is True
    row = _opinion_row(fact_a, "obs-1")
    assert (row["user_id"], row["bank_id"]) == ("user_x", "bank_a")

    # 负向对照：另一库事实的信念盖的是另一库的戳，绝不许串
    res = opinion_mod.set_opinion(fact_b, "oppose", confidence=0.6, source="obs-1")
    assert res["ok"] is True
    row = _opinion_row(fact_b, "obs-1")
    assert (row["user_id"], row["bank_id"]) == ("user_y", "bank_b")

    # upsert 覆盖：同 (fact, source) 更新态度，戳保持跟随事实
    res = opinion_mod.set_opinion(fact_a, "neutral", confidence=0.5, source="obs-1")
    assert res["ok"] is True
    row = _opinion_row(fact_a, "obs-1")
    assert row["stance"] == "neutral"
    assert (row["user_id"], row["bank_id"]) == ("user_x", "bank_a")
    count = _conn().execute(
        "SELECT COUNT(*) FROM opinions WHERE fact_id=?", (fact_a,)
    ).fetchone()[0]
    assert count == 1, "upsert 不许长出第二行"

    # 事实不存在：写入照常成功，戳落 default/default
    res = opinion_mod.set_opinion(99999, "support", source="obs-2")
    assert res["ok"] is True
    row = _opinion_row(99999, "obs-2")
    assert (row["user_id"], row["bank_id"]) == ("default", "default")


def test_set_opinion_v19_facts_without_scope_falls_back_default():
    """老库 facts 没有作用域列：_fact_scope 查询失败要回退 default，写入不许炸。"""
    conn = _conn()
    conn.execute("DROP TABLE IF EXISTS facts")
    conn.execute(_FACTS_DDL_V19)
    conn.execute("INSERT INTO facts (fact_key, fact_value) VALUES ('老键', '老值')")
    old_fact = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()

    res = opinion_mod.set_opinion(old_fact, "support", confidence=0.7, source="old-src")
    assert res["ok"] is True, f"v19 老库写信念不许失败: {res}"
    row = _opinion_row(old_fact, "old-src")
    assert (row["user_id"], row["bank_id"]) == ("default", "default")
