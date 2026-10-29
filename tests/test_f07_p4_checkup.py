"""f0.7 P4 · 心图日常体检(重复 / 过时 / 冲突)守卫。

体检只读、按租户隔离、诚实(没有 memory.db 就说不可用,不伪造零);
心图面板默认只拉体检,整张星图点了才加载;native 内核下图边读写同库。
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _mk_memory_db(path: Path, rows):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE memories (mid INTEGER PRIMARY KEY, id TEXT, content TEXT, user_id TEXT, "
              "bank_id TEXT DEFAULT 'default', memory_type TEXT DEFAULT 'FACTS', hash TEXT, "
              "created_at TEXT, expires_at TEXT)")
    for i, (uid, text, *rest) in enumerate(rows):
        exp = rest[0] if rest else None
        c.execute("INSERT INTO memories (id, content, user_id, hash, created_at, expires_at) VALUES (?,?,?,?,?,?)",
                  (f"m{i}", text, uid, hashlib.md5(text.encode()).hexdigest(), f"2026-10-{10 + i:02d}", exp))
    c.commit(); c.close()


def _mk_facts_db(path: Path, rows):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, category TEXT, fact_key TEXT, fact_value TEXT, "
              "source TEXT, agent_id TEXT, user_id TEXT, bank_id TEXT, archived INTEGER DEFAULT 0, valid_to TEXT, "
              "superseded_by TEXT, created_at TEXT)")
    for r in rows:
        c.execute("INSERT INTO facts (category, fact_key, fact_value, source, user_id, bank_id, archived, valid_to,"
                  " superseded_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,'2026-10-20')", r)
    c.commit(); c.close()


@pytest.fixture()
def ck(tmp_path, monkeypatch):
    mem, facts = tmp_path / "memory.db", tmp_path / "facts.db"
    _mk_memory_db(mem, [
        ("u1", "阿明喜欢喝美式。"),
        ("u1", "阿明  喜欢喝美式"),            # 去标点空白后相同 → 重复
        ("u1", "阿明喜欢喝美式!"),
        ("u1", "[SILENT]"), ("u1", "[SILENT]"),  # 机器话
        ("u1", "去年的活动安排", "2020-01-01"),   # 已过期
        ("u1", "一条正常的记忆"),
        ("u2", "阿明喜欢喝美式。"), ("u2", "阿明喜欢喝美式。"),   # 别的租户
    ])
    _mk_facts_db(facts, [
        ("偏好", "咖啡", "美式", "u1", "u1", "default", 0, None, None),
        ("偏好", "咖啡", "拿铁", "u1", "u1", "default", 0, None, None),          # 冲突
        ("偏好", "茶", "龙井", "u1", "u1", "default", 0, None, None),
        ("偏好", "茶", "普洱", "u1", "u1", "default", 0, None, "7"),             # 已被取代 → 过时,不算冲突
        ("pattern_metric", "30秒", "看门狗 30 秒", "u1", "u1", "default", 0, None, None),
        ("pattern_metric", "30秒", "每 30 秒写一次", "u1", "u1", "default", 0, None, None),  # 度量键天生多值
        ("住址", "城市", "杭州", "u1", "u1", "default", 0, "2020-01-01", None),   # 已失效
        ("偏好", "咖啡", "手冲", "u2", "u2", "default", 0, None, None),          # 别的租户
        ("偏好", "咖啡", "冷萃", "u1", "u1", "default", 1, None, None),          # 已归档
    ])
    import ducky.memory_kernel.impl_native as nat
    import ducky.utils as utils
    monkeypatch.setattr(nat, "default_db_path", lambda: str(mem))
    monkeypatch.setattr(utils, "FACTS_DB", str(facts))
    from ducky.memory_checkup import checkup
    return checkup, mem, facts


def test_duplicates_normalized_noise_and_tenant(ck):
    checkup, *_ = ck
    r = checkup("u1")
    assert r["available"] and r["total_memories"] == 7
    d = r["duplicates"]
    assert d["groups"] == 2 and d["extra_copies"] == 3, d
    top = d["items"][0]
    assert top["count"] == 3 and top["keep_id"] == "m0" and not top["machine_noise"]
    assert any(i["machine_noise"] and i["count"] == 2 for i in d["items"])
    assert all(not x.startswith("m7") and x != "m8" for i in d["items"] for x in i["extra_ids"]), "跨租户泄漏"
    assert checkup("u2")["duplicates"]["extra_copies"] == 1


def test_stale_and_conflicts(ck):
    checkup, *_ = ck
    r = checkup("u1")
    st = r["stale"]
    assert st["expired_memories"] == 1 and st["ended_facts"] == 2, st
    kinds = {i["kind"] for i in st["items"]}
    assert kinds == {"memory_expired", "fact_ended"}
    cf = r["conflicts"]
    assert cf["keys"] == 1, cf                                   # 只有「咖啡:美式≠拿铁」
    vals = {v["value"] for v in cf["items"][0]["values"]}
    assert vals == {"美式", "拿铁"}, "归档/他人/被取代的值不该算进冲突"
    assert "重复 2 组" in r["headline"] and "冲突 1 处" in r["headline"]


def test_checkup_is_read_only_and_honest(ck, monkeypatch):
    checkup, mem, facts = ck
    before = (mem.read_bytes(), facts.read_bytes())
    checkup("u1"); checkup("u2", limit=1)
    assert (mem.read_bytes(), facts.read_bytes()) == before, "体检改了数据"
    import ducky.memory_kernel.impl_native as nat
    monkeypatch.setattr(nat, "default_db_path", lambda: str(mem) + ".absent")
    r = checkup("u1")
    assert r["available"] is False and "duplicates" not in r, "没有 memory.db 不许伪造零"


def test_graph_edges_read_from_memory_db_under_native(tmp_path, monkeypatch):
    mem = tmp_path / "memory.db"
    from ducky.graph_memory import _DDL
    c = sqlite3.connect(mem); c.executescript(_DDL)
    c.execute("INSERT INTO graph_edges (src_type,src_id,dst_type,dst_id,edge_type,user_id,bank_id) "
              "VALUES ('memory','a','memory','b','same_session','u1','default')")
    c.commit(); c.close()
    import ducky.graph_memory as gm
    import ducky.memory_kernel.impl_native as nat
    monkeypatch.setattr(nat, "default_db_path", lambda: str(mem))
    monkeypatch.setattr(gm, "_NATIVE_CONN", {})
    monkeypatch.setenv("AIDUMEI_MEMORY_KERNEL", "native")
    st = gm.graph_stats("u1", "default")
    assert st["by_type"] == {"same_session": 1}, "native 写边进 memory.db,统计却读了别的库"
    assert {n["id"] for n in gm.neighbors("u1", "memory", "a")["nodes"]} == {"a", "b"}


def test_route_registered_as_scope_read():
    from ducky.route_policy import POLICIES
    p = POLICIES[("get", "/graph/checkup")]
    assert (p.resource, p.action) == ("scope", "read")


def test_map_panel_loads_checkup_first_and_star_map_on_demand():
    js = (REPO / "frontend" / "js" / "panels.js").read_text(encoding="utf-8")
    m = re.search(r"async function renderMap\(body\) \{(.*?)\n\}\n", js, re.S)
    assert m, "renderMap 不见了"
    body = m.group(1)
    assert "/graph/checkup" in body
    assert "/knowledge/tree" not in body and "drawEChartsStarMap" not in body, "整张星图不该默认渲染"
    assert "renderStarMap" in body and "addEventListener('click'" in body


def test_nebula_real_edges_tenant_isolated_and_capped(tmp_path, monkeypatch):
    mem = tmp_path / "memory.db"
    _mk_memory_db(mem, [("u1", "甲"), ("u1", "乙"), ("u1", "丙"), ("u2", "丁")])
    from ducky.graph_memory import _DDL
    c = sqlite3.connect(mem); c.executescript(_DDL)
    for s_, d_, et, u in (("m0", "m1", "same_session", "u1"), ("m1", "m2", "co_occurrence", "u1"),
                          ("m3", "m0", "same_session", "u2")):
        c.execute("INSERT INTO graph_edges (src_type,src_id,dst_type,dst_id,edge_type,user_id,bank_id) "
                  "VALUES ('memory',?,'memory',?,?,?,'default')", (s_, d_, et, u))
    c.commit(); c.close()
    import ducky.graph_memory as gm
    import ducky.memory_kernel.impl_native as nat
    monkeypatch.setattr(nat, "default_db_path", lambda: str(mem))
    monkeypatch.setattr(gm, "_NATIVE_CONN", {})
    monkeypatch.setenv("AIDUMEI_MEMORY_KERNEL", "native")
    r = gm.nebula("u1")
    ids = {n["id"] for n in r["nodes"]}
    assert ids == {"memory:m0", "memory:m1", "memory:m2"}, "跨租户节点混进星云"
    assert {n["text"] for n in r["nodes"]} == {"甲", "乙", "丙"}
    assert len(r["edges"]) == 2 and not r["truncated"]
    hub = next(n for n in r["nodes"] if n["id"] == "memory:m1")
    assert hub["degree"] == 2


def test_map_panel_offers_nebula_on_demand_and_brand_name():
    js = (REPO / "frontend" / "js" / "panels.js").read_text(encoding="utf-8")
    assert "/graph/nebula" in js and "ckNebulaBtn" in js
    for page in ("index.html", "login.html"):
        html = (REPO / "frontend" / page).read_text(encoding="utf-8")
        assert "优忆思</b>" in html and "引擎</b>" not in html, f"{page} 品牌名应为 爱嘟优忆思"
