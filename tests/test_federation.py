"""
tests/test_federation.py — aiduMEM v13.0 Pantheon 联邦层测试
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

覆盖：schema 幂等迁移 / 分层衰减 / 去重三态 / 注册表 / 四级降级 / MoE 门控 / 广播

跑法：cd <仓库根> && python3 -m pytest tests/test_federation.py -v
测试全部在临时 facts.db 上跑，绝不碰生产库。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── 关键：在导入任何 federation 模块之前把 DB 指向临时库 ──
_tmp_dir = tempfile.mkdtemp(prefix="aidumem_fed_test_")
_TEST_DB = os.path.join(_tmp_dir, "facts.db")

import ducky.utils as utils  # noqa: E402

utils.FACTS_DB = _TEST_DB

# 本地 agent 标识（与 ducky.utils.DEFAULT_AGENT_ID 保持一致）
LOCAL_AGENT = utils.DEFAULT_AGENT_ID

# 生产 facts 表的最小必要结构（与真实 schema 的相关字段一致）
_FACTS_DDL = """
CREATE TABLE facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL DEFAULT 'general',
    fact_key TEXT NOT NULL,
    fact_value TEXT NOT NULL,
    source TEXT DEFAULT 'local',
    summary TEXT,
    overview TEXT,
    level TEXT DEFAULT 'L2',
    trust_score REAL DEFAULT 0.5,
    retrieval_count INTEGER DEFAULT 0,
    last_accessed_at TIMESTAMP,
    archived INTEGER DEFAULT 0,
    valid_from TEXT,
    valid_to TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(category, fact_key)
);
CREATE TABLE fact_entities (
    entity_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT, entity_type TEXT
);
"""


@pytest.fixture(scope="module", autouse=True)
def _bootstrap_db():
    conn = sqlite3.connect(_TEST_DB)
    conn.executescript(_FACTS_DDL)
    conn.commit()
    conn.close()

    from ducky.federation.schema import ensure_federation_schema

    ensure_federation_schema(force=True)
    yield


# 与其他测试模块同进程合跑时，其他模块的 autouse fixture 会把 utils.FACTS_DB
# 改成它们自己的临时库。这里在每测试前强制指回本文件的临时库，保证隔离。
@pytest.fixture(autouse=True)
def _bind_test_db():
    utils.FACTS_DB = _TEST_DB
    yield


# ═══════════════════ schema ═══════════════════
def test_schema_migration_is_idempotent():
    from ducky.federation.schema import ensure_federation_schema

    first = ensure_federation_schema(force=True)
    second = ensure_federation_schema(force=True)
    assert first["status"] == "ok"
    assert second["status"] == "ok"
    # 第二次不应再新增任何字段或表
    assert second["added_columns"] == []
    assert second["created_tables"] == []


def test_federation_columns_present():
    conn = sqlite3.connect(_TEST_DB)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(facts)")}
    conn.close()
    for expected in ("agent_id", "profile", "memory_tier", "recorded_at", "decay_at", "shared", "tags"):
        assert expected in cols, f"缺字段 {expected}"


# ═══════════════════ tier ═══════════════════
def test_tier_inference():
    from ducky.federation import tier

    assert tier.infer_tier("铁律", "变更前必须验证", "先确认再执行") == tier.PROCEDURAL
    assert tier.infer_tier("日记", "2026-07-31", "今天很开心") == tier.EPISODIC
    assert tier.infer_tier("config", "port", "8765") == tier.SEMANTIC
    # 推断不出 → semantic
    assert tier.infer_tier("随便", "xx", "yy") == tier.SEMANTIC


def test_procedural_never_decays():
    from ducky.federation import tier

    assert tier.decay_deadline(tier.PROCEDURAL) is None
    assert tier.decay_factor(tier.PROCEDURAL, 99999) == 1.0
    assert tier.score_multiplier(tier.PROCEDURAL, 3650) == pytest.approx(1.0)


def test_decay_halves_at_ttl():
    from ducky.federation import tier

    # 到 TTL 时衰减约一半
    assert tier.decay_factor(tier.EPISODIC, 30) == pytest.approx(0.5, abs=0.01)
    assert tier.decay_factor(tier.SEMANTIC, 180) == pytest.approx(0.5, abs=0.01)
    # 永不归零
    assert tier.decay_factor(tier.EPISODIC, 3650) > 0


def test_normalize_tier_rejects_garbage():
    from ducky.federation import tier

    assert tier.normalize_tier("PROCEDURAL") == tier.PROCEDURAL
    assert tier.normalize_tier("nonsense") == tier.SEMANTIC
    assert tier.normalize_tier(None) == tier.SEMANTIC


# ═══════════════════ writer + dedup ═══════════════════
def test_write_insert_then_merge():
    from ducky.federation.dedup import ACTION_INSERT, ACTION_MERGE
    from ducky.federation.writer import write_fact

    first = write_fact("测试类", "去重键1", "主服务器租期到 2027 年 4 月为止",
                       agent_id=LOCAL_AGENT, memory_tier="semantic")
    assert first["status"] == "ok"
    assert first["action"] == ACTION_INSERT

    # 近乎相同的内容 → merge，不新增行
    second = write_fact("测试类", "去重键2", "主服务器租期到 2027 年 4 月为止",
                        agent_id=LOCAL_AGENT, memory_tier="semantic")
    assert second["action"] == ACTION_MERGE
    assert second["fact_id"] == first["fact_id"]


def test_write_respects_explicit_tier_and_decay():
    from ducky.federation.writer import write_fact

    res = write_fact("铁律区", "铁律测试", "变更必须先验证再宣布完成",
                     agent_id=LOCAL_AGENT)
    assert res["memory_tier"] == "procedural"
    assert res["decay_at"] is None  # 铁律永不衰减

    ep = write_fact("日记区", "日记测试", "今天决定把 aiduMEM 升到联邦架构",
                    agent_id=LOCAL_AGENT, memory_tier="episodic")
    assert ep["memory_tier"] == "episodic"
    assert ep["decay_at"] is not None


def test_write_rejects_empty():
    from ducky.federation.writer import write_fact

    assert write_fact("x", "", "值")["status"] == "error"
    assert write_fact("x", "键", "")["status"] == "error"


def test_dedup_can_be_disabled():
    from ducky.federation.dedup import ACTION_INSERT
    from ducky.federation.writer import write_fact

    a = write_fact("无去重区", "键A", "完全一样的一句话内容用于验证关去重",
                   agent_id=LOCAL_AGENT, dedup=False)
    b = write_fact("无去重区", "键B", "完全一样的一句话内容用于验证关去重",
                   agent_id=LOCAL_AGENT, dedup=False)
    assert a["action"] == ACTION_INSERT and b["action"] == ACTION_INSERT
    assert a["fact_id"] != b["fact_id"]


# ═══════════════════ registry ═══════════════════
def test_register_and_list_agents():
    from ducky.federation.registry import list_agents, register_agent

    register_agent("mimir", display_name="Mímir", profile="default", description="联邦测试 Agent")
    ids = {a["agent_id"] for a in list_agents()}
    assert "mimir" in ids
    assert LOCAL_AGENT in ids


def test_heartbeat_autoregisters_unknown_agent():
    from ducky.federation.registry import heartbeat, list_agents

    heartbeat("ghost_agent")
    assert "ghost_agent" in {a["agent_id"] for a in list_agents()}


def test_deactivate_removes_from_active_pool():
    from ducky.federation.registry import active_agent_ids, deactivate_agent, register_agent

    register_agent("temp_agent", profile="default")
    assert "temp_agent" in active_agent_ids()
    deactivate_agent("temp_agent")
    assert "temp_agent" not in active_agent_ids()


# ═══════════════════ recall ladder ═══════════════════
def test_l1_hot_channel_finds_own_fact():
    from ducky.federation.recall import federated_recall
    from ducky.federation.writer import write_fact

    write_fact("热通道区", "独有键", "这是本地 agent 独有的记忆内容甲乙丙",
               agent_id=LOCAL_AGENT, dedup=False)
    res = federated_recall("独有的记忆内容甲乙丙", agent_id=LOCAL_AGENT, federated=False)
    assert res["status"] == "ok"
    assert res["count"] >= 1
    assert res["level"] in ("L1", "L2")
    # 不联邦时绝不出现 L3/L4
    assert all(step["level"] not in ("L3", "L4") for step in res["ladder"])


def test_l3_federated_reaches_peer_agent():
    from ducky.federation.recall import federated_recall
    from ducky.federation.writer import write_fact

    write_fact("联邦区", "对端独有键", "只有 mimir 知道的智慧之泉秘密口令 XYZ",
               agent_id="mimir", profile="default", dedup=False)
    # 从本地 agent 视角查：L1 查不到（不是自己的），必须降级到 L3
    res = federated_recall("智慧之泉秘密口令 XYZ", agent_id=LOCAL_AGENT,
                           profile="default", federated=True)
    assert res["count"] >= 1
    assert res["level"] in ("L3", "L4")
    assert any(r["agent_id"] == "mimir" for r in res["results"])


def test_recall_never_raises_on_bad_input():
    from ducky.federation.recall import federated_recall

    res = federated_recall("", agent_id=LOCAL_AGENT, top_k=1)
    assert res["status"] in ("ok", "degraded")


def test_tier_filter_narrows_results():
    from ducky.federation.recall import federated_recall

    res = federated_recall("", agent_id=LOCAL_AGENT, top_k=50, tier_filter="procedural")
    assert all(r["memory_tier"] == "procedural" for r in res["results"])


def test_rerank_only_when_requested():
    from ducky.federation.recall import federated_recall

    plain = federated_recall("记忆", agent_id=LOCAL_AGENT, top_k=2, rerank=False)
    assert not any(s.get("level") == "rerank" for s in plain["ladder"])


# ═══════════════════ MoE router ═══════════════════
def test_router_respects_explicit_flag():
    from ducky.federation.router import decide

    assert decide("随便问问", federated=True).federated is True
    assert decide("随便问问", federated=False).federated is False


def test_router_detects_federation_intent():
    from ducky.federation.router import decide

    d = decide("其他agent都知道什么", agent_id=LOCAL_AGENT)
    assert d.federated is True
    assert "关键词" in d.reason


def test_router_defaults_to_hot_channel():
    from ducky.federation.router import decide

    d = decide("主服务器什么时候到期", agent_id=LOCAL_AGENT)
    assert d.channel == "hot"
    assert d.federated is False


def test_route_recall_attaches_decision():
    from ducky.federation.router import route_recall

    res = route_recall("服务器", agent_id=LOCAL_AGENT, top_k=3)
    assert "route" in res
    assert res["route"]["channel"] in ("hot", "federated")


# ═══════════════════ broadcast ═══════════════════
def test_broadcast_collects_peer_facts():
    from ducky.federation.broadcast import collect_updates
    from ducky.federation.registry import register_agent
    from ducky.federation.writer import write_fact

    register_agent("mimir", profile="default")
    write_fact("广播区", "新事实键", "mimir 刚刚学到的一件全新的事情记录",
               agent_id="mimir", profile="default", dedup=False)
    res = collect_updates(LOCAL_AGENT, advance_cursor=False)
    assert res["status"] == "ok"
    assert res["count"] >= 1
    assert "mimir" in res["by_peer"]


def test_broadcast_cursor_advances_and_prevents_repeat():
    from ducky.federation.broadcast import collect_updates

    first = collect_updates(LOCAL_AGENT, advance_cursor=True)
    second = collect_updates(LOCAL_AGENT, advance_cursor=True)
    if first["count"] > 0:
        assert second["count"] == 0, "游标未生效，同一批事实被重复广播"


def test_awareness_summary_shape():
    from ducky.federation.broadcast import awareness_summary

    res = awareness_summary(LOCAL_AGENT)
    assert res["status"] == "ok"
    assert res["total_facts"] > 0
    assert LOCAL_AGENT in res["agents"]
    assert "tiers" in res["agents"][LOCAL_AGENT]
