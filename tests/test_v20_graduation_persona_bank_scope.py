"""aiduMEI v20 P0-2 — Instinct 毕业链 + Persona 统计的记忆库作用域测试

v19 的两处读泄漏收口：

毕业链（ducky/instinct_graduation.py + routes_v8 /graduate）：
  v19 的 scan/graduate 全库 get_all → 跨库分组、跨库删除源记忆、
  蒸馏出的 skill 不带域戳（掉进 default 域，等于把甲库知识蒸给了所有人）。
  v20：整条链锁在 (user_id, bank_id) 域内——
  ① 命名域把 bank_id 下推进 filters 且复筛掉他库/存量项；
  ② 默认域保持 v19 filters 形状（不下推），复筛掉命名域的点；
  ③ 非法 bank_id 在取数前就抛 BankScopeError（负向对照：一次 get_all 都不发）；
  ④ 蒸馏出的 skill 元数据盖本域戳，memory.add 用规范化后的 user_id；
  ⑤ 删除的 source_ids 只来自复筛后的本域项——他库记忆永远不会被毕业删掉；
  ⑥ /graduate 路由递进 bank_id，BankScopeError 包成 {"status":"error"} 不抛 500。

Persona（legacy_routes _refresh_persona_inline + /persona + /persona/refresh）：
  v19 无任何作用域——facts_count 是全库计数（统计面泄漏）。
  v20：opt-in 作用域过滤；不传 = v19 管理员全量视图（存量零改动）。

跑法：cd <仓库根> && .venv/bin/pytest tests/test_v20_graduation_persona_bank_scope.py -v
测试全部在临时 DB / 假 mem 上跑，绝不碰生产库、绝不调 LLM。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── 关键：在导入任何业务模块之前把 DB 指向临时库 ──
_tmp_dir = tempfile.mkdtemp(prefix="aidumem_v20_gradpersona_")
_TEST_DB = os.path.join(_tmp_dir, "facts.db")

import ducky.utils as utils  # noqa: E402

utils.FACTS_DB = _TEST_DB
utils.SCENES_DB = os.path.join(_tmp_dir, "scenes.db")
utils.TEXT_FTS_DB = os.path.join(_tmp_dir, "text_fts.db")

import ducky.hot.legacy_helpers as legacy_helpers  # noqa: E402
import ducky.instinct_graduation as graduation  # noqa: E402
from ducky.bank_contract import BankScopeError  # noqa: E402

_FACTS_DDL = """
CREATE TABLE facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL DEFAULT 'general',
    fact_key TEXT NOT NULL,
    fact_value TEXT NOT NULL,
    trust_score REAL DEFAULT 0.5,
    user_id TEXT NOT NULL DEFAULT 'default',
    bank_id TEXT NOT NULL DEFAULT 'default',
    archived INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


@pytest.fixture(scope="module", autouse=True)
def _bootstrap_db():
    conn = sqlite3.connect(_TEST_DB)
    conn.executescript(_FACTS_DDL)
    conn.commit()
    conn.close()
    yield


@pytest.fixture(autouse=True)
def _bind_test_db(monkeypatch):
    monkeypatch.setattr(utils, "FACTS_DB", _TEST_DB)
    monkeypatch.setattr(legacy_helpers, "FACTS_DB", _TEST_DB)
    conn = sqlite3.connect(_TEST_DB)
    conn.execute("DELETE FROM facts")
    conn.commit()
    conn.close()
    yield


class _FakeMem:
    """极简 mem0 替身：无视 filters 返回全部种子项（模拟「下推失效」的最坏
    后端），并记录每次调用收到的 filters 和所有 add/delete——
    「下推没下推、删没删错人」一目了然。"""

    def __init__(self, items):
        self.items = items
        self.get_all_filters: list[dict] = []
        self.added: list[dict] = []
        self.deleted: list[str] = []

    def get_all(self, filters=None, limit=10000, **kw):
        self.get_all_filters.append(dict(filters or {}))
        return {"results": [dict(i) for i in self.items]}

    def add(self, messages, user_id=None, metadata=None, **kw):
        self.added.append({"messages": messages, "user_id": user_id,
                           "metadata": dict(metadata or {})})
        return {"results": []}

    def delete(self, memory_id):
        self.deleted.append(memory_id)


def _seed_items():
    """同一 category「打卡」三个域各 3 条：分组数量刚好踩在 MIN_GROUP_SIZE 上，
    任何一条跨库泄漏都会改变分组计数或删除清单。"""
    items = []
    for i in range(3):
        items.append({"id": f"a_{i}_0000000000000000", "memory": f"甲库打卡记录{i}",
                      "metadata": {"category": "打卡", "bank_id": "bank_a"}})
    for i in range(3):
        items.append({"id": f"b_{i}_0000000000000000", "memory": f"乙库打卡记录{i}",
                      "metadata": {"category": "打卡", "bank_id": "bank_b"}})
    for i in range(3):
        items.append({"id": f"legacy_{i}_0000000000", "memory": f"存量打卡记录{i}",
                      "metadata": {"category": "打卡"}})
    return items


# ═══════════════ ① scan：命名域下推 + 复筛 ═══════════════
def test_graduation_scan_named_bank_pushes_filter_and_rescreens():
    mem = _FakeMem(_seed_items())
    groups = graduation.scan_instincts(mem, "user_x", "bank_a")

    assert mem.get_all_filters == [{"user_id": "user_x", "bank_id": "bank_a"}], \
        "命名域必须把 bank_id 下推进 filters"
    assert len(groups) == 1 and groups[0]["category"] == "打卡"
    assert groups[0]["count"] == 3, \
        f"甲库只有 3 条，计数 {groups[0]['count']} 说明他库/存量项漏进了分组"
    assert all(sid.startswith("a_") for sid in groups[0]["sample_ids"]), \
        "毕业候选样本混入了他库/存量记忆"


# ═══════════════ ② scan：默认域保持 v19 形状但复筛 ═══════════════
def test_graduation_scan_default_bank_keeps_v19_shape_but_rescreens():
    mem = _FakeMem(_seed_items())
    groups = graduation.scan_instincts(mem, "user_x")  # 不传 = default 域

    assert mem.get_all_filters == [{"user_id": "user_x"}], \
        "默认域 filters 必须保持 v19 形状（无 bank_id 键，存量向量无此字段）"
    assert len(groups) == 1 and groups[0]["count"] == 3, \
        "默认域应只见 3 条存量记忆（正向对照）"
    assert all(sid.startswith("legacy_") for sid in groups[0]["sample_ids"]), \
        "命名域的记忆漏进了默认域的毕业候选"


# ═══════════════ ③ 非法作用域：取数前就炸 ═══════════════
def test_graduation_invalid_scope_raises_before_fetch():
    mem = _FakeMem(_seed_items())
    with pytest.raises(BankScopeError):
        graduation.scan_instincts(mem, "user_x", "../etc")
    with pytest.raises(BankScopeError):
        graduation.graduate_to_skill(mem, "user_x", {"category": "打卡"}, "../etc")
    with pytest.raises(BankScopeError):
        graduation.auto_graduate(mem, "a/b")
    assert mem.get_all_filters == [], \
        "非法作用域不许发出任何 get_all（负向对照）"
    assert mem.added == [] and mem.deleted == []


# ═══════════════ ④ graduate：skill 盖戳 + 只删本域 ═══════════════
def test_graduation_skill_stamped_and_foreign_never_deleted(monkeypatch):
    monkeypatch.setattr(graduation, "_call_llm",
                        lambda prompt, max_tokens=512: "蒸馏后的打卡技能")
    mem = _FakeMem(_seed_items())

    out = graduation.graduate_to_skill(
        mem, "user_x", {"category": "打卡", "count": 3}, "bank_a")
    assert out, "本域 3 条同类记忆必须毕业成功（正向对照）"

    assert len(mem.added) == 1
    skill = mem.added[0]
    assert skill["user_id"] == "user_x"
    assert skill["metadata"]["bank_id"] == "bank_a", \
        "蒸馏出的 skill 必须盖本域戳——否则甲库知识掉进 default 域人人可见"
    assert skill["metadata"]["level"] == "skill"
    assert all(sid.startswith("a_") for sid in skill["metadata"]["source_ids"]), \
        "skill 溯源链混入了他库/存量记忆 id"

    assert mem.deleted and all(sid.startswith("a_") for sid in mem.deleted), \
        f"毕业删除越库了: {mem.deleted}——他库记忆被甲库的毕业动作销毁"
    assert set(mem.deleted) == set(skill["metadata"]["source_ids"]), \
        "删除清单必须与溯源链一致"


def test_graduation_default_domain_deletes_only_legacy(monkeypatch):
    """默认域毕业：只吃存量（无戳）记忆，skill 也按约定盖 default 戳。"""
    monkeypatch.setattr(graduation, "_call_llm",
                        lambda prompt, max_tokens=512: "存量打卡技能")
    mem = _FakeMem(_seed_items())

    out = graduation.graduate_to_skill(
        mem, "user_x", {"category": "打卡", "count": 3}, "default")
    assert out
    assert mem.added[0]["metadata"]["bank_id"] == "default", \
        "全码约定：永远盖戳，default 也不例外"
    assert all(sid.startswith("legacy_") for sid in mem.deleted), \
        f"默认域毕业删到了命名域: {mem.deleted}"


# ═══════════════ ⑤ auto_graduate 端到端 ═══════════════
def test_auto_graduate_threads_scope_end_to_end(monkeypatch):
    monkeypatch.setattr(graduation, "_call_llm",
                        lambda prompt, max_tokens=512: "端到端技能")
    mem = _FakeMem(_seed_items())

    res = graduation.auto_graduate(mem, "user_x", bank_id="bank_b")
    assert res["graduated_groups"] == 1
    for f in mem.get_all_filters:
        assert f == {"user_id": "user_x", "bank_id": "bank_b"}, \
            f"auto_graduate 链路中有一环没带作用域: {f}"
    assert mem.added[0]["metadata"]["bank_id"] == "bank_b"
    assert all(sid.startswith("b_") for sid in mem.deleted)


# ═══════════════ ⑥ /graduate 路由递进 ═══════════════
def test_graduate_route_threads_bank_scope(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import ducky.routes_v8 as routes_v8

    mem = _FakeMem(_seed_items())
    monkeypatch.setattr(routes_v8, "get_memory", lambda: mem)

    app = FastAPI()
    routes_v8.register_v8_routes(app)
    client = TestClient(app)

    # 命名域 dry_run：filters 下推 + 候选只有本域
    resp = client.post("/graduate", params={
        "user_id": "user_x", "bank_id": "bank_a", "dry_run": "true",
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok" and body["total_groups"] == 1
    assert body["groups"][0]["count"] == 3
    assert mem.get_all_filters[-1] == {"user_id": "user_x", "bank_id": "bank_a"}

    # 不传 bank_id = default 域：v19 filters 形状 + 只见存量
    resp = client.post("/graduate", params={
        "user_id": "user_x", "dry_run": "true",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_groups"] == 1 and body["groups"][0]["count"] == 3
    assert all(sid.startswith("legacy_") for sid in body["groups"][0]["sample_ids"])
    assert mem.get_all_filters[-1] == {"user_id": "user_x"}

    # 非法 bank_id：包成 error dict，不抛 500，且没发出任何取数/写删
    n_calls = len(mem.get_all_filters)
    resp = client.post("/graduate", params={
        "user_id": "user_x", "bank_id": "../etc", "dry_run": "true",
    })
    assert resp.status_code == 200, "非法作用域必须包成 error dict 不抛 500"
    assert resp.json()["status"] == "error"
    assert len(mem.get_all_filters) == n_calls and mem.deleted == []


# ═══════════════ ⑦ Persona 统计面 opt-in 作用域 ═══════════════
def _insert_fact(key, value, user, bank, category="项目"):
    conn = sqlite3.connect(_TEST_DB)
    conn.execute(
        "INSERT INTO facts (category, fact_key, fact_value, user_id, bank_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (category, key, value, user, bank),
    )
    conn.commit()
    conn.close()


def test_persona_count_scoped_optin():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from ducky.hot.legacy_routes import register_legacy_routes

    _insert_fact("a1", "甲库项目事实1", "u1", "bank_a")
    _insert_fact("a2", "甲库项目事实2", "u1", "bank_a")
    _insert_fact("b1", "乙库项目事实1", "u1", "bank_b")
    _insert_fact("d1", "存量项目事实1", "default", "default")

    app = FastAPI()
    register_legacy_routes(app)
    client = TestClient(app)

    # 不传作用域 = v19 管理员全量视图（存量零改动）
    r_all = client.get("/persona")
    assert r_all.status_code == 200
    assert r_all.json()["facts_count"] == 4

    # 传作用域：只数本域——乙库/存量不许灌进甲库的画像计数
    r_a = client.get("/persona", params={"user_id": "u1", "bank_id": "bank_a"})
    assert r_a.json()["facts_count"] == 2, \
        "甲库画像计数不等于 2——统计面跨库泄漏没堵住"
    r_b = client.get("/persona", params={"user_id": "u1", "bank_id": "bank_b"})
    assert r_b.json()["facts_count"] == 1

    # POST /persona/refresh（Form 参数）同样递进作用域
    r_ref = client.post("/persona/refresh",
                        data={"name": "user", "user_id": "u1", "bank_id": "bank_a"})
    assert r_ref.status_code == 200
    assert r_ref.json()["facts_count"] == 2, \
        "/persona/refresh 没把 Form 作用域递进 _refresh_persona_inline"
