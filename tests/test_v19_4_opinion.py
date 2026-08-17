"""
tests/test_v19_4_opinion.py — v19.4.0 Mímir 借鉴 B6 信念层回归测试

覆盖内容（对照实施计划书验收标准）：
1. 三态信念（support/oppose/neutral）各写入一条成功——不许只有 support
2. 单来源 3 条同 stance 不触发聚合（Mímir 回声室教训）
3. 双来源触发聚合
4. 同源同事实 upsert 覆盖（UNIQUE(fact_id, source)，防同源刷票）
5. 非法 stance / 空 source 拒绝
6. 信念写入走 B5 账本留痕（action=opinion_set）
"""

import os
import sys
import tempfile

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_tmp_dir = tempfile.mkdtemp(prefix="aidumem_b6_test_")
_TEST_FACTS_DB = os.path.join(_tmp_dir, "facts.db")
_TEST_TEXT_DB = os.path.join(_tmp_dir, "text_fts.db")

import ducky.utils as utils
utils.FACTS_DB = _TEST_FACTS_DB
utils.TEXT_FTS_DB = _TEST_TEXT_DB

from ducky.opinion import (
    STANCES,
    aggregate_opinion,
    ensure_opinion_schema,
    list_opinions,
    set_opinion,
)


@pytest.fixture(autouse=True)
def _setup_test_env():
    utils.FACTS_DB = _TEST_FACTS_DB
    utils.TEXT_FTS_DB = _TEST_TEXT_DB
    ensure_opinion_schema()
    yield


# ─────────────────────────────────────────────────────────────
# 1. 三态写入（验收标准：不许只有 support）
# ─────────────────────────────────────────────────────────────

def test_three_stances_all_writable():
    for i, stance in enumerate(STANCES, start=1):
        res = set_opinion(fact_id=100 + i, stance=stance, confidence=0.7,
                          source=f"src_{stance}", owner="agent_a")
        assert res["ok"], f"{stance} 写入失败: {res['detail']}"
    # 三态各查出一条
    for i, stance in enumerate(STANCES, start=1):
        rows = list_opinions(100 + i)
        assert len(rows) == 1 and rows[0]["stance"] == stance


def test_invalid_stance_rejected():
    res = set_opinion(fact_id=1, stance="love", source="s1")
    assert not res["ok"] and "stance" in res["detail"]


def test_empty_source_rejected():
    res = set_opinion(fact_id=1, stance="support", source="")
    assert not res["ok"] and "source" in res["detail"]


# ─────────────────────────────────────────────────────────────
# 2. 同源 upsert：防单来源刷票
# ─────────────────────────────────────────────────────────────

def test_same_source_upsert_no_duplicate():
    """同一来源对同一事实只留一条最新信念"""
    set_opinion(fact_id=200, stance="support", confidence=0.6, source="agent_a")
    set_opinion(fact_id=200, stance="oppose", confidence=0.8, source="agent_a")
    rows = list_opinions(200)
    assert len(rows) == 1
    assert rows[0]["stance"] == "oppose"  # 被最新覆盖
    assert abs(rows[0]["confidence"] - 0.8) < 1e-9


# ─────────────────────────────────────────────────────────────
# 3. 聚合规则（Mímir 回声室教训）
# ─────────────────────────────────────────────────────────────

def test_single_source_three_same_stance_no_aggregate():
    """单来源 3 条同 stance 不触发聚合（刷好评无效）"""
    # 同源 upsert 后其实只剩 1 条；即便构造 3 个不同 fact 也无用——
    # 聚合按 fact_id 维度，这里直接验证同源单事实
    set_opinion(fact_id=300, stance="support", confidence=0.9, source="echo_bot")
    agg = aggregate_opinion(300)
    assert agg["aggregated"] is False
    assert agg["reason"] == "insufficient_sources"
    assert agg["distinct_sources"] == 1


def test_two_sources_trigger_aggregate():
    """双来源触发聚合"""
    set_opinion(fact_id=400, stance="support", confidence=0.9, source="agent_a")
    set_opinion(fact_id=400, stance="support", confidence=0.7, source="tool_log")
    agg = aggregate_opinion(400)
    assert agg["aggregated"] is True
    assert agg["stance"] == "support"
    assert agg["distinct_sources"] == 2
    assert abs(agg["confidence"] - 0.8) < 1e-3  # 均值


def test_two_sources_conflict_majority_wins():
    """双来源不同 stance：多数票；1v1 平票保守落 neutral"""
    set_opinion(fact_id=500, stance="support", confidence=0.9, source="s1")
    set_opinion(fact_id=500, stance="oppose", confidence=0.8, source="s2")
    agg = aggregate_opinion(500)
    assert agg["aggregated"] is True
    assert agg["stance"] == "neutral"  # 平票保守态

    set_opinion(fact_id=500, stance="oppose", confidence=0.7, source="s3")
    agg2 = aggregate_opinion(500)
    assert agg2["stance"] == "oppose"  # 2v1 多数票
    assert agg2["votes"] == {"support": 1, "oppose": 2}


# ─────────────────────────────────────────────────────────────
# 4. 账本留痕（B5 咬合）
# ─────────────────────────────────────────────────────────────

def test_opinion_write_records_ledger_event():
    set_opinion(fact_id=600, stance="neutral", confidence=0.5,
                source="observer", owner="agent_a")
    from ducky.event_ledger import get_history
    hist = get_history("fact:600")
    assert any(e["action"] == "opinion_set" and "stance=neutral" in e["reason"]
               for e in hist)
