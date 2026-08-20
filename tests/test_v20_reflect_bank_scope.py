"""
tests/test_v20_reflect_bank_scope.py — v20 P0-2 Reflect 反思引擎 bank 作用域回归
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

v19 的反思链路全程无作用域：
  _gather_recent_facts 扫全库、_gather_recent_memories 不看 bank →
  乙库素材被蒸进洞察 → save_insights 落库无 bank → inject_reflections
  把乙库私密内容注入甲库的对话上下文——读侧泄漏 + 写侧污染双杀。

v20 修复（本文件守住的六件事）：
  ① reflections 旧表就地补 bank_id 列，存量行归 default 域零丢失，幂等；
  ② _gather_recent_facts 按 (user_id, bank_id) 取材；旧库缺列退回 v19
     查询不哑掉（全库本就是单一 default 域）；
  ③ save_insights 落库盖 bank 戳，去重按域——两库允许各持一条同文洞察；
     非法 bank_id 抛 BankScopeError 不静默；
  ④ get_reflections / inject_reflections opt-in 域过滤，不传 = v19 全量视图；
  ⑤ run_reflect 全链路：素材收集（记忆复筛口径与 /search 一致）与落库
     守同一 bank，乙库素材绝不进甲库的提示词；
  ⑥ HTTP 层 /reflect 系端点透传 bank_id，非法值报错不 500。

跑法：cd <仓库根> && .venv/bin/pytest tests/test_v20_reflect_bank_scope.py -v
测试全部在临时 facts.db 上跑，绝不碰生产库。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── 关键：在导入任何业务模块之前把 DB 指向临时库 ──
_tmp_dir = tempfile.mkdtemp(prefix="aidumem_v20_reflectscope_")
_TEST_DB = os.path.join(_tmp_dir, "facts.db")

import ducky.utils as utils  # noqa: E402

utils.FACTS_DB = _TEST_DB
utils.TEXT_FTS_DB = os.path.join(_tmp_dir, "text_fts.db")

import ducky.reflect as reflect  # noqa: E402
from ducky.bank_contract import BankScopeError  # noqa: E402

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

# v19 形状的 reflections 表：无 bank_id 列
_V19_REFLECTIONS_DDL = """
CREATE TABLE reflections (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL DEFAULT 'default',
    insight_type TEXT DEFAULT 'pattern',
    content      TEXT NOT NULL,
    confidence   REAL DEFAULT 0.5,
    evidence     TEXT DEFAULT '[]',
    source       TEXT DEFAULT 'manual',
    recorded_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
    # reflections 建表标记是全局的：全量合跑时别的模块可能已在自己的
    # 库上置过 True，这里强制在本文件的库上重建/迁移
    monkeypatch.setattr(reflect, "_checked", False)
    yield


def _rows(sql: str, params=()):
    conn = sqlite3.connect(_TEST_DB)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _insert_fact(key: str, value: str, user: str, bank: str, category: str = "思区"):
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
    try:
        conn.execute("DELETE FROM facts")
        try:
            conn.execute("DELETE FROM reflections")
        except sqlite3.OperationalError:
            pass  # 表还没建
        conn.commit()
    finally:
        conn.close()


# ═══════════════ ① v19 表迁移（先跑：会重建 reflections 表） ═══════════════
def test_reflections_migration_adds_bank_column():
    conn = sqlite3.connect(_TEST_DB)
    try:
        conn.execute("DROP TABLE IF EXISTS reflections")
        conn.executescript(_V19_REFLECTIONS_DDL)
        conn.execute(
            "INSERT INTO reflections (user_id, insight_type, content) "
            "VALUES ('default', 'pattern', '升级前就在库里的洞察')"
        )
        conn.commit()
    finally:
        conn.close()

    reflect.ensure_reflect_schema()

    cols = {r[1] for r in _rows("PRAGMA table_info(reflections)")}
    assert "bank_id" in cols, "迁移没给 reflections 补 bank_id 列"
    row = _rows(
        "SELECT bank_id, content FROM reflections WHERE content='升级前就在库里的洞察'"
    )
    assert row == [("default", "升级前就在库里的洞察")], \
        "v19 存量洞察必须零丢失地归入 default 域"

    # 幂等：再跑一次不得重复迁移或报错
    reflect._checked = False
    reflect.ensure_reflect_schema()
    assert _rows("SELECT COUNT(*) FROM reflections")[0][0] == 1


# ═══════════════ ② 事实取材分域 + 旧库回退 ═══════════════
def test_gather_recent_facts_scoped_with_v19_fallback(monkeypatch):
    _wipe()
    _insert_fact("rf:a1", "甲库的部署面板事实", "u1", "bank_a")
    _insert_fact("rf:b1", "乙库的告警队列事实", "u1", "bank_b")
    _insert_fact("rf:c1", "别人家的事实", "u2", "bank_a")

    facts_a = reflect._gather_recent_facts(10, user_id="u1", bank_id="bank_a")
    assert [f["key"] for f in facts_a] == ["rf:a1"], \
        f"甲库取材混入他域: {[f['key'] for f in facts_a]}"
    assert all("乙库" not in f["text"] for f in facts_a), "乙库事实被蒸进甲库素材"

    # 负向对照：乙库自己取材必须取得到（分域没把取材做死）
    facts_b = reflect._gather_recent_facts(10, user_id="u1", bank_id="bank_b")
    assert [f["key"] for f in facts_b] == ["rf:b1"], "乙库取材失效——反思整体哑掉"

    # 旧库缺作用域列：退回 v19 查询、不哑掉
    old_db = os.path.join(_tmp_dir, "facts_v19.db")
    conn = sqlite3.connect(old_db)
    try:
        conn.execute(
            """CREATE TABLE facts (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   category TEXT, fact_key TEXT, fact_value TEXT,
                   archived INTEGER DEFAULT 0,
                   created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                   updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""
        )
        conn.execute(
            "INSERT INTO facts (category, fact_key, fact_value) "
            "VALUES ('旧思区', 'old:k1', '升级前的旧事实')"
        )
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(utils, "FACTS_DB", old_db)
    facts_old = reflect._gather_recent_facts(10, user_id="u1", bank_id="bank_a")
    assert len(facts_old) == 1 and facts_old[0]["key"] == "old:k1", \
        "旧库回退查询失败——迁移未跑时反思整体哑掉"


# ═══════════════ ③ 落库盖戳 + 分域去重 + 非法域拒绝 ═══════════════
def test_save_insights_stamps_bank_and_dedups_per_bank():
    _wipe()
    ins = [{"type": "pattern", "content": "用户近期反复关注部署面板", "confidence": 0.8}]

    assert reflect.save_insights(ins, "u1", "test", bank_id="bank_a") == 1
    # 同域同文：去重收敛
    assert reflect.save_insights(ins, "u1", "test", bank_id="bank_a") == 0, \
        "同域同文洞察未去重"
    # 异域同文：两库各持一条，互不吞并
    assert reflect.save_insights(ins, "u1", "test", bank_id="bank_b") == 1, \
        "跨域去重把乙库自己的洞察吞了"
    # 不传 bank = default 域（v19 行为）
    assert reflect.save_insights(ins, "u1", "test") == 1

    banks = sorted(r[0] for r in _rows(
        "SELECT bank_id FROM reflections WHERE user_id='u1'"
    ))
    assert banks == ["bank_a", "bank_b", "default"], f"落库戳错: {banks}"

    # 非法 bank_id：抛作用域异常，不静默落错域
    with pytest.raises(BankScopeError):
        reflect.save_insights(ins, "u1", "test", bank_id="../etc")


# ═══════════════ ④ 读侧 opt-in 过滤 + 注入链路 ═══════════════
def test_get_reflections_bank_filter_and_inject():
    _wipe()
    reflect.save_insights(
        [{"type": "pattern", "content": "甲库的私密洞察内容", "confidence": 0.9}],
        "u1", "test", bank_id="bank_a",
    )
    reflect.save_insights(
        [{"type": "pattern", "content": "乙库的私密洞察内容", "confidence": 0.9}],
        "u1", "test", bank_id="bank_b",
    )

    rows_a = reflect.get_reflections("u1", bank_id="bank_a")
    assert [r["content"] for r in rows_a] == ["甲库的私密洞察内容"], \
        "bank 过滤失效——乙库洞察对甲库可读"

    # 不传 = v19 全量视图（管理员口径，存量零改动）
    rows_all = reflect.get_reflections("u1")
    assert {r["content"] for r in rows_all} == {"甲库的私密洞察内容", "乙库的私密洞察内容"}

    # 注入链路：这是泄漏的最后一跳，必须只带本域洞察
    ctx = reflect.inject_reflections("u1", bank_id="bank_a")
    assert "甲库的私密洞察内容" in ctx, "本域洞察没注入——反思白做"
    assert "乙库的私密洞察内容" not in ctx, "乙库洞察注入了甲库对话——泄漏复活"


# ═══════════════ ⑤ run_reflect 全链路作用域 ═══════════════
def test_run_reflect_full_chain_scoped(monkeypatch):
    _wipe()
    _insert_fact("rr:a1", "甲库独有的事实素材", "u1", "bank_a")
    _insert_fact("rr:b1", "乙库独有的事实素材", "u1", "bank_b")

    class _FakeMem:
        """mem0 替身：get_all 返回两域各一条记忆，考验复筛。"""

        def get_all(self, **kwargs):
            return {"results": [
                {"id": "ma", "memory": "甲库独有的记忆素材", "user_id": "u1",
                 "metadata": {"bank_id": "bank_a"}},
                {"id": "mb", "memory": "乙库独有的记忆素材", "user_id": "u1",
                 "metadata": {"bank_id": "bank_b"}},
            ]}

    prompts: list[str] = []

    def _fake_llm(prompt, **kwargs):
        prompts.append(prompt)
        return json.dumps([{
            "type": "pattern", "content": "甲库素材蒸出的洞察",
            "confidence": 0.8, "evidence": ["m1"],
        }], ensure_ascii=False)

    monkeypatch.setattr(reflect, "call_llm", _fake_llm)

    res = reflect.run_reflect(memory=_FakeMem(), user_id="u1", source="test",
                              bank_id="bank_a")
    assert res["status"] == "ok" and res["saved"] == 1, res

    # 提示词只许含甲库素材——乙库内容进了提示词，洞察就已经被污染
    assert len(prompts) == 1
    assert "甲库独有的记忆素材" in prompts[0], "本域记忆没进提示词——反思素材断供"
    assert "甲库独有的事实素材" in prompts[0], "本域事实没进提示词——反思素材断供"
    assert "乙库独有的记忆素材" not in prompts[0], "乙库记忆蒸进了甲库反思"
    assert "乙库独有的事实素材" not in prompts[0], "乙库事实蒸进了甲库反思"

    # 洞察落库必须盖发起域的戳
    row = _rows("SELECT bank_id FROM reflections WHERE content='甲库素材蒸出的洞察'")
    assert row == [("bank_a",)], f"洞察落库戳错域: {row}"


# ═══════════════ ⑥ HTTP 端点透传 ═══════════════
def test_reflect_routes_scoped():
    _wipe()
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from ducky.routes_p0 import register_p0_routes

    app = FastAPI()
    register_p0_routes(app)
    client = TestClient(app)

    # 非法 bank_id：报错 dict，不许 500（BankScopeError 被路由层接住）
    r_bad = client.post("/reflect", json={"user_id": "u1", "bank_id": "../etc"})
    assert r_bad.status_code == 200, r_bad.text
    assert r_bad.json()["status"] == "error", "非法 bank_id 未被作用域契约拦下"

    # 种子洞察分两域，验证 list / context 的透传过滤
    reflect.save_insights(
        [{"type": "pattern", "content": "甲库路由洞察", "confidence": 0.9}],
        "u1", "test", bank_id="bank_a",
    )
    reflect.save_insights(
        [{"type": "pattern", "content": "乙库路由洞察", "confidence": 0.9}],
        "u1", "test", bank_id="bank_b",
    )

    r_list = client.get("/reflect/list", params={"user_id": "u1", "bank_id": "bank_a"})
    assert r_list.status_code == 200
    contents = [i["content"] for i in r_list.json()["insights"]]
    assert contents == ["甲库路由洞察"], f"/reflect/list bank 过滤失效: {contents}"

    # 不传 = v19 全量视图
    r_all = client.get("/reflect/list", params={"user_id": "u1"})
    assert {i["content"] for i in r_all.json()["insights"]} == {"甲库路由洞察", "乙库路由洞察"}

    r_ctx = client.get("/reflect/context", params={"user_id": "u1", "bank_id": "bank_b"})
    ctx = r_ctx.json()["context"]
    assert "乙库路由洞察" in ctx and "甲库路由洞察" not in ctx, \
        "/reflect/context 的 bank 过滤失效——洞察跨库注入"
