"""
tests/test_v21_sidecar_epistemic.py — v21.0 收口 🔴-1/🔴-2 守卫
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
生产用户审计整改验收（红→绿）：
  1. schema v7：memory_epistemic sidecar 建表落地、幂等
  2. stamp_memory_refs：写入/更新/非法档拒绝/未迁移库如实 0
  3. 打分回落：facts 列无值时 sidecar 生效（主链路腿乘数真的工作）
  4. 🔴-2：pattern_extract 经 write_fact → reasoned（不再误标 referenced）；
     联邦路由入口 via_federation=True → referenced
"""
from __future__ import annotations

import time

import pytest

import ducky.utils as utils
from write_path_fixtures import isolated_write_stores as _isolated_write_stores  # noqa: F401 -- opt-in fixture

pytestmark = pytest.mark.usefixtures("isolated_write_stores")


# ── 1. schema v7 ─────────────────────────────────────────────────────────────

def test_v7_sidecar_table_lands():
    from ducky.schema_bootstrap import CURRENT_SCHEMA_VERSION
    conn = utils.get_facts_conn()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_epistemic)").fetchall()}
    assert {"memory_ref", "epistemic_mode", "user_id", "bank_id", "source"} <= cols
    conn.close()


# ── 2. stamp_memory_refs ────────────────────────────────────────────────────

def test_stamp_memory_refs_write_and_update():
    from ducky.epistemic import stamp_memory_refs
    n = stamp_memory_refs(["uuid-1", "uuid-2"], "user_provided",
                          user_id="dudu", bank_id="default", source="add")
    assert n == 2
    conn = utils.get_facts_conn()
    assert conn.execute(
        "SELECT epistemic_mode FROM memory_epistemic WHERE memory_ref='uuid-1'"
    ).fetchone()[0] == "user_provided"
    # 更新语义：重打覆盖档位
    stamp_memory_refs(["uuid-1"], "reasoned", user_id="dudu", bank_id="default")
    assert conn.execute(
        "SELECT epistemic_mode FROM memory_epistemic WHERE memory_ref='uuid-1'"
    ).fetchone()[0] == "reasoned"
    conn.close()


def test_stamp_memory_refs_fail_closed():
    from ducky.epistemic import stamp_memory_refs
    assert stamp_memory_refs(["uuid-x"], "bogus_mode", user_id="d", bank_id="b") == 0
    assert stamp_memory_refs([], "reasoned", user_id="d", bank_id="b") == 0
    assert stamp_memory_refs([None, ""], "reasoned", user_id="d", bank_id="b") == 0


# ── 3. 打分回落（sidecar 生效）──────────────────────────────────────────────

def test_scoring_falls_back_to_sidecar():
    from ducky.scoring import DEFAULT_WEIGHTS, _score_one_candidate
    from ducky.epistemic import load_epistemic_multipliers, stamp_memory_refs
    stamp_memory_refs(["uuid-main"], "user_provided", user_id="d", bank_id="b")
    item = {  # 无 facts 列（mem0 腿形态）
        "id": "uuid-main", "memory": "用户喜欢用 Python 写后端服务",
        "score": 0.9, "created_at": time.time() - 3600, "updated_at": time.time() - 3600,
    }
    kept, _ = _score_one_candidate(
        "Python 后端", item,
        w=DEFAULT_WEIGHTS, now_ts=time.time(), is_fact_query=False,
        type_decay_on=False, salience_map={}, type_map={},
        memory_type_filter=None, gate_on=False,
        epistemic_mult=load_epistemic_multipliers(env={}),
        epi_map={"uuid-main": "user_provided"},
    )
    assert kept["_epistemic_mult"] == 1.15, "sidecar 里的出身必须让乘数生效"


# ── 4. 🔴-2：via_federation 归位 ─────────────────────────────────────────────

def _write_via(conn, source, via_federation):
    from ducky.federation.writer import write_fact
    return write_fact("general", f"k-{source}-{via_federation}", f"值 {source}",
                      source=source, user_id="dudu", bank_id="default",
                      via_federation=via_federation)


def test_pattern_extract_no_longer_mislabeled_referenced():
    _write_via(None, "pattern_extract", False)
    conn = utils.get_facts_conn()
    row = conn.execute(
        "SELECT epistemic_mode FROM facts WHERE fact_key='k-pattern_extract-False'"
    ).fetchone()
    conn.close()
    assert row[0] == "reasoned", "pattern_extract 必须 reasoned，不许再误标 referenced"


# ── 5. layer1 登记点打标（🔴-1 真实缝位）────────────────────────────────────

def test_layer1_index_after_add_stamps_sidecar(monkeypatch):
    from ducky.layer1_selfcheck import _index_after_add
    from ducky import mem0_runtime

    class VectorReplica:
        """Only the external vector transport is replaced; indexing stays real."""
        collection_name = 'synthetic-sidecar'

        def __init__(self):
            self.vector_store = self
            self.client = self
            self.points = {'uuid-l1-1': {}, 'uuid-l1-2': {}}

        def set_payload(self, *, collection_name, payload, points):
            assert collection_name == self.collection_name
            for memory_id in points:
                self.points[memory_id].update(payload)

    replica = VectorReplica()
    monkeypatch.setattr(mem0_runtime, 'get_memory', lambda: replica)
    _index_after_add({"results": [{"id": "uuid-l1-1", "memory": "用户喜欢手冲咖啡"}]},
                     user_id="dudu", bank_id="default", infer=True)
    _index_after_add({"results": [{"id": "uuid-l1-2", "memory": "原文直写"}]},
                     user_id="dudu", bank_id="default", infer=False)
    conn = utils.get_facts_conn()
    r1 = conn.execute(
        "SELECT epistemic_mode FROM memory_epistemic WHERE memory_ref='uuid-l1-1'").fetchone()
    r2 = conn.execute(
        "SELECT epistemic_mode FROM memory_epistemic WHERE memory_ref='uuid-l1-2'").fetchone()
    conn.close()
    assert r1[0] == "reasoned", "LLM 蒸馏经手必须 reasoned"
    assert r2[0] == "user_provided", "确定性直写必须 user_provided"
    facts = utils.get_facts_conn()
    text = utils.get_text_conn()
    for memory_id, payload in replica.points.items():
        recorded = facts.execute(
            "SELECT memory_type FROM memory_types WHERE memory_ref_raw=? AND user_id=? AND bank_id=?",
            (memory_id, 'dudu', 'default'),
        ).fetchone()[0]
        assert payload['memory_type'] == recorded
        assert text.execute(
            "SELECT memory_type FROM memories WHERE id=? AND user_id=? AND bank_id=?",
            (memory_id, 'dudu', 'default'),
        ).fetchone()[0] == recorded
    facts.close()
    text.close()


def test_federation_route_marks_referenced():
    _write_via(None, "some_agent", True)
    conn = utils.get_facts_conn()
    row = conn.execute(
        "SELECT epistemic_mode FROM facts WHERE fact_key='k-some_agent-True'"
    ).fetchone()
    conn.close()
    assert row[0] == "referenced", "联邦路由写入保持 referenced"
