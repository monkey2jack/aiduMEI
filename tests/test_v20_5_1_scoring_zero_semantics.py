"""
tests/test_v20_5_1_scoring_zero_semantics.py — T-17：打分因子「显式 0 ≠ 缺失」
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ducky/scoring.py 三处 `x or default` 把**显式 0** 吞成兜底值：
  · :355 `_bm25_factor`      上游给了 bm25_score=0（真算过、就是零重合），
                             被 `or` 换成现算的 token 覆盖率 —— 显式结果被静默覆盖；
  · :368 `_reliability_factor`  显式 reliability=0（不可信）被改成 0.5（中性）——
                             「最不可信」被打成「不好不坏」，方向性错误；
  · :374 `_heat_factor`      metadata 显式 access_count=0 被换成 salience 兜底。

修法：缺失（None）才兜底，显式 0 原样进入有限性闸门。其余行为逐字不变。

判据（先红后绿）：同一候选的两种输入（显式 0 / 键缺失）输出必须**可区分**；
实现前三条因子用例全红（旧代码两种输入输出相同）。
"""
from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ducky.scoring import (  # noqa: E402
    DEFAULT_WEIGHTS,
    _bm25_factor,
    _heat_factor,
    _reliability_factor,
    score_and_rank_candidates,
)

QUERY = "拿铁"
TEXT = "用户喜欢喝热拿铁"  # 对 QUERY 的 token 覆盖率 = 1.0（「拿铁」整段命中）


@pytest.fixture(autouse=True)
def _rerank_off(monkeypatch):
    """断分数的用例一律摘掉 rerank 腿（test_recall_min_score_gate 的踩坑沉淀：
    环境里有凭据时重排真融合，同一条候选分数就变了）。"""
    def _no_rerank(*a, **kw):
        return []
    monkeypatch.setattr("ducky.mem0_runtime.rerank", _no_rerank, raising=False)


# ── 1. 因子级：显式 0 与缺失必须可区分 ─────────────────────────────

def test_bm25_factor_explicit_zero_not_replaced_by_computed():
    item = {"metadata": {"bm25_score": 0}}
    assert _bm25_factor(QUERY, item, TEXT) == 0.0, (
        "上游显式给了 0（零重合的诚实结果），不许被现算覆盖率偷换"
    )
    missing = _bm25_factor(QUERY, {"metadata": {}}, TEXT)
    assert missing == 1.0, f"缺失时才现算覆盖率，应得 1.0，实得 {missing}"


def test_reliability_factor_explicit_zero_stays_zero():
    item = {"metadata": {"reliability": 0}}
    assert _reliability_factor(item) == 0.0, (
        "显式 0 = 不可信；被兜成 0.5 是把「最不可信」打成「中性」"
    )
    assert _reliability_factor({"metadata": {}}) == 0.5, "缺失才兜 0.5"


def test_heat_factor_explicit_zero_not_replaced_by_salience():
    item = {"metadata": {"access_count": 0}}
    assert _heat_factor(item, {"access_count": 7}) == 0.0, (
        "metadata 显式 0 时不许回落 salience 的 7"
    )
    # 缺失时回落链逐字不变：metadata 缺 → salience；两层都缺 → 1
    assert _heat_factor({"metadata": {}}, {"access_count": 7}) == 0.07
    assert _heat_factor({"metadata": {}}, {}) == 0.01


def test_metadata_none_key_treated_as_missing():
    """显式 None 视同缺失（None 不是数值，进有限性闸门也是兜底）。"""
    assert _reliability_factor({"metadata": {"reliability": None}}) == 0.5
    assert _bm25_factor(QUERY, {"metadata": {"bm25_score": None}}, TEXT) == 1.0
    assert _heat_factor({"metadata": {"access_count": None}}, {"access_count": 7}) == 0.07


# ── 2. 端到端：同一条候选，两种输入打出两个分 ──────────────────────

def _mk(metadata: dict | None) -> dict:
    it = {
        "id": "m-zero",
        "memory": "zz 的正文",       # 对 NEUTRAL 查询覆盖率 0，隔离 bm25 支路
        "score": 0.8,
        "created_at": time.time(),
    }
    if metadata is not None:
        it["metadata"] = metadata
    return it


def test_pipeline_distinguishes_explicit_zero_from_missing():
    """显式全 0 与不带 metadata 的同一条候选，终态分必须不同且差值可解释：
    差 = w_reliability×0.5 + w_heat×0.01（缺失兜底 0.5 与 1/100 的贡献）。"""
    query = "随便问点别的内容"
    explicit_zero = _mk({"bm25_score": 0, "reliability": 0,
                         "access_count": 0, "memory_type": "FACTS"})
    missing = _mk(None)

    out_zero = score_and_rank_candidates(query, [explicit_zero], limit=5)
    out_missing = score_and_rank_candidates(query, [missing], limit=5)
    assert len(out_zero) == len(out_missing) == 1, "vec=0.8 过了证据闸门，两条都该在"

    s_zero, s_missing = out_zero[0]["_hybrid_score"], out_missing[0]["_hybrid_score"]
    expected_delta = DEFAULT_WEIGHTS["reliability"] * 0.5 + DEFAULT_WEIGHTS["heat"] * 0.01
    assert abs((s_missing - s_zero) - expected_delta) < 0.002, (
        f"显式 0 与缺失的分差应≈{expected_delta:.4f}，实得 {s_missing - s_zero:.4f} —— "
        "要么显式 0 仍被吞（修复没生效），要么缺失兜底改了（越界改动）"
    )
