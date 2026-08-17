"""
tests/test_v19_4_0_secondary_paths.py — v19.4.0 审计修复 🟡-D 回归测试
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

生产审计 🟡-D：三条次路径（refine_memory / federation writer /
persona ai-self）直接 INSERT/UPDATE facts，绕过治理与事件账本。
拍板（记录于 writer.py docstring + CHANGELOG）：

  · federation insert = 真实外部路径（/federation/facts/add）
    → 治理 + 账本全上；update/merge 补账本
  · refine_memory / ai-self = 内部路径 → 只补账本，不上治理

本文件守住：联邦三路径账本留痕、联邦 insert 治理生效
（密钥 reject 归档、正常内容 provisional 降权）、钩子存在性守卫。
"""

import os
import sqlite3
import sys
import tempfile

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_tmp_dir = tempfile.mkdtemp(prefix="aidumem_v1941_secondary_")
_TEST_DB = os.path.join(_tmp_dir, "facts.db")

import ducky.utils as utils
utils.FACTS_DB = _TEST_DB
utils.TEXT_FTS_DB = os.path.join(_tmp_dir, "text_fts.db")

from ducky.event_ledger import ensure_ledger_schema, get_history
from ducky.federation.writer import write_fact

# 与生产对齐的最小 facts DDL（含 archived_at，治理归档可用）
_FACTS_DDL = """
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL DEFAULT 'general',
    fact_key TEXT NOT NULL,
    fact_value TEXT NOT NULL,
    source TEXT DEFAULT 'local',
    summary TEXT,
    overview TEXT,
    level TEXT DEFAULT 'L2',
    agent_id TEXT DEFAULT 'local',
    profile TEXT DEFAULT 'default',
    memory_tier TEXT DEFAULT 'semantic',
    trust_score REAL DEFAULT 0.5,
    retrieval_count INTEGER DEFAULT 0,
    last_accessed_at TIMESTAMP,
    archived INTEGER DEFAULT 0,
    archived_at TIMESTAMP,
    tags TEXT DEFAULT '',
    shared INTEGER DEFAULT 1,
    peer TEXT DEFAULT '',
    valid_from TEXT,
    valid_to TEXT,
    recorded_at TIMESTAMP,
    decay_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(agent_id, category, fact_key)
);
"""


@pytest.fixture(autouse=True)
def _setup_test_env():
    utils.FACTS_DB = _TEST_DB
    utils.TEXT_FTS_DB = os.path.join(_tmp_dir, "text_fts.db")
    conn = sqlite3.connect(_TEST_DB)
    conn.executescript(_FACTS_DDL)
    conn.commit()
    conn.close()
    ensure_ledger_schema()
    yield


def _fact_row(fid):
    conn = utils.get_facts_conn()
    row = conn.execute("SELECT * FROM facts WHERE id=?", (fid,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ─────────────────────────────────────────────────────────────
# 1. 联邦 insert：账本 + 治理
# ─────────────────────────────────────────────────────────────

def test_federation_insert_ledger():
    res = write_fact("偏好", "tea_pref", "改喝乌龙茶了", agent_id="agent_t")
    assert res["status"] == "ok" and res["action"] == "insert"
    hist = get_history("fact:tea_pref")
    assert any(e["action"] == "add" and "federation insert" in e["reason"] for e in hist)


def test_federation_insert_governance_secret_rejected():
    """外部路径含密钥 → 规则 reject 归档（与 /facts/add 同等审计）"""
    res = write_fact("general", "leak_cred",
                     "api_key: sk-abcdef1234567890abcdef", agent_id="agent_t")
    assert res["status"] == "ok"
    assert res["governance"]["route"] == "rule_rejected"
    row = _fact_row(res["fact_id"])
    assert row["archived"] == 1, "密钥事实必须归档"


def test_federation_insert_governance_provisional():
    """正常内容 → provisional 降权待审"""
    res = write_fact("general", "meeting_note", "每周三下午开组会", agent_id="agent_t")
    assert res["governance"]["route"] == "llm_eval"
    row = _fact_row(res["fact_id"])
    assert row["archived"] == 0
    assert abs(row["trust_score"] - 0.30) < 1e-9


# ─────────────────────────────────────────────────────────────
# 2. 联邦 update / merge：账本
# ─────────────────────────────────────────────────────────────

def test_federation_update_ledger():
    # 0.70 ≤ sim < 0.85 → update（「4月→5月」变体实测 sim≈0.778）
    a = write_fact("测试类", "upd_key1", "主服务器租期到 2027 年 4 月为止", agent_id="agent_u")
    b = write_fact("测试类", "upd_key2", "主服务器租期到 2027 年 5 月为止", agent_id="agent_u")
    assert b["action"] == "update" and b["fact_id"] == a["fact_id"]
    hist = get_history(f"fact:{a['fact_id']}")
    assert any(e["action"] == "update" and "federation update" in e["reason"] for e in hist)


def test_federation_merge_ledger():
    # sim ≥ 0.85 → merge（相同内容不同键，实测 sim=1.0）
    a = write_fact("测试类", "merge_key1", "主服务器租期到 2027 年 4 月为止", agent_id="agent_m")
    b = write_fact("测试类", "merge_key2", "主服务器租期到 2027 年 4 月为止", agent_id="agent_m")
    assert b["action"] == "merge" and b["fact_id"] == a["fact_id"]
    hist = get_history(f"fact:{a['fact_id']}")
    assert any(e["action"] == "update" and "federation merge" in e["reason"] for e in hist)


# ─────────────────────────────────────────────────────────────
# 3. 钩子存在性守卫（防误删）
# ─────────────────────────────────────────────────────────────

def test_ledger_hooks_present_in_secondary_paths():
    for rel in (
        os.path.join("ducky", "federation", "writer.py"),
        os.path.join("ducky", "refine_memory.py"),
        os.path.join("ducky", "extended", "routes.py"),
    ):
        with open(os.path.join(_REPO_ROOT, *rel.split(os.sep)), encoding="utf-8") as f:
            src = f.read()
        assert "record_event" in src, f"{rel} 的事件账本钩子不见了"


def test_governance_hook_present_in_federation_writer():
    with open(os.path.join(_REPO_ROOT, "ducky", "federation", "writer.py"),
              encoding="utf-8") as f:
        src = f.read()
    assert "govern_fact_write" in src, "联邦写入的治理钩子不见了"
    assert "spawn_async_eval" in src, "联邦异步评估派发不见了"
