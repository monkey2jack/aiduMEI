"""
tests/test_v21_2_memmy_fusion.py — v21.2 Memmy 融改验收守卫（M1/M2/M4/M6/M7/M8）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
每条都是任务计划书里写死的验收判据，红→绿：

  M2 回声抑制  — schema v9 三列、写入落值、同 session 滤掉、换 session 召回、
                 开关关闭负向对照、存量空值不误杀
  M4 MMR      — 近义簇被打散、异主题进榜、开关关闭逐条等价、limit 边界
  M6 错误签名  — 抽取出 errsig、查询侧识别与加分、中文查询零影响
  M1 轨迹奖励  — 权重公式（和为 1 / 越靠后越重）、结算回传、无 session 不记、
                 credit 权重默认 0 → 排序零变化
  M7 rollup   — 默认关；开了同 episode 聚合且与单条去重
  M8 借阅     — grant/revoke 进事件账本、dossier 第八节在场
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp_dir = tempfile.mkdtemp(prefix="aidumem_v212_")

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
    for k in ("AIDUMEI_ECHO_SUPPRESS", "AIDUMEI_MMR_ENABLED", "AIDUMEI_MMR_LAMBDA",
              "AIDUMEI_CREDIT_WEIGHT", "AIDUMEI_ROLLUP_ENABLED", "AIDUMEI_ERRSIG_BONUS"):
        os.environ.pop(k, None)
    yield
    for k in ("AIDUMEI_ECHO_SUPPRESS", "AIDUMEI_MMR_ENABLED", "AIDUMEI_MMR_LAMBDA",
              "AIDUMEI_CREDIT_WEIGHT", "AIDUMEI_ROLLUP_ENABLED", "AIDUMEI_ERRSIG_BONUS"):
        os.environ.pop(k, None)


# ══════════════════ M2 回声抑制 ══════════════════

def test_m2_schema_v9_origin_columns():
    """schema 迁到 v9：sidecar 带溯源三列，且 user_version 就是 9。"""
    conn = utils.get_facts_conn()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_epistemic)")}
        assert {"origin_session_id", "origin_agent", "origin_turn"} <= cols
        assert conn.execute("PRAGMA user_version").fetchone()[0] >= 9
    finally:
        conn.close()


def test_m2_stamp_writes_origin_from_context():
    """写入侧：origin 显式传入即落列（复位后再读上下文会读到空——所以必须显式）。"""
    from ducky.epistemic import stamp_memory_refs
    n = stamp_memory_refs(["ref-echo-1"], "user_provided", user_id="dudu",
                          bank_id="default", source="add",
                          origin=("hermes", "sess-A", 3))
    assert n == 1
    conn = utils.get_facts_conn()
    try:
        row = conn.execute(
            "SELECT origin_session_id, origin_agent, origin_turn FROM memory_epistemic "
            "WHERE memory_ref=?", ("ref-echo-1",)).fetchone()
    finally:
        conn.close()
    assert row[0] == "sess-A" and row[1] == "hermes" and row[2] == 3


def test_m2_same_session_filtered_other_session_kept():
    """同 session 写入的条目被滤；换 session 正常在。"""
    from ducky.epistemic import stamp_memory_refs
    from ducky.scoring import _load_echo_refs, _drop_echo
    stamp_memory_refs(["ref-A"], "user_provided", user_id="dudu", bank_id="default",
                      origin=("hermes", "sess-A", 1))
    stamp_memory_refs(["ref-B"], "user_provided", user_id="dudu", bank_id="default",
                      origin=("hermes", "sess-B", 1))
    cands = [{"id": "ref-A", "memory": "甲"}, {"id": "ref-B", "memory": "乙"}]
    echo = _load_echo_refs(cands, "sess-A", "dudu", "default")
    assert echo == {"ref-A"}
    kept = _drop_echo(cands, echo)
    assert [c["id"] for c in kept] == ["ref-B"]


def test_m2_empty_session_never_filters():
    """session_id 为空 = 无从判断，一律不过滤（存量调用方零破坏）。"""
    from ducky.scoring import _load_echo_refs
    assert _load_echo_refs([{"id": "x", "memory": "甲"}], "", "dudu", "default") == set()


def test_m2_legacy_rows_without_session_not_dropped():
    """存量行 origin_session_id 为空 —— 任何 session 下都不许被误杀。"""
    from ducky.epistemic import stamp_memory_refs
    from ducky.scoring import _load_echo_refs
    stamp_memory_refs(["ref-legacy"], "fuzzy", user_id="dudu", bank_id="default")
    echo = _load_echo_refs([{"id": "ref-legacy", "memory": "老记忆"}],
                           "sess-A", "dudu", "default")
    assert echo == set()


def test_m2_switch_off_is_negative_control():
    """负向对照：开关关掉，回声抑制整条规则不生效。"""
    from ducky.scoring import echo_suppress_enabled
    assert echo_suppress_enabled() is True
    os.environ["AIDUMEI_ECHO_SUPPRESS"] = "0"
    assert echo_suppress_enabled() is False


def test_m2_cross_domain_isolation():
    """域隔离：别的殿写的同名 session 不许影响本殿。"""
    from ducky.epistemic import stamp_memory_refs
    from ducky.scoring import _load_echo_refs
    stamp_memory_refs(["ref-other"], "user_provided", user_id="xiaoli",
                      bank_id="default", origin=("hermes", "sess-A", 1))
    echo = _load_echo_refs([{"id": "ref-other", "memory": "他殿"}],
                           "sess-A", "dudu", "default")
    assert echo == set()


def test_m2_verbatim_leg_session_key_aligned():
    """原文腿的 session 口径必须与 origin_context / sidecar 对齐。

    实机冒烟暴露过：向量腿滤掉了本会话刚写入的那句话，原文腿（打分之后
    才融合）又把它原样送了回来 —— 因为 verbatim 写入只认 `session_id` /
    `conversation_id`，不认写入管道实际透传的 `_origin_session_id`。
    键名不对不会报错，只会让整个功能静默失效。
    """
    import inspect
    from ducky import verbatim_vault
    src = inspect.getsource(verbatim_vault)
    assert "_origin_session_id" in src, (
        "verbatim 写入未认 _origin_session_id —— 回声抑制会在原文腿上漏")


def test_m2_search_route_filters_verbatim_echo():
    """/search 融合原文腿之前必须按同一规则滤掉本会话自己的原文。"""
    import inspect
    from ducky.hot import search as hs
    src = inspect.getsource(hs)
    i = src.find("verbatim_search(")
    j = src.find("fuse_verbatim(", i)
    assert i != -1 and j != -1, "原文融合段落找不到 —— 守卫失去着力点"
    between = src[i:j]
    assert "echo_suppress_enabled" in between and "session_id" in between, (
        "verbatim_search 与 fuse_verbatim 之间没有回声过滤 —— "
        "打分后融合的腿会把滤掉的那句话原样送回")


# ══════════════════ M4 MMR ══════════════════

def _near_dup_pool():
    return [{"id": f"m{i}", "memory": t, "_hybrid_score": s} for i, (t, s) in enumerate([
        ("用户喜欢喝美式咖啡不加糖", 0.90), ("用户爱喝美式咖啡不加糖的", 0.89),
        ("用户喝咖啡习惯是美式不加糖", 0.88), ("用户的咖啡偏好美式不加糖", 0.87),
        ("用户常点美式咖啡不放糖", 0.86),
        ("机房服务器跑的是 aiduMEI 记忆服务", 0.60)])]


def test_m4_mmr_promotes_second_cluster():
    """5 条近义 + 1 条异主题 → top5 里异主题必须在（至少两个语义簇）。"""
    from ducky.scoring import mmr_select
    top5 = mmr_select(_near_dup_pool(), 5)
    assert any("机房" in c["memory"] for c in top5)


def test_m4_switch_off_equals_score_truncation():
    """负向对照：关掉 MMR，逐条等于按分截断（零回归证明）。"""
    from ducky.scoring import mmr_select
    os.environ["AIDUMEI_MMR_ENABLED"] = "0"
    pool = _near_dup_pool()
    assert [c["id"] for c in mmr_select(list(pool), 3)] == [c["id"] for c in pool[:3]]


def test_m4_limit_boundaries():
    """边界：limit<=0 空表；limit>=候选数原样返回。"""
    from ducky.scoring import mmr_select
    pool = _near_dup_pool()
    assert mmr_select(list(pool), 0) == []
    assert len(mmr_select(list(pool), 99)) == len(pool)


def test_m4_illegal_lambda_falls_back():
    """非法 lambda fail-closed 回默认 0.7，不炸检索。"""
    from ducky.scoring import _mmr_config, MMR_LAMBDA_DEFAULT
    os.environ["AIDUMEI_MMR_LAMBDA"] = "不是数字"
    assert _mmr_config()[1] == MMR_LAMBDA_DEFAULT
    os.environ["AIDUMEI_MMR_LAMBDA"] = "9.9"      # 越界
    assert _mmr_config()[1] == MMR_LAMBDA_DEFAULT


def test_m4_ignited_items_exempt_from_redundancy_not_from_ranking():
    """点火条豁免的是**冗余惩罚**，不是排序本身。

    判据要能分辨两种语义（否则是白护栏）：
      · 正向：一条近义记忆本会因冗余被挤掉，点火后活下来
      · 负向：点火不许让低分条压过高分条（无条件占位是错的）
    """
    from ducky.scoring import mmr_select
    base = [{"id": "a", "memory": "用户喜欢喝美式咖啡不加糖", "_hybrid_score": 0.90},
            {"id": "b", "memory": "用户爱喝美式咖啡不加糖的", "_hybrid_score": 0.89},
            {"id": "c", "memory": "机房服务器跑的是 aiduMEI 记忆服务", "_hybrid_score": 0.60}]
    # 不点火：b 与 a 近义 → 多样性让 c 上位
    plain = [dict(x) for x in base]
    assert [x["id"] for x in mmr_select(plain, 2)] == ["a", "c"]
    # b 点火：豁免冗余惩罚 → 凭 0.89 的分活下来
    ign = [dict(x) for x in base]
    ign[1]["_ignited"] = True
    assert [x["id"] for x in mmr_select(ign, 2)] == ["a", "b"]
    # 负向对照：点火的**低分**条不许压过高分条
    low = [dict(x) for x in base]
    low[2]["_ignited"] = True
    assert [x["id"] for x in mmr_select(low, 1)] == ["a"]


# ══════════════════ M6 错误签名 ══════════════════

def test_m6_extract_error_signature_and_code():
    """写入侧：traceback 抽出 errsig，错误码抽成 code:N。"""
    from ducky.pattern_extract import extract_patterns
    items = extract_patterns(
        "部署报错 ModuleNotFoundError: No module named ducky，errno 2")
    keys = {it["fact_key"] for it in items if it["kind"] == "errsig"}
    assert "ModuleNotFoundError" in keys
    assert "code:2" in keys


def test_m6_query_side_detection_and_bonus():
    """检索侧：查询里有报错名 → 正文命中的候选拿有界 bonus。"""
    from ducky.scoring import extract_error_signatures, _errsig_factor
    sigs = extract_error_signatures("ModuleNotFoundError ducky 怎么修")
    assert sigs == ("ModuleNotFoundError",)
    assert _errsig_factor("报错 ModuleNotFoundError: No module named ducky", sigs) > 0
    assert _errsig_factor("完全无关的一段话", sigs) == 0.0


def test_m6_chinese_query_unaffected():
    """普通中文查询识别不到签名 → 整条规则不参与打分（零回归）。"""
    from ducky.scoring import extract_error_signatures
    assert extract_error_signatures("用户喜欢喝什么咖啡") == ()


def test_m6_bonus_is_bounded():
    """加分有界，非法配置 fail-closed 回默认。"""
    from ducky.scoring import _errsig_bonus, ERRSIG_BONUS_DEFAULT
    os.environ["AIDUMEI_ERRSIG_BONUS"] = "abc"
    assert _errsig_bonus() == ERRSIG_BONUS_DEFAULT
    os.environ["AIDUMEI_ERRSIG_BONUS"] = "5"
    assert _errsig_bonus() == ERRSIG_BONUS_DEFAULT


# ══════════════════ M1 轨迹级奖励 ══════════════════

def test_m1_credit_weights_sum_to_one_and_increase():
    """权重公式：和恒为 1；越靠近结果的步骤权重越大。"""
    from ducky.evolve_mem import credit_weights
    assert credit_weights(0) == []
    assert credit_weights(1) == [1.0]
    w = credit_weights(5)
    assert abs(sum(w) - 1.0) < 1e-6
    assert w == sorted(w), "γ 位置衰减方向反了：越靠后应越重"


def test_m1_episode_settle_propagates_by_position(tmp_path, monkeypatch):
    """3 步 episode 负反馈 → 每步 credit 按位置递减（越靠后担责越重）。"""
    import ducky.evolve_mem as em
    monkeypatch.setattr(em, "EVOLVE_DB_PATH", str(tmp_path / "evolve.db"))
    em.ensure_evolve_schema()
    sid = "sess-ep-1"
    for ref in ("s1", "s2", "s3"):
        assert em.record_episode_step(sid, [ref], user_id="dudu", bank_id="default") == 1
    res = em.record_episode_feedback(sid, -1.0)
    assert res["ok"] and res["steps"] == 3
    cm = em.get_credit_map(["s1", "s2", "s3"])
    assert abs(cm["s1"]) < abs(cm["s2"]) < abs(cm["s3"])


def test_m1_no_session_no_episode(tmp_path, monkeypatch):
    """无 session 的写入（cron 类）不产生 episode —— 不污染轨迹统计。"""
    import ducky.evolve_mem as em
    monkeypatch.setattr(em, "EVOLVE_DB_PATH", str(tmp_path / "evolve.db"))
    em.ensure_evolve_schema()
    assert em.record_episode_step("", ["x"]) == 0
    assert em.record_episode_feedback("", 1.0)["ok"] is False


def test_m1_credit_weight_defaults_to_zero():
    """credit 维度默认权重 0 = 排序行为零变化（灰度铁律）。"""
    from ducky.scoring import credit_dimension_weight, _load_credit_map
    assert credit_dimension_weight() == 0.0
    # 权重为 0 时连查询都不发（不生效的维度不许白花一次往返）
    assert _load_credit_map([{"id": "any"}]) == {}


def test_m1_credit_weight_is_bounded():
    """credit 权重有界 [0, 0.5]，非法值回默认 0。"""
    from ducky.scoring import credit_dimension_weight
    os.environ["AIDUMEI_CREDIT_WEIGHT"] = "0.15"
    assert credit_dimension_weight() == 0.15
    os.environ["AIDUMEI_CREDIT_WEIGHT"] = "9"
    assert credit_dimension_weight() == 0.0
    os.environ["AIDUMEI_CREDIT_WEIGHT"] = "abc"
    assert credit_dimension_weight() == 0.0


def test_m1_episode_params_bounded():
    """episode 参数全部 env 可覆盖且有界（上游默认值不可盲信）。"""
    from ducky.evolve_mem import episode_params
    p = episode_params()
    assert 0.0 <= p["gamma"] <= 1.0 and 0.0 <= p["lambda"] <= 1.0
    os.environ["AIDUMEI_EPISODE_GAMMA"] = "99"
    try:
        assert episode_params()["gamma"] == 0.9   # 越界回默认
    finally:
        os.environ.pop("AIDUMEI_EPISODE_GAMMA", None)


# ══════════════════ M7 rollup ══════════════════

def test_m7_rollup_off_by_default():
    """M7 默认关 —— 它依赖 M1 攒数据，先观察再开。"""
    from ducky.recall_funnel import _rollup_enabled
    assert _rollup_enabled() is False
    os.environ["AIDUMEI_ROLLUP_ENABLED"] = "1"
    assert _rollup_enabled() is True


def test_m7_rollup_noop_when_disabled():
    """关闭时 rollup 原样返回，不动任何候选。"""
    from ducky.recall_funnel import _apply_episode_rollup
    items = [{"id": "a", "memory": "甲", "_hybrid_score": 0.9},
             {"id": "b", "memory": "乙", "_hybrid_score": 0.8}]
    out, made = _apply_episode_rollup(list(items), 5)
    assert made == 0 and [x["id"] for x in out] == ["a", "b"]


# ══════════════════ M8 借阅对齐 ══════════════════

def test_m8_grant_writes_event_ledger():
    """借阅授权进事件账本（来源标记可查）。"""
    from ducky.pantheon import grant_hall_access
    from ducky.event_ledger import ensure_ledger_schema, get_history
    ensure_ledger_schema()
    g = grant_hall_access("dudu", "xiaohou", actions="read", created_by="dudu")
    hist = get_history(g["grant_id"])
    assert any(h.get("action") == "hall_grant" for h in hist)


def test_m8_revoke_only_logs_when_actually_revoked():
    """撤销未撤到的 grant 不许记账 —— 账本里不写没发生过的事。"""
    from ducky.pantheon import revoke_hall_grant
    from ducky.event_ledger import ensure_ledger_schema, get_history
    ensure_ledger_schema()
    res = revoke_hall_grant("hg_does_not_exist")
    assert res["revoked"] is False
    assert not [h for h in get_history("hg_does_not_exist")
                if h.get("action") == "hall_grant_revoke"]


def test_m8_dossier_has_grants_section():
    """档案第八节「当前生效借阅」必须在场（人能看见谁在读我的记忆）。"""
    from ducky.dossier import render_markdown
    md = render_markdown({"user_id": "dudu", "bank_id": "default", "sections": {}})
    assert "## 八、当前生效借阅" in md
    assert "无（没有任何殿能读这座殿的记忆" in md
