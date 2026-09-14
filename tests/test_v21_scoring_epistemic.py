"""
tests/test_v21_scoring_epistemic.py — v21 F1 检索乘数守卫
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
验收（任务书 §三-2.3/2.5，红→绿）：
  1. user_provided 与同分 reasoned 的终态分按 1.15/0.85 拉开
  2. 缺列 / None / 未登记值一律 ×1.00（存量零回归）
  3. env 覆盖生效，非法值 fail-closed
  4. 乘数被如实写回候选（_epistemic_mult 探针字段）
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ducky.scoring import DEFAULT_WEIGHTS, _epistemic_factor, _score_one_candidate  # noqa: E402
from ducky.epistemic import load_epistemic_multipliers  # noqa: E402

_BASE_ITEM = {
    "id": "m1",
    "memory": "用户喜欢用 Python 写后端服务",
    "score": 0.9,
    "created_at": time.time() - 3600,
    "updated_at": time.time() - 3600,
}


def _score(mode):
    item = dict(_BASE_ITEM)
    if mode is not None:
        item["epistemic_mode"] = mode
    kept, _ = _score_one_candidate(
        "Python 后端", item,
        w=DEFAULT_WEIGHTS, now_ts=time.time(), is_fact_query=False,
        type_decay_on=False, salience_map={}, type_map={},
        memory_type_filter=None, gate_on=False,
        epistemic_mult=load_epistemic_multipliers(env={}),
    )
    return kept


def test_user_provided_outranks_reasoned_at_same_evidence():
    up = _score("user_provided")
    rn = _score("reasoned")
    assert up["_hybrid_score"] > rn["_hybrid_score"]
    assert up["_epistemic_mult"] == 1.15
    assert rn["_epistemic_mult"] == 0.85
    # 比例如实（同一底分，只允许乘数差异）
    ratio = up["_hybrid_score"] / rn["_hybrid_score"]
    assert abs(ratio - (1.15 / 0.85)) < 0.01


def test_missing_or_unknown_mode_is_zero_change():
    for mode in (None, "", "bogus", "USER_PROVIDED"):
        item = _score(mode)
        assert item["_epistemic_mult"] == 1.0, f"{mode!r} 必须零变化"


def test_epistemic_factor_fail_closed():
    assert _epistemic_factor({"epistemic_mode": "reasoned"}, {}) == 1.0
    assert _epistemic_factor({"epistemic_mode": "reasoned"}, None) == 1.0
    assert _epistemic_factor({"epistemic_mode": "reasoned"}, {"reasoned": "bad"}) == 1.0
    assert _epistemic_factor({"epistemic_mode": "reasoned"}, {"reasoned": 9.9}) == 1.0
    assert _epistemic_factor({}, {"reasoned": 0.5}) == 1.0
    assert _epistemic_factor({"epistemic_mode": None}, {"reasoned": 0.5}) == 1.0


def test_env_override_applies():
    mult = load_epistemic_multipliers(env={"AIDUMEI_EPISTEMIC_MULT_REASONED": "0.95"})
    assert _epistemic_factor({"epistemic_mode": "reasoned"}, mult) == 0.95
