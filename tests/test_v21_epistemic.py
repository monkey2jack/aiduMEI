"""
tests/test_v21_epistemic.py — v21 preview F1/F2 schema 总批次 + epistemic 判定守卫
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
v21 任务书 §二/§三验收（红→绿）：
  1. v6 迁移落地：facts.epistemic_mode / facts.superseded_by /
     knowledge_evolution 溯源三列 / reflection_candidates / retrieval_weights
  2. 存量行一律默认 'fuzzy'（不回填——宁缺毋滥）
  3. 迁移幂等：重复 apply_migrations 不炸、不重复加列
  4. resolve_epistemic 映射表（本仓真实 source 值）
  5. 乘数配置：默认四值齐备；非法 env fail-closed 回默认
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp_dir = tempfile.mkdtemp(prefix="aidumem_v21_epistemic_")

import ducky.utils as utils  # noqa: E402

utils.FACTS_DB = os.path.join(_tmp_dir, "facts.db")


@pytest.fixture(autouse=True)
def setup_test_db():
    fd, db_path = tempfile.mkstemp(prefix="facts_", suffix=".db", dir=_tmp_dir)
    os.close(fd)
    utils.FACTS_DB = db_path
    from ducky.schema_bootstrap import ensure_core_schema
    from ducky.federation.schema import ensure_federation_schema
    ensure_core_schema(force=True)
    ensure_federation_schema(force=True)
    yield


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


# ── 1. v6 迁移落地 ───────────────────────────────────────────────────────────

def test_v6_migration_lands_all_columns_and_tables():
    from ducky.schema_bootstrap import CURRENT_SCHEMA_VERSION
    conn = utils.get_facts_conn()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == CURRENT_SCHEMA_VERSION

    facts_cols = _cols(conn, "facts")
    assert "epistemic_mode" in facts_cols
    assert "superseded_by" in facts_cols

    ke_cols = _cols(conn, "knowledge_evolution")
    assert {"origin_agent", "origin_session_id", "origin_turn"} <= ke_cols

    rc_cols = _cols(conn, "reflection_candidates")
    assert {"user_id", "bank_id", "source", "candidate_text", "status"} <= rc_cols

    rw_cols = _cols(conn, "retrieval_weights")
    assert {"user_id", "bank_id", "dimension", "weight"} <= rw_cols


def test_v6_existing_rows_default_fuzzy_no_backfill():
    conn = utils.get_facts_conn()
    conn.execute(
        "INSERT INTO facts (category, fact_key, fact_value, agent_id) "
        "VALUES ('general', 'k1', '存量老记忆', 'dudu')")
    conn.commit()
    row = conn.execute("SELECT epistemic_mode FROM facts WHERE fact_key='k1'").fetchone()
    assert row[0] == "fuzzy", "存量行必须保持 fuzzy 兜底，不得编造出身"


def test_v6_migration_idempotent():
    from ducky.schema_bootstrap import apply_migrations
    conn = utils.get_facts_conn()
    apply_migrations(conn)  # 重跑不炸
    apply_migrations(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 9  # v21.2 M2：+溯源三列


# ── 2. resolve_epistemic 映射表 ─────────────────────────────────────────────

@pytest.mark.parametrize("source,expected", [
    ("dudu", "user_provided"),            # 用户 id 直写（本仓历史惯例）
    ("", "fuzzy"),                        # 空来源如实兜底
    ("cron_lesson", "reasoned"),          # dudu验收用例：cron 提炼 → reasoned
    ("pattern_extract", "reasoned"),
    ("reflect", "reasoned"),
    ("autodream", "reasoned"),
    ("self_edit", "reasoned"),
    ("web_extract", "referenced"),
    ("federation_sync", "referenced"),
    ("wal_cascade", "fuzzy"),             # 系统内部记账不算用户事实
    ("backfill", "fuzzy"),
    ("schema_v5", "fuzzy"),
])
def test_resolve_epistemic_mapping(source, expected):
    from ducky.epistemic import resolve_epistemic
    assert resolve_epistemic(source) == expected


def test_resolve_epistemic_external_ref_flag_wins():
    from ducky.epistemic import resolve_epistemic, EPISTEMIC_MODES
    assert resolve_epistemic("dudu", has_external_ref=True) == "referenced"
    # 枚举自检：四档恰好齐备
    assert set(EPISTEMIC_MODES) == {"user_provided", "referenced", "reasoned", "fuzzy"}


# ── 3. 乘数配置（fail-closed）───────────────────────────────────────────────

def test_multipliers_default_complete():
    from ducky.epistemic import load_epistemic_multipliers, EPISTEMIC_MODES
    m = load_epistemic_multipliers(env={})
    assert set(m) == set(EPISTEMIC_MODES)
    assert m["user_provided"] == 1.15
    assert m["reasoned"] == 0.85
    assert m["fuzzy"] == 1.00


def test_multipliers_env_override_and_fail_closed():
    from ducky.epistemic import load_epistemic_multipliers
    m = load_epistemic_multipliers(env={"AIDUMEI_EPISTEMIC_MULT_REASONED": "0.9"})
    assert m["reasoned"] == 0.9
    # 非法值一律回默认，不炸
    for bad in ("abc", "-1", "3.0", "nan"):
        m2 = load_epistemic_multipliers(env={"AIDUMEI_EPISTEMIC_MULT_REASONED": bad})
        assert m2["reasoned"] == 0.85, f"非法配置 {bad!r} 必须 fail-closed 回默认"
