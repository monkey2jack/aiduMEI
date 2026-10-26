"""f0.6++ WP5 · 记忆图谱(数据层+API)守卫。

三类边全部从**已经发生的事**派生,不猜测;租户隔离与 facts 同款谓词;
API 走 scope read(与 /facts 族同款四百)。
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def graph_env(tmp_path, monkeypatch):
    (tmp_path / "d").mkdir(parents=True, exist_ok=True)
    (tmp_path / "l").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AIDUMEM_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("AIDUMEM_LOG_DIR", str(tmp_path / "l"))
    # facts.db 必须先行存在(derive_update_chain 往同一个库挂表)
    import sqlite3 as _sq
    c=_sq.connect(str(tmp_path / "d" / "facts.db"))
    from ducky.schema_bootstrap import ensure_core_schema
    ensure_core_schema()
    c.close()
    from ducky import graph_memory as gm
    gm.ensure_graph_schema()
    yield gm


def test_edge_write_is_typed_and_idempotent(graph_env):
    gm = graph_env
    assert gm.add_edge("fact", "1", "fact", "2", "update_chain", "u1", "default")
    assert gm.add_edge("fact", "1", "fact", "2", "update_chain", "u1", "default")  # 幂等
    with pytest.raises(ValueError):
        gm.add_edge("fact", "1", "fact", "2", "made_up_type", "u1", "default")
    st = gm.graph_stats("u1", "default")
    assert st["by_type"].get("update_chain") == 1, "幂等写入被计双"


def test_neighbors_depth_and_tenant_isolation(graph_env):
    gm = graph_env
    # u1 的链: f1 -> f2 -> f3
    for a, b in (("1", "2"), ("2", "3")):
        gm.add_edge("fact", a, "fact", b, "update_chain", "u1", "default")
    # u2 的边(隔离对照)
    gm.add_edge("fact", "9", "fact", "10", "same_session", "u2", "default")

    r1 = gm.neighbors("u1", "fact", "1", bank_id="default", depth=2)
    ids = {n["id"] for n in r1["nodes"]}
    assert {"1", "2", "3"} <= ids, f"u1 邻域展开不全: {ids}"
    assert "9" not in ids and "10" not in ids, "跨租户泄漏 —— 红线"

    r3 = gm.neighbors("u1", "fact", "1", bank_id="default", depth=3)
    assert r3["depth"] == 3

    r_u2 = gm.neighbors("u2", "fact", "9", bank_id="default", depth=1)
    u2_ids = {n["id"] for n in r_u2["nodes"]}
    assert "10" in u2_ids and "1" not in u2_ids, "u2 视图被污染"


def test_neighbors_rejects_bad_depth(graph_env):
    gm = graph_env
    with pytest.raises(ValueError):
        gm.neighbors("u1", "fact", "1", bank_id="default", depth=99)


def test_derive_update_chain_from_facts(graph_env, tmp_path, monkeypatch):
    """update_chain 派生:同名 fact_key 的两条 facts 顺位相连。"""
    from ducky.utils import get_facts_conn
    conn = get_facts_conn()
    # UNIQUE(agent_id,user_id,bank_id,category,fact_key):同 key 多行必须有
    # 不同 agent_id 顺序先后才插得进去——正好模拟「两个 agent 对同一事实的
    # 先后认知」这一 update_chain 的真实来源。
    conn.execute(
        "INSERT OR IGNORE INTO facts (id, fact_key, fact_value, category, user_id, bank_id, valid_from, created_at, agent_id) "
        "VALUES (9001, 'birthday', 'x', 'pattern_kv', 'graphu', 'default', datetime('now'), datetime('now'), 'agentA')")
    conn.execute(
        "INSERT OR IGNORE INTO facts (id, fact_key, fact_value, category, user_id, bank_id, valid_from, created_at, agent_id) "
        "VALUES (9002, 'birthday', 'y-updated', 'pattern_kv', 'graphu', 'default', datetime('now'), datetime('now'), 'agentB')")
    conn.commit()
    got = conn.execute("SELECT id, agent_id FROM facts WHERE fact_key='birthday' AND user_id='graphu'").fetchall()
    assert len(got) == 2, f"前提破缺: 同 key 两条没插进去, 现有 {got!r}"
    gm = graph_env
    n = gm.derive_update_chain_edges("graphu", "default")
    assert n >= 1, "update_chain 没派生出任何边"
    st = gm.graph_stats("graphu", "default")
    assert st["by_type"].get("update_chain", 0) >= 1
