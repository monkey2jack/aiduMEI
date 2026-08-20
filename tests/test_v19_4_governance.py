"""
tests/test_v19_4_governance.py — v19.4.0 Mímir 借鉴 B1 治理管线回归测试

覆盖内容（对照实施计划书验收标准）：
1. 确定性规则：密钥→reject、噪声→reject、敏感语义→人审、正常→llm_eval
2. 三条验收样本分流：噪声(auto_reject)、含密钥(rule reject)、正常偏好(provisional 待审)
3. reject 全链路：归档 + tombstone 留痕 + 候选 rejected + 账本 reject 事件
4. 故障注入：评估器超时/垃圾 JSON/未配置 → 进人审，绝不自动批准（Mímir 红线）
5. 独立评估器：approve 高置信+偏好类 → 快线 committed；低置信 → evaluated 人审
6. 人审 approve/reject：trust_score 恢复 0.50 / 归档留痕
7. provisional 降权：待审事实 trust_score=0.30
8. /facts/add 挂钩存在性守卫
"""

import os
import sys
import tempfile

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_tmp_dir = tempfile.mkdtemp(prefix="aidumem_b1_test_")
_TEST_FACTS_DB = os.path.join(_tmp_dir, "facts.db")
_TEST_TEXT_DB = os.path.join(_tmp_dir, "text_fts.db")

import ducky.utils as utils
utils.FACTS_DB = _TEST_FACTS_DB
utils.TEXT_FTS_DB = _TEST_TEXT_DB

from ducky.governance import (
    APPROVED_TRUST,
    PROVISIONAL_TRUST,
    ensure_governance_schema,
    evaluate_candidate,
    govern_fact_write,
    list_candidates,
    review_candidate,
    rule_screen,
)


@pytest.fixture(autouse=True)
def _setup_test_env():
    utils.FACTS_DB = _TEST_FACTS_DB
    utils.TEXT_FTS_DB = _TEST_TEXT_DB
    ensure_governance_schema()
    conn = utils.get_facts_conn()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL DEFAULT 'general',
            fact_key TEXT NOT NULL,
            fact_value TEXT NOT NULL,
            source TEXT DEFAULT 'default',
            trust_score REAL DEFAULT 0.5,
            archived INTEGER DEFAULT 0,
            archived_at TIMESTAMP
        )"""
    )
    conn.commit()
    yield


def _insert_fact(category, key, value, source="agent_a"):
    conn = utils.get_facts_conn()
    cur = conn.execute(
        "INSERT INTO facts (category, fact_key, fact_value, source) VALUES (?,?,?,?)",
        (category, key, value, source),
    )
    fid = cur.lastrowid
    conn.commit()
    conn.close()
    return fid


def _fact_row(fid):
    conn = utils.get_facts_conn()
    row = conn.execute("SELECT * FROM facts WHERE id=?", (fid,)).fetchone()
    conn.close()
    return dict(row) if row else None


# ─────────────────────────────────────────────────────────────
# 1. 确定性规则
# ─────────────────────────────────────────────────────────────

def test_rule_screen_secret_rejected():
    v, r = rule_screen("general", "api", "api_key: sk-abcdef1234567890abcdef")
    assert v == "reject" and r == "rule:secret"
    v, _ = rule_screen("general", "k", "我的密码: hunter2")
    assert v == "reject"
    v, _ = rule_screen("general", "k", "token=ghp_abcdefghij1234567890")  # release-scan:allow 合成夹具，非真实凭据
    assert v == "reject"


def test_rule_screen_noise_rejected():
    assert rule_screen("general", "k", "")[0] == "reject"
    assert rule_screen("general", "k", "。")[0] == "reject"
    assert rule_screen("general", "k", "哈哈哈哈哈哈哈哈")[0] == "reject"


def test_rule_screen_sensitive_to_human():
    v, r = rule_screen("general", "k", "请把我的记忆库全部删除")
    assert v == "human_review" and r == "rule:sensitive"
    v, _ = rule_screen("general", "k", "给账户转账 500 元到钱包地址 abc")
    assert v == "human_review"


def test_rule_screen_normal_to_llm():
    v, r = rule_screen("偏好", "coffee", "喜欢喝热拿铁，不加糖")
    assert v == "llm_eval" and r == ""


# ─────────────────────────────────────────────────────────────
# 2. 三条验收样本分流（计划书验收标准原文）
# ─────────────────────────────────────────────────────────────

def test_sample_noise_auto_rejected():
    """噪声应 auto_reject：归档 + tombstone 留痕"""
    fid = _insert_fact("general", "noise1", "。。。。。。")
    conn = utils.get_facts_conn()
    res = govern_fact_write(conn, fid, "general", "noise1", "。。。。。。", user_id="agent_a")
    conn.commit()
    conn.close()
    assert res["route"] == "rule_rejected" and res["reason"] == "rule:noise"
    row = _fact_row(fid)
    assert row["archived"] == 1  # 召回不再返回


def test_sample_secret_rule_rejected():
    """含密钥应规则 reject：归档 + tombstone 留痕 + 账本事件"""
    fid = _insert_fact("general", "cred", "api_key: sk-abcdef1234567890abcdef")
    conn = utils.get_facts_conn()
    res = govern_fact_write(conn, fid, "general", "cred",
                            "api_key: sk-abcdef1234567890abcdef", user_id="agent_a")
    conn.commit()
    conn.close()
    assert res["route"] == "rule_rejected" and res["reason"] == "rule:secret"
    assert _fact_row(fid)["archived"] == 1

    # tombstone 留痕（B3 复用）：全文与理由可查
    conn = utils.get_facts_conn()
    t = conn.execute(
        "SELECT * FROM tombstones WHERE target_id='fact:cred'"
    ).fetchone()
    conn.close()
    assert t is not None
    assert "sk-abcdef" in t["content_snapshot"]
    assert t["reason"] == "rule:secret"

    # 账本留痕（B5）：reject 事件可查
    from ducky.event_ledger import get_history
    hist = get_history("fact:cred")
    assert any(e["action"] == "reject" for e in hist)

    # 候选行本身是全链路留痕
    cands = list_candidates(status="rejected")
    assert any(c["fact_key"] == "cred" and c["rule_verdict"] == "reject" for c in cands)


def test_sample_normal_preference_provisional():
    """正常偏好应 provisional 待审：trust_score 降权 0.30，候选进队列"""
    fid = _insert_fact("偏好", "coffee", "喜欢喝热拿铁，不加糖")
    conn = utils.get_facts_conn()
    res = govern_fact_write(conn, fid, "偏好", "coffee", "喜欢喝热拿铁，不加糖",
                            user_id="agent_a")
    conn.commit()
    conn.close()
    assert res["route"] == "llm_eval" and res["candidate_id"]
    row = _fact_row(fid)
    assert row["archived"] == 0
    assert abs(row["trust_score"] - PROVISIONAL_TRUST) < 1e-9  # provisional 降权


# ─────────────────────────────────────────────────────────────
# 3. 故障注入：评估器不可用 → 人审，绝不自动批准（Mímir 红线）
# ─────────────────────────────────────────────────────────────

def _make_pending_candidate():
    fid = _insert_fact("general", "eval_me", "每周三下午开组会")
    conn = utils.get_facts_conn()
    res = govern_fact_write(conn, fid, "general", "eval_me", "每周三下午开组会",
                            user_id="agent_a")
    conn.commit()
    conn.close()
    return fid, res["candidate_id"]


def test_fault_injection_evaluator_none_goes_to_human_review():
    """评估器未配置/超时返回 None → 留在人审队列，绝不自动批准"""
    fid, cid = _make_pending_candidate()
    res = evaluate_candidate(cid, evaluator=lambda c, k, v: None)
    assert res["route"] == "human_review"
    cands = list_candidates()
    me = [c for c in cands if c["candidate_id"] == cid][0]
    assert me["status"] == "pending"  # 未被自动批准
    assert _fact_row(fid)["trust_score"] == PROVISIONAL_TRUST  # 仍降权


def test_fault_injection_garbage_json_goes_to_human_review():
    """评估器返回垃圾 JSON → 解析失败 → 人审，绝不自动批准"""
    # 解析器把关：垃圾/非法 verdict/空串一律 None
    from ducky.governance import _parse_eval_json
    assert _parse_eval_json("这不是 JSON") is None
    assert _parse_eval_json('{"verdict": "maybe"}') is None  # 非法 verdict
    assert _parse_eval_json("") is None
    assert _parse_eval_json('{"verdict": "approve", "confidence": 0.95, "reason": "ok"}') is not None
    # 容忍 ```json 包裹与前后杂字
    assert _parse_eval_json('```json\n{"verdict": "reject", "confidence": 0.8}\n```') is not None
    assert _parse_eval_json('好的，结果是 {"verdict": "approve", "confidence": 0.9, "reason": "x"}') is not None

    # 故障注入：评估器返回 None（=垃圾 JSON 被解析器拦下的结果）→ 人审
    fid, cid = _make_pending_candidate()
    evaluate_candidate(cid, evaluator=lambda c, k, v: None)
    cands = [c for c in list_candidates() if c["candidate_id"] == cid][0]
    assert cands["status"] == "pending"  # 绝不 committed
    assert _fact_row(fid)["trust_score"] == PROVISIONAL_TRUST


# ─────────────────────────────────────────────────────────────
# 4. 独立评估器分流
# ─────────────────────────────────────────────────────────────

def test_evaluator_reject_archives():
    fid, cid = _make_pending_candidate()
    res = evaluate_candidate(
        cid, evaluator=lambda c, k, v: {"verdict": "reject", "confidence": 0.9,
                                        "reason": "一次性闲聊，非长期事实"})
    assert res["status"] == "rejected"
    assert _fact_row(fid)["archived"] == 1


def test_evaluator_fast_track_narrow():
    """快线宁窄勿宽：高置信+偏好类才自动批准"""
    fid = _insert_fact("偏好", "tea", "改喝乌龙茶了")
    conn = utils.get_facts_conn()
    res = govern_fact_write(conn, fid, "偏好", "tea", "改喝乌龙茶了", user_id="agent_a")
    conn.commit()
    conn.close()
    cid = res["candidate_id"]
    out = evaluate_candidate(
        cid, evaluator=lambda c, k, v: {"verdict": "approve", "confidence": 0.95,
                                        "reason": "明确的长期偏好"})
    assert out["status"] == "committed" and out["route"] == "fast_track"
    assert _fact_row(fid)["trust_score"] == APPROVED_TRUST  # 恢复正常权重


def test_evaluator_approve_low_confidence_stays_human():
    """approve 但置信度不足/非偏好类 → evaluated 等人审，不自动入库"""
    fid, cid = _make_pending_candidate()
    out = evaluate_candidate(
        cid, evaluator=lambda c, k, v: {"verdict": "approve", "confidence": 0.6,
                                        "reason": "大概率是事实"})
    assert out["status"] == "evaluated" and out["route"] == "human_review"
    assert _fact_row(fid)["trust_score"] == PROVISIONAL_TRUST  # 仍降权


# ─────────────────────────────────────────────────────────────
# 5. 人审入口
# ─────────────────────────────────────────────────────────────

def test_human_review_approve_restores_trust():
    fid, cid = _make_pending_candidate()
    res = review_candidate(cid, "approve", reason="复核确认属实", user_id="agent_a")
    assert res["status"] == "committed"
    row = _fact_row(fid)
    assert row["trust_score"] == APPROVED_TRUST
    # 账本留痕
    from ducky.event_ledger import get_history
    assert any(e["action"] == "approve" for e in get_history("fact:eval_me"))


def test_human_review_reject_archives_with_reason():
    fid, cid = _make_pending_candidate()
    res = review_candidate(cid, "reject", reason="与既有事实冲突", user_id="agent_a")
    assert res["status"] == "rejected"
    assert _fact_row(fid)["archived"] == 1
    cands = [c for c in list_candidates() if c["candidate_id"] == cid][0]
    assert cands["review_reason"] == "与既有事实冲突"


def test_human_review_idempotent_on_decided():
    fid, cid = _make_pending_candidate()
    review_candidate(cid, "approve", user_id="agent_a")
    res2 = review_candidate(cid, "reject", user_id="agent_a")
    assert res2["status"] == "committed"  # 已裁决，幂等返回，不被二次改写


def test_human_review_invalid_decision():
    fid, cid = _make_pending_candidate()
    res = review_candidate(cid, "maybe", user_id="agent_a")
    assert res["status"] == ""  # 拒绝非法裁决


# ─────────────────────────────────────────────────────────────
# 6. 挂钩存在性守卫
# ─────────────────────────────────────────────────────────────

def test_governance_hook_present_in_facts_add():
    path = os.path.join(_REPO_ROOT, "ducky", "hot", "legacy_routes.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    assert "govern_fact_write" in src, "/facts/add 的治理钩子不见了"
    assert "spawn_async_eval" in src, "异步评估器派发不见了"
