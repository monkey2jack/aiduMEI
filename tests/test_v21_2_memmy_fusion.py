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
        assert em.record_episode_step([ref], session_id=sid, user_id="dudu", bank_id="default") == 1
    res = em.record_episode_feedback(sid, -1.0)
    assert res["ok"] and res["steps"] == 3
    cm = em.get_credit_map(["s1", "s2", "s3"])
    assert abs(cm["s1"]) < abs(cm["s2"]) < abs(cm["s3"])


def test_m1_no_session_no_episode(tmp_path, monkeypatch):
    """无 session 的写入（cron 类）不产生 episode —— 不污染轨迹统计。"""
    import ducky.evolve_mem as em
    monkeypatch.setattr(em, "EVOLVE_DB_PATH", str(tmp_path / "evolve.db"))
    em.ensure_evolve_schema()
    assert em.record_episode_step(["x"], session_id="") == 0
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


def test_m1_episode_hook_sits_at_the_real_stamping_seam():
    """轨迹登记必须跟着出身打标走同一个缝位。

    实机冒烟暴露过：sidecar 里 origin_session_id 有值、episode 表却是空的 ——
    因为主链路的打标发生在 layer1 的 `_index_after_add`（包装器吞掉了 mem0 的
    results，路由层拿不到 ref），而轨迹登记只钩在路由层。钩错缝位不会报错，
    只会让功能静默失效。
    """
    import inspect
    from ducky import layer1_selfcheck as l1
    src = inspect.getsource(l1._index_after_add)
    assert "stamp_memory_refs" in src, "打标缝位变了 —— 守卫失去着力点"
    assert "record_episode_step" in src, (
        "layer1 打标缝位没有 episode 登记 —— 主链路写入不会产生轨迹")


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


# ══════════════ v21.2.0 审计整改轮守卫 ══════════════
#
# 由来：2026-09-17 用户审计翻生产库发现 —— v21.2.0 上线后
# memory_epistemic 33 行里 origin_session_id 非空 = 0 行，M2 回声抑制的
# 向量腿与 M1 轨迹登记从上线起就在空转，而 epistemic_ok 一直是绿的。
# 下面每一条都是把「绿着的空转」钉成红字的判据。


def test_origin_from_metadata_prefers_explicit_over_contextvar():
    """显式 > 隐式：metadata 里的保留键优先于 contextvar。

    这是 🔴-1 的加固核心 —— contextvar 只在「调用方先 set 过」且「同一执行
    上下文」两个前提都成立时才对，而空值不报错。metadata 跟着数据走。
    """
    from ducky.origin_context import origin_from_metadata, set_origin, reset_origin
    tok = set_origin(agent="ctx-agent", session_id="ctx-sess", turn=1)
    try:
        # 有 metadata → 用 metadata，不用 contextvar
        assert origin_from_metadata(
            {"_origin_agent": "md-agent", "_origin_session_id": "md-sess",
             "_origin_turn": 5}) == ("md-agent", "md-sess", 5)
        # 无 metadata → 回退 contextvar（不打断已在上下文里工作的调用方）
        assert origin_from_metadata(None) == ("ctx-agent", "ctx-sess", 1)
        assert origin_from_metadata({}) == ("ctx-agent", "ctx-sess", 1)
    finally:
        reset_origin(tok)


def test_origin_from_metadata_tolerates_garbage_turn():
    """turn 非法值不许炸写入主链路（fail-soft 回 0）。"""
    from ducky.origin_context import origin_from_metadata
    assert origin_from_metadata(
        {"_origin_session_id": "s", "_origin_turn": "不是数字"}) == ("", "s", 0)


def test_index_after_add_takes_origin_from_metadata_not_contextvar():
    """`_index_after_add` 必须显式收 metadata 并据此取 origin。

    钉死 🔴-1 的缝位：只要这里退回读 contextvar，任何一条没先 set 的通路
    就会静默把三列写成空 —— 单测全绿、生产全空。
    """
    import inspect
    from ducky import layer1_selfcheck as l1
    sig = inspect.signature(l1._index_after_add)
    assert "metadata" in sig.parameters, "_index_after_add 未接收 metadata"
    src = inspect.getsource(l1._index_after_add)
    assert "origin_from_metadata" in src, "未走显式 origin 解析"
    assert "get_origin()" not in src, (
        "仍在直接读 contextvar —— 隐式通道少一次 set 就静默变空")
    # 调用点必须真的把 metadata 传进去（签名有、调用不传 = 白护栏）
    mod_src = inspect.getsource(l1)
    calls = [ln for ln in mod_src.splitlines() if "_index_after_add(" in ln
             and "def _index_after_add" not in ln and "``" not in ln]
    assert calls, "找不到 _index_after_add 调用点 —— 守卫失去着力点"
    for ln in calls:
        assert "metadata=metadata" in ln, f"调用点没传 metadata: {ln.strip()}"


def test_track_knowledge_evolution_also_takes_explicit_origin():
    """同型加固：演化关系的溯源也不许依赖隐式上下文。"""
    import inspect
    from ducky import layer1_selfcheck as l1
    assert "metadata" in inspect.signature(l1.track_knowledge_evolution).parameters
    src = inspect.getsource(l1.track_knowledge_evolution)
    assert "origin_from_metadata" in src and "get_origin()" not in src


def test_stamping_survives_a_fresh_thread(tmp_path, monkeypatch):
    """跨线程实证（用户审计点名要的判据）：在**新线程**里打标，三列必须非空。

    contextvar 不跨线程 —— 这条用例在改回 `get_origin()` 的实现上必红。
    """
    import threading
    import ducky.utils as u
    db = str(tmp_path / "facts.db")
    monkeypatch.setattr(u, "FACTS_DB", db)
    from ducky.schema_bootstrap import ensure_core_schema
    ensure_core_schema(force=True)

    from ducky.epistemic import stamp_memory_refs
    from ducky.origin_context import origin_from_metadata
    md = {"_origin_agent": "thr-agent", "_origin_session_id": "thr-sess",
          "_origin_turn": 3}
    box = {}

    def _worker():
        # 新线程里没有任何人 set 过 contextvar —— 只有 metadata 能救它
        box["n"] = stamp_memory_refs(
            ["ref-thread-1"], "user_provided", user_id="dudu", bank_id="default",
            origin=origin_from_metadata(md))

    t = threading.Thread(target=_worker)
    t.start()
    t.join(timeout=20)
    assert box.get("n") == 1

    conn = u.get_facts_conn()
    try:
        _r = conn.execute(
            "SELECT origin_agent, origin_session_id, origin_turn FROM memory_epistemic "
            "WHERE memory_ref=?", ("ref-thread-1",)).fetchone()
        row = tuple(_r) if _r is not None else None
    finally:
        conn.close()
    assert row == ("thr-agent", "thr-sess", 3), (
        f"跨线程打标三列应非空，实得 {row} —— contextvar 不跨线程，必须走 metadata")


def test_health_exposes_session_coverage_and_episode_probes():
    """🔴-1/🔴-2：两个探针必须在源码里真的存在并记降级。

    `episode_ok` 是任务书 DoD 点名要的，v21.2.0 漏做且实录没登记缺口 ——
    以「报告写了自己没做的事」论，比单纯遗漏更伤诚实性铁律。
    """
    import inspect
    from ducky.hot import health as h
    src = inspect.getsource(h)
    for key in ("epistemic_session_coverage", "epistemic_session_fresh_24h",
                "epistemic_session_fresh_7d", "episode_ok", "episode_step_count"):
        assert f'"{key}"' in src, f"/health 缺探针 {key}"
    # 光有字段不够 —— 长期为 0 必须真的记降级，否则又是一块绿着的空转
    assert "epistemic_session_coverage" in src and "record_degradation" in src
    i = src.find('"epistemic_session_coverage"')
    assert "record_degradation" in src[i:i + 3000], (
        "覆盖率探针只报数不记降级 —— 静默失效照样不会发红")


def test_session_coverage_window_is_wide_enough_to_have_range():
    """判据的射程必须盖得住本仓的真实流量分布。

    本仓典型日增只有个位数 sidecar 行。若用 24h 窗口配 ≥10 的阈值，
    探针会几乎永不触发 —— 那就是把「守卫坏死法」里的白护栏又造了一遍：
    看着有判据，实际一辈子不发红。窗口必须是 7 天。
    """
    import inspect
    from ducky.hot import health as h
    src = inspect.getsource(h)
    i = src.find('"epistemic_session_coverage"')
    seg = src[max(0, i - 3000):i + 3000]
    assert "24 * 7" in seg or "168" in seg, "覆盖率判据没有 7 天窗口"
    assert '"7d"' in seg, "未标注判据窗口，读数的人无从判断样本跨度"


def test_echo_suppress_degradation_is_not_silent():
    """🟢-1：回声抑制降级必须留 warning 并带上下文，不能只 debug。"""
    import inspect
    from ducky import scoring
    src = inspect.getsource(scoring._load_echo_refs)
    assert "logger.warning" in src, "降级只打 debug —— 用户看到回声却查不到线索"
    assert "session" in src and "user" in src, "降级日志没带 session/user 上下文"


def test_search_route_rejects_unauthenticated_instead_of_empty_success():
    """🟡-1 复核：未鉴权检索必须走鉴权拒绝，不能返回「空成功体」。

    实测生产已返回 401（审计中的「空 results」实为解析脚本 `.get("results", [])`
    的默认值）。这条把语义钉死：`/search` 不许自己吞掉鉴权失败再回 200。
    """
    import inspect
    from ducky.hot import search as hs
    src = inspect.getsource(hs.register_search_routes)
    i = src.find('@app.post("/search"')
    j = src.find('@app.post("/search_trace"')
    body = src[i:j if j > i else len(src)]
    # 路由体内不许出现「鉴权失败 → 返回 200 空结果」的形态
    assert "401" not in body or "raise" in body, (
        "/search 路由自行处理 401 时必须抛出，不许包装成成功响应")
    # 空 query 的判语路径仍在（这是业务语义，不是鉴权）
    assert "empty_query" in body, "空 query 判语不该被改掉"


# ══════ 自查轮：施工方复审翻出的「默认关掩盖着的空转」 ══════
#
# 用户审计只点了 2🔴3🟡2🟢；下面这批是整改期施工方自己回头复审 v21.2.0
# 全部六项时翻出来的 —— 都是「代码在场、开关一开就不对」的形态，
# 因为默认关 / 旁路 / 观测缺失而从未在冒烟里现形。


def test_dossier_grants_filter_uses_the_key_that_actually_exists():
    """M8：借阅过滤必须读 `revoked`（真实键），不是 `revoked_at`。

    读一个不存在的键恒得 None、`not None` 恒 True —— 于是一条都滤不掉，
    已撤销的借阅照样列在「当前生效」下，且无异常无日志。
    """
    import ast as _ast
    import inspect
    from ducky import dossier
    src = inspect.getsource(dossier.build_dossier_data)
    # 判据走 AST 而不是 grep —— 注释里正写着那个被删掉的错键名，
    # 字符串判据分不清代码和注释（本仓老教训，一轮能绊三次）。
    tree = _ast.parse(src.strip())
    bad = [n for n in _ast.walk(tree)
           if isinstance(n, _ast.Constant) and n.value == "revoked_at"]
    assert not bad, "代码里仍在读不存在的键 revoked_at"
    good = {n.value for n in _ast.walk(tree)
            if isinstance(n, _ast.Constant) and isinstance(n.value, str)}
    assert "revoked" in good, "没有读真实存在的 revoked 键"
    assert "expires_at" in good, "漏了过期判据 —— 过期 grant 会显示为生效"
    assert "_is_live" in {n.name for n in _ast.walk(tree)
                          if isinstance(n, _ast.FunctionDef)}


def test_dossier_grants_live_filter_semantics():
    """三态判据：已撤销滤掉、已过期滤掉、解析不出的过期时间按已过期（fail-closed）。"""
    import time
    from ducky.dossier import render_markdown
    future = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 86400))
    past = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - 86400))
    data = {"user_id": "dudu", "bank_id": "default", "sections": {"grants": {
        "granted": [{"grantee_user_id": "a", "actions": ["read", "export"],
                     "bank_id": "*", "expires_at": future, "revoked": False}],
        "received": []}}}
    md = render_markdown(data)
    # actions 是 list —— 必须拼成人读的逗号串，不能渲染出 ['read']
    assert "可 read, export" in md, md[md.find("## 八"):][:200]
    assert "['read'" not in md

    # 已撤销 / 已过期两态：build 侧的 _is_live 必须都滤掉。用真实调用验，
    # 不只看源码 —— 光断言「代码里有 expires_at」是白护栏。
    import ducky.pantheon as _pth
    from ducky.dossier import build_dossier_data
    _rows = [
        {"grant_id": "g1", "grantor_user_id": "dudu", "grantee_user_id": "live",
         "actions": ["read"], "bank_id": "*", "expires_at": future, "revoked": False},
        {"grant_id": "g2", "grantor_user_id": "dudu", "grantee_user_id": "gone",
         "actions": ["read"], "bank_id": "*", "expires_at": None, "revoked": True},
        {"grant_id": "g3", "grantor_user_id": "dudu", "grantee_user_id": "stale",
         "actions": ["read"], "bank_id": "*", "expires_at": past, "revoked": False},
    ]
    _orig = _pth.list_hall_grants
    _pth.list_hall_grants = lambda uid, direction="granted": (
        _rows if direction == "granted" else [])
    try:
        got = build_dossier_data("dudu", "default")["sections"]["grants"]["granted"]
    finally:
        _pth.list_hall_grants = _orig
    assert [g["grantee_user_id"] for g in got] == ["live"], (
        f"已撤销/已过期的借阅没被滤掉：{[g['grantee_user_id'] for g in got]}")
    # 这三条是 build 侧 _is_live 的判据，用纯函数直验
    from ducky import dossier as _d
    import inspect
    src = inspect.getsource(_d.build_dossier_data)
    assert "if not ts or ts <= _t.time()" in src, "过期时间解析失败未按已过期处理"


def test_mmr_protects_ignited_only_at_the_pre_boost_cut():
    """M4：打分出口那一刀看到的是 IGNITION_BOOST 之前的分，无权淘汰点火条。

    与 `_apply_score_floor` 完全同一条推理 —— 那里已为此显式豁免 ignited，
    MMR 这一刀当初漏了同一条。判据要能分辨两种语义：
      · 打分出口（protect_ignited=True）：点火条必留
      · funnel 那一刀（boost 已应用、分是终态）：点火条按真实分竞争
    """
    from ducky.scoring import mmr_select
    pool = [{"id": "hi", "memory": "高分非点火", "_hybrid_score": 0.90},
            {"id": "mid", "memory": "次高非点火", "_hybrid_score": 0.85},
            {"id": "ign", "memory": "低分点火", "_hybrid_score": 0.30, "_ignited": True}]
    # 打分出口：点火条必须活着进 funnel（否则 boost 永远没机会发生）
    kept = mmr_select([dict(x) for x in pool], 2, protect_ignited=True)
    assert any(x.get("_ignited") for x in kept), "打分出口误杀了点火条"
    # funnel 那一刀：不保护，低分点火条正常出局（不许压过高分条）
    kept2 = mmr_select([dict(x) for x in pool], 2)
    assert [x["id"] for x in kept2] == ["hi", "mid"]


def test_mmr_protection_holds_even_when_switch_is_off():
    """开关关掉也不许在这一刀误杀点火条 —— 豁免与多样性无关。"""
    from ducky.scoring import mmr_select
    os.environ["AIDUMEI_MMR_ENABLED"] = "0"
    try:
        pool = [{"id": "a", "_hybrid_score": 0.9}, {"id": "b", "_hybrid_score": 0.8},
                {"id": "ign", "_hybrid_score": 0.1, "_ignited": True}]
        assert any(x.get("_ignited") for x in mmr_select(pool, 2, protect_ignited=True))
    finally:
        os.environ.pop("AIDUMEI_MMR_ENABLED", None)


def test_rollup_actually_uses_limit_and_dedups_the_whole_group():
    """M7：`limit` 必须真被用上（回填），去重必须覆盖整组而非前 6 条。

    原实现里 `limit` 形参出现 0 次：折叠掉的名额不回填，一开开关返回条数
    就变少；而 `members[:6]` 之后只从这 6 条取 drop 集，第 7 条及以后既不
    进摘要也不被移除 —— 与 docstring 说的「与单条去重」正好相反。
    """
    import ast as _ast
    import inspect
    from ducky import recall_funnel as rf
    fn = _ast.parse(inspect.getsource(rf._apply_episode_rollup)).body[0]
    names = {n.id for n in _ast.walk(fn) if isinstance(n, _ast.Name)}
    assert "limit" in names, "limit 形参从未被使用 —— 折叠后不回填，结果会变少"
    src = inspect.getsource(rf._apply_episode_rollup)
    assert "all_items" in src and "ranked[1:]" in src, "去重仍只覆盖前 6 条"
    assert "spare" in src, "没有回填来源"


def test_rollup_degradation_is_not_silent():
    """rollup 降级要留 warning（它会改变返回条数，静默不可接受）。"""
    import inspect
    from ducky import recall_funnel as rf
    assert "logger.warning" in inspect.getsource(rf._apply_episode_rollup)


def test_workspace_fastpath_applies_echo_suppression_and_declares_bypass():
    """M2：workspace 热缓存正是回声最可能出现的地方，不能整条绕开。

    这条快路提前 return，从不经过打分出口。不在这里补一刀，M2 在最常命中、
    用户感知最强的那条路上等于不存在；同时必须如实声明它旁路了哪几项。
    """
    import inspect
    from ducky.hot import search as hs
    src = inspect.getsource(hs.register_search_routes)
    i = src.find("ws_lookup(")
    j = src.find('"_workspace_hit": True')
    assert i != -1 and j > i, "workspace 分支找不到 —— 守卫失去着力点"
    seg = src[i:j]
    assert "_load_echo_refs" in seg and "_drop_echo" in seg, "快路没做回声抑制"
    assert "_bypassed" in src[j:j + 1200], "旁路了打分出口却不声明，调用方无从得知"


def test_mcp_search_carries_session_id():
    """M2：MCP 通路必须能传 session_id，否则整条 MCP 上回声抑制不存在。"""
    import inspect
    # mcp 是可选依赖（`aidumei[mcp]`）。缺它是「没装可选轴」而不是缺陷 ——
    # 与本仓 ruff/nltk/regex 同一待遇：诚实跳过。跳过判据与既有 mcp_extra 轴
    # 同一口径（importorskip("mcp_server")），这样它直接落进已登记的那条轴，
    # 不新造一条没人知道的跳过轴。
    mcp_server = pytest.importorskip("mcp_server")
    assert "session_id" in inspect.signature(mcp_server.mem_search).parameters
    src = inspect.getsource(mcp_server.mem_search)
    assert '_payload["session_id"] = session_id' in src, "收了 session_id 却不发送"


def test_errsig_regex_is_a_single_source():
    """M6：写入侧与检索侧必须复用同一份正则，不许各留一份字面量拷贝。

    两份拷贝当下相等，但任一侧演化就静默错位：检索侧认出的签名写入侧没抽过
    ＝ 白加权，而且不报错。
    """
    from ducky.pattern_extract import _ERRSIG_RE
    from ducky.scoring import _ERRSIG_QUERY_RE
    assert _ERRSIG_QUERY_RE is _ERRSIG_RE, "两侧不是同一个对象 —— 又成两份拷贝"


def test_v212_effectiveness_lands_in_telemetry():
    """生效证据必须可观测 —— 本轮审计的核心结论就是「要有数据面旁证」。

    `_errsig_hit` / `_credit` 此前写进候选却全仓无人读取：加权到底命中过
    没有，线上无从判断。
    """
    import inspect
    from ducky import scoring
    src = inspect.getsource(scoring)
    assert "_report_v212_telemetry" in src
    body = inspect.getsource(scoring._report_v212_telemetry)
    for k in ("echo_suppressed", "errsig_hits", "credit_applied", "credit_weight"):
        assert k in body, f"遥测缺 {k}"
    # 必须真被调用，不能只定义（定义了不调用是最典型的白护栏）
    assert "_report_v212_telemetry(final" in src


def test_funnel_degraded_leg_keeps_scope_and_session():
    """降级腿不许丢 bank_id / session_id。

    丢了 bank_id，命名域下类型/出身/信用三张账本一条都查不到 —— 正是
    v20.2.4 F-15 修过的病在降级路径复发，而且照样返回结果、没有告警。
    """
    import inspect
    from ducky import recall_funnel as rf
    src = inspect.getsource(rf._fetch_candidate_pool)
    assert "session_id" in inspect.signature(rf._fetch_candidate_pool).parameters
    i = src.find("lazy_import_hybrid()")
    seg = src[i:i + 500]
    assert "bank_id=bank_id" in seg and "session_id=session_id" in seg


def test_credit_map_can_be_scoped():
    """轨迹信用查询要能按域收窄（作用域棘轮不留新缺口）。"""
    import inspect
    from ducky.evolve_mem import get_credit_map
    params = inspect.signature(get_credit_map).parameters
    assert "user_id" in params and "bank_id" in params
    src = inspect.getsource(get_credit_map)
    assert "JOIN evolve_episodes" in src, "没 join 就拿不到域列"


def test_echo_suppression_documents_its_range_honestly():
    """射程边界必须写在代码里 —— facts 类候选不被回声抑制是设计取向，
    不许靠沉默让人以为覆盖了。"""
    import inspect
    from ducky.scoring import _load_echo_refs
    doc = inspect.getdoc(_load_echo_refs) or ""
    assert "射程边界" in doc and "fact:" in doc


def test_gate_telemetry_actually_reaches_the_caller():
    """遥测必须真的下发 —— 否则「有数据面旁证」只是自我安慰。

    整改期自查发现：`last_gate_telemetry()` **全仓零消费**。闸门拦了多少、
    M2/M4/M6/M1 生没生效，全写进了一条死路 —— 连既有的 evidence_filtered
    也一样。把生效证据挂进死路，正是本轮批评的那种「绿着的空转」。
    """
    import inspect
    from ducky.hot import search as hs
    src = inspect.getsource(hs.register_search_routes)
    assert "last_gate_telemetry" in src, "遥测无人读取 —— 写了等于没写"
    assert "reset_gate_telemetry" in src, (
        "没有每请求重置 —— 线程复用时上一请求的残留会被读成本次的")
    assert '"_gate": gate_telem' in src, "遥测没有进响应体"


# ══════ 范围外缺口收口：跨殿借阅在 /search 上真生效 ══════


def test_search_routes_enforce_cross_hall_grant():
    """`/search` 与 `/search_trace` 必须校验借阅。

    v21.1 把借阅织进了 recall_chain / session_search / dossier，但 `/search`
    —— 最主要的那条 core 读路径 —— 收了 `caller_user_id` 却从不校验：声明
    被 Pydantic 安静收下然后丢弃，「声明了」与「没声明」行为逐字节相同。
    """
    import inspect
    from ducky.hot import search as hs
    src = inspect.getsource(hs.register_search_routes)
    i = src.find('@app.post("/search"')
    j = src.find('@app.post("/search_trace"')
    k = src.find('@app.get("/gate"')
    assert i != -1 and j > i, "路由段找不到 —— 守卫失去着力点"
    for name, seg in (("/search", src[i:j]), ("/search_trace", src[j:k if k > j else len(src)])):
        assert "authorize_cross_hall" in seg, f"{name} 未校验跨殿借阅"
        assert "status_code=403" in seg, f"{name} 的授权拒绝没转 403"


def test_hall_denial_is_403_not_swallowed_into_error_body():
    """授权拒绝必须以 403 出去，不能被通用 except 吞成 {"status":"error"}。

    P1-4 的老教训：`raise HTTPException` 写在 try 里，下面一个裸
    `except Exception` 就能把它吞掉再包成成功体——「无权限」与「服务端
    故障」混成一件事，调用方的重试逻辑会一直重试一个永远不会成功的请求。
    """
    import inspect
    import re
    from ducky.hot import search as hs
    from ducky import routes_v8
    for mod_fn in (hs.register_search_routes, routes_v8.register_v8_routes):
        src = inspect.getsource(mod_fn)
        # 判据按**路由体**切分，不用固定行窗口 —— /search 的函数体比任何
        # 固定窗口都长，窗口式判据会把「保护在更远处」误判成「没保护」
        # （假红灯与假绿灯一样害人）。
        starts = [m.start() for m in re.finditer(r"^    @app\.(post|get)\(", src, re.M)]
        for a, b in zip(starts, starts[1:] + [len(src)]):
            body = src[a:b]
            if "status_code=403" not in body:
                continue
            route = body.splitlines()[0].strip()
            lines = body.splitlines()
            i403_ln = next(n for n, ln in enumerate(lines) if "status_code=403" in ln)
            # 只看**路由级**（缩进 8）的 handler —— 内层 try 的 except（缩进 12）
            # 接的是它自己那几行，不会接到 403。按缩进判层级，否则第一个内层
            # except Exception 就会让判据误判成「没保护」。
            level = [ln.strip() for n, ln in enumerate(lines)
                     if n > i403_ln and re.match(r"^ {8}except ", ln)]
            assert level, f"{mod_fn.__name__} 的 {route}：403 之后没有路由级 except"
            # 只有**宽捕获**（except Exception / 裸 except）才会吞 HTTPException；
            # except ImportError 这类窄捕获接不到它，路过无害。判据只要求：
            # 在第一个宽捕获**之前**必须已经放行过 HTTPException。
            broad = next((i for i, ln in enumerate(level)
                          if ln.startswith("except Exception") or ln == "except:"), None)
            passthru = next((i for i, ln in enumerate(level)
                             if ln.startswith("except HTTPException")), None)
            assert broad is None or (passthru is not None and passthru < broad), (
                f"{mod_fn.__name__} 的 {route}：403 之后的宽捕获前没有"
                "「except HTTPException: raise」—— 403 会被吞成成功体")


def test_empty_caller_still_passes_zero_breakage():
    """空 caller / caller==user_id 一律放行 —— 存量调用方零破坏。

    这是加校验能安全上线的前提：宿主、MCP、控制台都不传 caller。
    """
    from ducky.pantheon import authorize_cross_hall
    assert authorize_cross_hall("dudu", "") is True
    assert authorize_cross_hall("dudu", "   ") is True
    assert authorize_cross_hall("dudu", "dudu") is True


# ══════════ 写入活性：观测盲区收口 ══════════
#
# 2026-09-17 的教训：本仓所有探针都在回答「库里已有的记忆好不好」，
# 没有一个在回答「今天该进来的进来了吗」。于是一个只挂了读钩子、
# 没挂写钩子的部署，可以在全绿指标下失忆一个月。
# 下面这批把「读写比」这个判据焊死。


def test_health_has_ingest_liveness_probe():
    """/health 必须能回答「在读却不在写」。"""
    import inspect
    from ducky.hot import health as h
    src = inspect.getsource(h)
    for key in ("ingest_reads_24h", "ingest_writes_24h", "ingest_liveness_ok"):
        assert f'"{key}"' in src, f"/health 缺写入活性字段 {key}"
    i = src.find('"ingest_liveness_ok"')
    assert "record_degradation" in src[i:i + 2500], (
        "只报数不记降级 —— 那还是没人会发现失忆")


def test_ingest_threshold_is_configurable_and_fail_closed():
    """阈值可配、非法值回默认 —— 配置写错不许把探针关掉。"""
    import importlib
    from ducky.hot import health as h
    assert h._INGEST_MIN_READS >= 1
    old = os.environ.get("AIDUMEI_INGEST_MIN_READS")
    try:
        os.environ["AIDUMEI_INGEST_MIN_READS"] = "不是数字"
        importlib.reload(h)
        assert h._INGEST_MIN_READS == 5, "非法值没有 fail-closed 回默认"
        os.environ["AIDUMEI_INGEST_MIN_READS"] = "20"
        importlib.reload(h)
        assert h._INGEST_MIN_READS == 20
    finally:
        if old is None:
            os.environ.pop("AIDUMEI_INGEST_MIN_READS", None)
        else:
            os.environ["AIDUMEI_INGEST_MIN_READS"] = old
        importlib.reload(h)


def test_wiring_checker_verdicts():
    """自查脚本的三态判决：只读不写 / 正常 / 样本不足，必须分得开。"""
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "_wiring", root / "scripts" / "check_ingest_wiring.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # ① 只读不写 —— 必须 fail（这是事故的形状）
    code, lines, _ = mod.diagnose({"probes": {
        "ingest_reads_24h": 24, "ingest_writes_24h": 0,
        "ingest_turn_writes_24h": 0, "ingest_liveness_ok": False}})
    assert code == 1 and any("没在写" in ln for ln in lines)

    # ①b 真实事故的形状：后台通路一直在写（cron 整合器 / MEMORY.md 同步），
    #     对话一条没写。第一版探针数写入总数，在这个形状上恒绿 —— 必须红，
    #     而且结论里要说清「那是后台在写」，否则人会拿写入总数反驳这条告警。
    code, lines, _ = mod.diagnose({"probes": {
        "ingest_reads_24h": 24, "ingest_writes_24h": 18,
        "ingest_turn_writes_24h": 0, "ingest_liveness_ok": False}})
    assert code == 1, "后台在写、对话没写 —— 这正是那次事故，必须红"
    joined = " ".join(lines)
    assert "来自对话 0 条" in joined, "没把对话写入单独报出来，人只会看到 18"
    assert "后台" in joined or "同步引擎" in joined, "没解释那 18 条是谁写的"

    # ② 读写都有 —— 通过
    code, lines, _ = mod.diagnose({"probes": {
        "ingest_reads_24h": 24, "ingest_writes_24h": 9,
        "ingest_turn_writes_24h": 9, "ingest_liveness_ok": True}})
    assert code == 0 and any("都在工作" in ln for ln in lines)

    # ③ 样本不足 —— 不许假红灯挡住刚部署的人
    code, _, _ = mod.diagnose({"probes": {
        "ingest_reads_24h": 1, "ingest_writes_24h": 0,
        "ingest_turn_writes_24h": 0, "ingest_liveness_ok": True}})
    assert code == 0

    # ④ 写了但没带 session —— 通过但要提醒（两功能在空转）
    code, lines, _ = mod.diagnose({"probes": {
        "ingest_reads_24h": 24, "ingest_writes_24h": 9,
        "ingest_turn_writes_24h": 9, "ingest_liveness_ok": True,
        "epistemic_session_coverage": 0}})
    assert code == 0 and any("session" in ln for ln in lines)

    # ⑤ 旧服务端无探针 vs 未鉴权被脱敏 —— 两种「读不到」必须给不同指引
    _, l_old, _ = mod.diagnose({"version": "20.0", "probes": {}})
    _, l_red, _ = mod.diagnose({"probes": {"_redacted": "x"}})
    assert any("还没有写入活性探针" in ln for ln in l_old)
    assert any("--token" in ln for ln in l_red)


def test_integration_check_verifies_host_wiring_not_just_api():
    """集成检查必须验「宿主在不在调」，不能只验「API 能不能用」。

    这是事故的根本成因：原脚本自己调 /add、自己调 /search，当然全绿 ——
    它测的是被集成方，不是集成本身。
    """
    src = (__import__("pathlib").Path(__file__).resolve().parent.parent
           / "scripts" / "agent_integration_check.py").read_text(encoding="utf-8")
    assert 'check("host-wiring"' in src, "集成检查没有宿主接线判据"
    assert "ingest_reads_24h" in src and "ingest_writes_24h" in src, "没用真实流量判据"
    assert "_INGEST_MIN_READS" in src, "阈值未与 /health 探针同源"


def test_docs_tell_agents_where_to_hook_the_write_wire():
    """文档必须明确告诉宿主 Agent：写钩子挂在哪、漏了会怎样。"""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    doc = (root / "docs" / "AGENT_INTEGRATION.md").read_text(encoding="utf-8")
    for token in ("post_llm_call", "Stop", "_origin_session_id", "check_ingest_wiring"):
        assert token in doc, f"AGENT_INTEGRATION.md 没讲 {token}"
    canon = (root / "prompts" / "install.txt").read_text(encoding="utf-8")
    assert "check_ingest_wiring" in canon, "一键部署正典没让 Agent 验证写线"
    assert "post_llm_call" in canon, "正典没说写钩子挂哪"
    # README 双语都要有，否则只看 README 的人仍会踩
    for name in ("README.md", "README_EN.md"):
        assert "check_ingest_wiring" in (root / name).read_text(encoding="utf-8"), \
            f"{name} 未提示接线自查"


def test_install_canon_line_count_matches_readme_claim():
    """正典行数与 README 宣称必须一致（宣称即承诺）。"""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    lines = [ln for ln in (root / "prompts" / "install.txt")
             .read_text(encoding="utf-8").strip().splitlines() if ln.strip()]
    zh = (root / "README.md").read_text(encoding="utf-8")
    m = re.search(r"（(\d+) 行正典）", zh)
    assert m, "README.md 缺少「N 行正典」宣称"
    assert int(m.group(1)) == len(lines), (
        f"README 宣称 {m.group(1)} 行，install.txt 实为 {len(lines)} 行")
    en = (root / "README_EN.md").read_text(encoding="utf-8")
    m2 = re.search(r"the (\d+)-line canon", en)
    assert m2 and int(m2.group(1)) == len(lines), "README_EN 行数宣称不一致"


# ══════════════════════════════════════════════════════════════════
# v21.2.0：仓库自己交付的接入物料必须两条线齐全
#
# 事故的真正源头不在部署方，在这里：本仓此前只提供 aidumem-inject.sh（读线），
# config.yaml.snippet 与 INTEGRATION_GUIDE.md 也只教人注册 pre_llm_call。
# 照着我们文档装出来的部署，每轮都在读、从来没写过。
# 下面几条守卫盯住「别再只发一半电路」。
# ══════════════════════════════════════════════════════════════════

def test_repo_ships_a_write_wire_hook_not_just_a_read_one(tmp_path):
    """写线脚本必须真实存在、可执行、且带能吵起来的自检路径。"""
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    hook = root / "integrations" / "aidumem-ingest.sh"
    assert hook.is_file(), "仓库没有写线钩子——文档说的挂点指向空气"
    assert os.access(hook, os.X_OK), "写线钩子没有可执行位，拷过去就用不了"
    src = hook.read_text(encoding="utf-8")
    # 语法必须真过，不是「文件在就算数」
    subprocess.run(["bash", "-n", str(hook)], check=True, capture_output=True, timeout=10)
    assert "post_llm_call" in src, "写线钩子没声明自己挂在哪个事件"
    assert "--selftest" in src, "写线的静默失败最毒，必须有一条能吵起来的路径"
    # 必带溯源三件套：不传不会报错，只会让回声抑制与轨迹信用静默失效
    for key in ("_origin_session_id", "_origin_turn", "_origin_agent"):
        assert key in src, f"写线钩子没传 {key}"
    # 与读线共用同一条凭据/身份链：写进 A 租户、读的是 B 租户是最难查的故障
    for token in ("AIDUMEM_API_TOKEN", "AIDUMEM_USER_ID", "_lookup_env_key"):
        assert token in src, f"写线钩子没走与读线同源的 {token}"
    # 兼容 extra 下沉的真实 payload 形状（顶层取不到就是恒静默不写）
    assert "extra" in src and "assistant_response" in src, "没按真实 payload 形状解析"


def test_shipped_config_snippets_register_both_wires():
    """我们发给用户照抄的配置，必须两个挂点都**注册**在 yaml 里。

    判据走真解析而不是字符串包含：`post_llm_call` 这个词在旁边的说明表格里
    也会出现，substring 分不清「配置里注册了」和「正文里提过」——首次写这条
    守卫时就是这么被自己的负向对照抓住的（删掉注册行，守卫照样绿）。
    """
    import re
    from pathlib import Path
    import yaml
    root = Path(__file__).resolve().parent.parent
    for rel in ("integrations/config.yaml.snippet", "integrations/INTEGRATION_GUIDE.md"):
        text = (root / rel).read_text(encoding="utf-8")
        hooks: dict = {}
        for block in re.findall(r"```ya?ml\n(.*?)```", text, re.S):
            try:
                doc = yaml.safe_load(block)
            except yaml.YAMLError:
                continue
            if isinstance(doc, dict) and isinstance(doc.get("hooks"), dict):
                hooks.update(doc["hooks"])
        assert hooks, f"{rel} 里没有一段可解析的 hooks 配置"
        for event, script in (("pre_llm_call", "aidumem-inject.sh"),
                              ("post_llm_call", "aidumem-ingest.sh"),
                              ("on_session_end", "aidumem-distill.sh")):
            entries = hooks.get(event)
            assert entries, (
                f"{rel} 的 yaml 没注册 {event}——"
                f"照抄的人会装出一条只有半边的电路")
            cmds = " ".join(str((e or {}).get("command", "")) for e in entries)
            assert script in cmds, f"{rel} 的 {event} 没指向 {script}，指向了 {cmds!r}"
        assert "check_ingest_wiring" in text, f"{rel} 没给装完之后的验收办法"


def test_five_minute_watchdog_reads_the_degradation_list():
    """每 5 分钟的 health_check 必须读 health_status/degraded，不能只看 HTTP 200。

    此前它只确认「/health 这个接口还活着」，于是服务端算出来的所有降级
    对定时哨兵一律不可见 —— 探针再准也没有任何自动化通路会因此变红。
    """
    import ast
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "scripts" / "health_check.py") \
        .read_text(encoding="utf-8")
    tree = ast.parse(src)  # 判据走 AST，别让注释里的字眼冒充代码
    literals = {n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert "health_status" in literals, "哨兵不读 health_status"
    assert "degraded" in literals, "哨兵不读 degraded 清单"
    assert "warming_up" in literals, "预热态未区分，重启后会假红"
    # 降级必须真的影响判决，而不只是打印出来看看
    assert "_api_ok = False" in src, "读了降级却不改判决 = 白读"


def test_report_cron_threshold_tracks_the_task_list_not_a_literal():
    """装齐判据必须跟着清单走。写死数字的话，清单一加任务就变成假绿灯。"""
    import json
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    listed = json.loads(subprocess.run(
        ["bash", str(root / "scripts" / "update_crontab.sh"), "--list"],
        check=True, capture_output=True, text=True, timeout=15).stdout)
    n = len(listed["tasks"])
    src = (root / "scripts" / "report.py").read_text(encoding="utf-8")
    assert "effective < _required" in src, "装齐门槛仍是字面量，会随清单漂移"
    assert f"< {n}" not in src.replace("< _required", ""), "门槛里还留着写死的任务数"
    # 写线哨兵必须在清单里，且指向真实脚本
    names = {t["name"] for t in listed["tasks"]}
    assert "ingest_wiring" in names, "定时任务清单里没有写线哨兵"
    sentinel = next(t for t in listed["tasks"] if t["name"] == "ingest_wiring")
    assert (root / "scripts" / "check_ingest_wiring.py").is_file(), "哨兵指向不存在的脚本"
    assert "post_llm_call" in sentinel["failure_action"], "哨兵红了却没说该去挂什么"


def test_ingest_degradation_gets_a_named_action_not_a_generic_one():
    """「有组件降级」这句泛话对写线断裂是误导——问题不在召回质量。"""
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "_rpt_guard", root / "scripts" / "report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    actions = mod._safe_next_actions(
        {"health_status": "degraded", "degraded": ["ingest_liveness"], "warming_up": []},
        {"crontab_task_count": 9, "crontab_installed_count": 9,
         "latest_backup": {"verified": True}},
    )
    joined = " ".join(actions)
    assert "check_ingest_wiring" in joined, "没告诉运维用什么命令确认"
    assert "post_llm_call" in joined, "没点名该挂哪个钩子"
    # 负向对照：没有这项降级时不许乱喊，否则告警疲劳
    quiet = mod._safe_next_actions(
        {"health_status": "ok", "degraded": [], "warming_up": []},
        {"crontab_task_count": 9, "crontab_installed_count": 9,
         "latest_backup": {"verified": True}},
    )
    assert "check_ingest_wiring" not in " ".join(quiet), "无故障时也喊 = 假红灯"


def test_write_wire_hook_actually_posts_a_correct_add_request(tmp_path):
    """写线钩子必须真发出一条形状正确的 /add ——「文件在」不等于「能用」。

    用真监听器接住请求，验的是整条链（解析真实 payload 形状 → 拼 body →
    带凭据 POST），不是脚本里有没有某个字眼。
    """
    import json
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from pathlib import Path

    got: dict = {}

    class _H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            got["path"] = self.path
            got["body"] = json.loads(self.rfile.read(n).decode("utf-8"))
            got["auth"] = self.headers.get("Authorization")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"results":[]}')

        def log_message(self, *a):  # 静音，别污染用例输出
            return

    srv = HTTPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.handle_request, daemon=True)
    t.start()

    payload = json.dumps({
        "hook_event_name": "post_llm_call",
        "session_id": "sess-abc",
        "cwd": "/tmp",
        "extra": {
            "user_message": "一个足够长的用户问题，用于越过最小字数门槛",
            "assistant_response": "助手的回答",
            "turn_id": 7,
            "platform": "hermes",
            "conversation_history": [],
        },
    }, ensure_ascii=False)

    hook = Path(__file__).resolve().parent.parent / "integrations" / "aidumem-ingest.sh"
    proc = subprocess.run(
        ["bash", str(hook)], input=payload, text=True, capture_output=True, timeout=30,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/nonexistent",
             "AIDUMEM_DATA_DIR": str(tmp_path / "_iso_data"),
             "AIDUMEM_LOG_DIR": str(tmp_path / "_iso_logs"),
             "AIDUMEM_URL": f"http://127.0.0.1:{port}",
             "AIDUMEM_USER_ID": "guard-user",
             "AIDUMEM_API_TOKEN": "tok-guard",
             "AIDUMEM_HOOK_QUIET": "1"},
    )
    assert proc.stdout.strip() == "{}", f"钩子必须输出 {{}} 不改写任何东西，实得 {proc.stdout!r}"
    t.join(timeout=10)

    assert got.get("path") == "/add", f"打错了端点：{got.get('path')!r}"
    body = got.get("body") or {}
    roles = [m.get("role") for m in body.get("messages") or []]
    assert roles == ["user", "assistant"], f"消息体形状不对：{roles}"
    assert body.get("user_id") == "guard-user", "身份没透传"
    md = body.get("metadata") or {}
    # 溯源三件套是回声抑制与轨迹信用的输入；不传不报错，只会静默失效
    assert md.get("_origin_session_id") == "sess-abc", "session 没传，回声抑制会静默失效"
    assert md.get("_origin_turn") == 7, "turn 没传，轨迹信用归不了集"
    assert md.get("_origin_agent"), "agent 没传"
    assert got.get("auth") == "Bearer tok-guard", "凭据没带上，门禁开着就是 401 后静默"


def test_write_wire_hook_stays_silent_when_service_is_down(tmp_path):
    """服务打不通时必须安静退出 0 —— 记忆写不进去，绝不能连带拖垮对话。"""
    import json
    import subprocess
    from pathlib import Path
    hook = Path(__file__).resolve().parent.parent / "integrations" / "aidumem-ingest.sh"
    payload = json.dumps({
        "hook_event_name": "post_llm_call",
        "extra": {"user_message": "一个足够长的用户问题用于越过门槛",
                  "assistant_response": "答", "turn_id": 1},
    }, ensure_ascii=False)
    proc = subprocess.run(
        ["bash", str(hook)], input=payload, text=True, capture_output=True, timeout=30,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/nonexistent",
             "AIDUMEM_DATA_DIR": str(tmp_path / "_iso_data"),
             "AIDUMEM_LOG_DIR": str(tmp_path / "_iso_logs"),
             "AIDUMEM_URL": "http://127.0.0.1:1",       # 必然打不通
             "AIDUMEM_INGEST_TIMEOUT": "2.0",
             "AIDUMEM_HOOK_QUIET": ""},                 # 故意开着诊断
    )
    assert proc.returncode == 0, "写入失败不许非 0 退出，会拖累宿主"
    assert proc.stdout.strip() == "{}", "必须返回空对象"
    # 安静≠失声：失败必须在 stderr 留痕，否则又是一次没人知道的静默
    assert "aidumem-ingest" in proc.stderr, "失败时 stderr 一声不吭 = 下一次事故"


def test_claude_code_stop_hook_writes_the_last_turn(tmp_path):
    """Claude Code 的写线钩子必须从真转录里取出最后一轮并发出正确的 /add。

    同目录的 claude-code-hook.py 是 pre_compact 存代码的手动工具，不是逐轮
    写入；文档一度指向它，等于把用户指向空气。这条守卫盯住「指哪儿就得有
    什么」，判据是服务端真正收到的请求。
    """
    import json
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from pathlib import Path

    got: dict = {}

    class _H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            got["path"] = self.path
            got["body"] = json.loads(self.rfile.read(n).decode("utf-8"))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"results":[]}')

        def log_message(self, *a):
            return

    srv = HTTPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.handle_request, daemon=True)
    t.start()

    # 真转录：工具循环会在 user 与 assistant 之间插记录，所以不能按行号倒数
    rows = [
        {"type": "user", "message": {"role": "user", "content": "上一轮的老问题"}},
        {"type": "assistant", "message": {"role": "assistant", "content": "上一轮的老回答"}},
        {"type": "user", "message": {"role": "user", "content": "这一轮真正的问题够长了"}},
        {"type": "assistant", "message": {"role": "assistant",
                                          "content": [{"type": "tool_use", "name": "x"}]}},
        {"type": "user", "message": {"role": "user",
                                     "content": [{"type": "tool_result", "content": "..."}]}},
        {"type": "assistant", "message": {"role": "assistant",
                                          "content": [{"type": "text", "text": "这一轮的回答"}]}},
    ]
    transcript = str(tmp_path / "transcript.jsonl")
    with open(transcript, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    hook = (Path(__file__).resolve().parent.parent / "integrations" / "cursor-hook"
            / "claude-code-stop-hook.py")
    payload = json.dumps({"hook_event_name": "Stop", "session_id": "cc-sess-1",
                          "transcript_path": transcript, "turn": 3,
                          "stop_hook_active": False}, ensure_ascii=False)
    proc = subprocess.run(
        [sys.executable, str(hook)], input=payload, text=True,
        capture_output=True, timeout=30,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent",
             "AIDUMEM_DATA_DIR": str(tmp_path / "_iso_data"),
             "AIDUMEM_LOG_DIR": str(tmp_path / "_iso_logs"),
             "AIDUMEM_URL": f"http://127.0.0.1:{port}",
             "AIDUMEM_USER_ID": "cc-guard", "AIDUMEM_HOOK_QUIET": "1"},
    )
    assert proc.returncode == 0, f"Stop 钩子非 0 退出会打断宿主：{proc.stderr[:300]}"
    t.join(timeout=10)

    assert got.get("path") == "/add", f"打错端点：{got.get('path')!r}"
    body = got.get("body") or {}
    msgs = {m["role"]: m["content"] for m in body.get("messages") or []}
    assert msgs.get("user") == "这一轮真正的问题够长了", \
        f"没取到本轮 user（按行号倒数就会取错）：{msgs.get('user')!r}"
    assert msgs.get("assistant") == "这一轮的回答", \
        f"没从 content blocks 里取出文本：{msgs.get('assistant')!r}"
    md = body.get("metadata") or {}
    assert md.get("_origin_session_id") == "cc-sess-1", "session 没传"
    # turn 从转录推导（payload 的 turn 字段 Claude Code 实际不给）。
    # 这份转录里 role=user 的有 3 条，但其中一条是工具结果 —— 工具循环插入的
    # user 消息不算一轮对话，所以正确答案是 2。数成 3 就说明把工具回执当人话了。
    assert md.get("_origin_turn") == 2, (
        f"轮次应为 2（工具结果那条 user 不算一轮），实得 {md.get('_origin_turn')!r}")



def test_claude_code_stop_hook_respects_recursion_guard(tmp_path):
    """stop_hook_active 时不许再写一遍，否则钩子自触发会把同一轮写进去两次。"""
    import json
    import subprocess
    from pathlib import Path
    hook = (Path(__file__).resolve().parent.parent / "integrations" / "cursor-hook"
            / "claude-code-stop-hook.py")
    proc = subprocess.run(
        [sys.executable, str(hook)],
        input=json.dumps({"hook_event_name": "Stop", "session_id": "s",
                          "transcript_path": "/nonexistent.jsonl",
                          "stop_hook_active": True}),
        text=True, capture_output=True, timeout=30,
        # 故意**不**设 AIDUMEM_HOOK_QUIET：诊断必须开着，否则守卫被绕过时
        # stderr 照样是空的，这条断言就永远绿——首版就是这么白的。
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent",
             "AIDUMEM_DATA_DIR": str(tmp_path / "_iso_data"),
             "AIDUMEM_LOG_DIR": str(tmp_path / "_iso_logs"),
             "AIDUMEM_URL": "http://127.0.0.1:1"},
    )
    assert proc.returncode == 0
    # 递归守卫应在读转录之前就返回；转录根本不存在也不该有任何抱怨
    assert not proc.stderr.strip(), f"递归守卫没生效，仍走了写入路径：{proc.stderr[:200]}"


def test_write_wire_hooks_use_async_mode_so_the_host_never_waits():
    """写线必须走 async_mode。

    生产实测同步 /add 的 p50 约 4 秒（要跑完整抽取管线），长尾未知；而宿主
    给 hook 的超时通常是个位数秒。超时被杀的钩子 = 静默不写 —— 正好是这批
    改动要根治的那个失败形态。异步下服务端先收下、后台落库，溯源三件套在
    /add 入口就已归一进 metadata，不受影响。
    """
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    for rel in ("integrations/aidumem-ingest.sh",
                "integrations/cursor-hook/claude-code-stop-hook.py"):
        src = (root / rel).read_text(encoding="utf-8")
        assert "async_mode" in src, f"{rel} 走同步写入，会被宿主超时杀掉"
    # 仓库里发的插件那条路早就是异步的，口径必须一致
    plugin = (root / "integrations" / "hermes-plugin" / "aidumem" / "__init__.py") \
        .read_text(encoding="utf-8")
    assert "async_mode" in plugin, "插件路径与 shell hook 路径写入口径不一致"


def test_ingest_probe_judges_conversation_writes_not_background_ones():
    """写入活性判据必须落在「带会话来源的写入」上，不是写入总数。

    本探针第一版数的是全部新增（facts + sidecar）。而实测那台出事的机器上，
    cron 整合器与 MEMORY.md 同步引擎每天写 6~18 条，带 session 的**恒为 0**
    —— 探针在它本该抓住的那次事故上永远是绿的。射程没盖住缺陷分布，
    白护栏一条。判据改数 origin_session_id 非空的那部分。
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "ducky" / "hot" / "health.py") \
        .read_text(encoding="utf-8")
    assert "ingest_turn_writes_24h" in src, "探针没暴露对话写入这个数"
    assert "origin_session_id" in src and "COALESCE" in src.upper(), \
        "没有按 origin_session_id 过滤，数的还是写入总数"
    assert "_ing_turn_writes == 0" in src, "判据没落在对话写入上"
    assert "_ing_writes == 0" not in src, "判据里还留着「写入总数为零」的旧口径"
    # 下游两个消费者必须同源，否则三处判决会各说各话
    for rel in ("scripts/check_ingest_wiring.py", "scripts/agent_integration_check.py"):
        text = (Path(__file__).resolve().parent.parent / rel).read_text(encoding="utf-8")
        assert "ingest_turn_writes_24h" in text, f"{rel} 判据与探针不同源"


# ══════════════════════════════════════════════════════════════════
# v21.2.0 用户审计整改（2026-09-17，外部用户审计 5 条 + 自查 2 条）
# ══════════════════════════════════════════════════════════════════

def test_search_logs_retrieval_on_the_main_path_not_only_the_funnel():
    """检索埋点必须落在主 /search 上。

    此前 log_search_quality 只在 recall_funnel 里调用，而主 /search 走
    mem.search + 融合，根本不经过 funnel —— evolve_queries 里于是只有
    e2e_smoke 每小时一次的巡检记录，真实对话的检索一次都没被记过。
    写入活性探针拿这张表当「有人在用」的证据，读到的却全是自己的心跳。
    （「挂钩必须落真实缝位」的第三次复发。）
    """
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    src = (root / "ducky" / "hot" / "search.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # 判据走 AST：注释里提到函数名不算数
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    imported = {a.asname or a.name for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom) for a in n.names}
    # 引入时可能带别名（as _log_sq），判据两边都认
    assert {"log_search_quality", "_log_sq"} & imported, "主 /search 没引入检索埋点"
    assert called & {"_log_sq", "log_search_quality"}, "引入了却没调用 = 空转"
    assert "origin_session_id=" in src, "埋点没带 session，无法区分对话与巡检"


def test_read_wire_passes_session_so_echo_suppression_can_work():
    """读线必须透传 session_id。

    服务端 _req_session_id 读不到 session 就返回空串 = 不过滤，于是 M2 回声
    抑制在 shell hook 这条接入路上一直空转。同一个 session 还是写入活性探针
    区分「真有人在对话」与「定时器在自检」的唯一依据。
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "integrations"
           / "aidumem-inject.sh").read_text(encoding="utf-8")
    assert "_INJECT_SESSION_PIPE" in src, "读线没从 payload 取 session"
    # 判据必须落在「正式检索那一处真的用了它」，不能只看有没有 session_id
    # 这个词 —— selftest 里也写着一个固定值，会冒充正式路径把守卫骗过去
    # （首版就是这么被自己的负向对照抓住的）。
    assert "os.environ.get('_INJECT_SESSION_PIPE'" in src, \
        "取到了 session 却没送进 /search body —— 取值与使用之间断了"
    # 取值本身也要在：只有使用没有取值同样是空转
    assert "export _INJECT_SESSION_PIPE=" in src, "没从 payload 解析出 session"


def test_ingest_probe_uses_conversation_reads_not_heartbeat_reads():
    """写入活性判据的「读」必须是对话检索，不是巡检心跳。

    实测生产近 24h 的 evolve_queries 每一条都是 `aidumei-smoke-*`（e2e_smoke
    每小时一次）。拿检索总数当「有人在用」，只要一天没聊天探针就必然误报
    写线断了 —— 这是写入侧「数了后台通路」的同一个错，犯在读取侧。
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "ducky" / "hot"
           / "health.py").read_text(encoding="utf-8")
    assert "ingest_conv_reads_24h" in src, "探针没暴露对话检索这个数"
    assert "_ing_conv_reads >= _INGEST_MIN_READS" in src, "判据没落在对话检索上"
    assert "_ing_reads >= _INGEST_MIN_READS and _ing_turn_writes == 0" not in src \
        .replace("_ing_conv_reads", "X"), "判据里还留着「检索总数」的旧口径"
    # 第三态：读线是旧版本时既不能判红也不能判绿
    assert "_ing_blind" in src, "缺第三态：读线不传 session 时会在两个方向撒谎"
    assert "ingest_liveness_note" in src, "第三态没有给出可执行的说明"


def test_wiring_checker_refuses_silent_pass_when_asked():
    """--require-judgment 下，「样本不足」与「读线旧版本」必须非 0 退出。

    默认行为不挡刚部署的新系统；但同一个脚本里「未鉴权」「无探针」都返回 2，
    唯独「样本不足」返回 0，口径不一致，而且一个刚装好就断线的系统会从 CI
    门禁一路绿过去（用户审计 🟡-3）。
    """
    import importlib.util
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "_wiring2", root / "scripts" / "check_ingest_wiring.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    thin = {"probes": {"ingest_reads_24h": 2, "ingest_conv_reads_24h": 2,
                       "ingest_writes_24h": 0, "ingest_turn_writes_24h": 0,
                       "ingest_liveness_ok": True}}
    assert mod.diagnose(thin)[0] == 0, "默认不许挡住刚部署的人"
    assert mod.diagnose(thin, require_judgment=True)[0] == 2, \
        "要明确结论时，样本不足必须报「无法判断」而不是沉默通过"

    # 读线旧版本：有检索但一次都不带 session
    blind = {"probes": {"ingest_reads_24h": 24, "ingest_conv_reads_24h": 0,
                        "ingest_writes_24h": 12, "ingest_turn_writes_24h": 0,
                        "ingest_liveness_ok": True}}
    code, lines, _ = mod.diagnose(blind)
    joined = " ".join(lines)
    assert "无法判断" in joined, "没说出「判不了」，会被当成一切正常"
    assert "回声抑制" in joined, "没提 M2 同时也在空转"
    assert mod.diagnose(blind, require_judgment=True)[0] == 1, \
        "要明确结论时，读线旧版本必须非 0"


def test_write_wire_leaves_a_trace_when_it_skips_a_turn(tmp_path):
    """跳过某一轮必须留痕 —— 漏写和正常跳过在日志里要分得开。"""
    import json
    import subprocess
    from pathlib import Path
    hook = Path(__file__).resolve().parent.parent / "integrations" / "aidumem-ingest.sh"
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/nonexistent",
           "AIDUMEM_DATA_DIR": str(tmp_path / "_iso_data"),
           "AIDUMEM_LOG_DIR": str(tmp_path / "_iso_logs"),
           "AIDUMEM_URL": "http://127.0.0.1:1", "AIDUMEM_HOOK_QUIET": ""}
    # ① 太短
    proc = subprocess.run(
        ["bash", str(hook)], text=True, capture_output=True, timeout=30, env=env,
        input=json.dumps({"hook_event_name": "post_llm_call",
                          "extra": {"user_message": "嗯", "assistant_response": "好"}}))
    assert proc.returncode == 0
    assert "skipped" in proc.stderr and "门槛" in proc.stderr, \
        f"短消息静默跳过 = 下一次「怎么没记住」查不出来：{proc.stderr!r}"
    # ② 整轮都是终端输出
    noise = "\n".join(f"$ command {i}" for i in range(30))
    proc2 = subprocess.run(
        ["bash", str(hook)], text=True, capture_output=True, timeout=30, env=env,
        input=json.dumps({"hook_event_name": "post_llm_call",
                          "extra": {"user_message": noise,
                                    "assistant_response": noise}}))
    assert "命令行/日志输出" in proc2.stderr, f"噪声轮次没被拦也没留痕：{proc2.stderr!r}"


def test_write_wire_derives_turn_as_an_ordinal_not_from_a_string_id(tmp_path):
    """turn 必须是序号。

    宿主传的 turn_id 是字符串（Hermes 侧 `turn_id = str(...)`），首版直接
    int() 它必然 ValueError 落到 0 —— 实测生产真实写入的 origin_turn 全是 0，
    M1 轨迹信用要的「第几步」恒拿不到。改用会话内 user 轮数。
    """
    import json
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from pathlib import Path

    got: dict = {}

    class _H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            got["body"] = json.loads(self.rfile.read(n).decode("utf-8"))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            return

    srv = HTTPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.handle_request, daemon=True)
    t.start()

    hook = Path(__file__).resolve().parent.parent / "integrations" / "aidumem-ingest.sh"
    payload = json.dumps({
        "hook_event_name": "post_llm_call",
        "session_id": "s-ord",
        "extra": {
            "user_message": "第三轮的问题，长度足够越过门槛",
            "assistant_response": "回答",
            "turn_id": "turn-8f3a-not-a-number",   # 宿主真实形态：字符串
            "conversation_history": [
                {"role": "user", "content": "一"}, {"role": "assistant", "content": "1"},
                {"role": "user", "content": "二"}, {"role": "assistant", "content": "2"},
                {"role": "user", "content": "三"},
            ],
        },
    }, ensure_ascii=False)
    subprocess.run(["bash", str(hook)], input=payload, text=True,
                   capture_output=True, timeout=30,
                   env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": "/nonexistent",
                        "AIDUMEM_DATA_DIR": str(tmp_path / "_iso_data"),
                        "AIDUMEM_LOG_DIR": str(tmp_path / "_iso_logs"),
                        "AIDUMEM_URL": f"http://127.0.0.1:{port}",
                        "AIDUMEM_HOOK_QUIET": "1"})
    t.join(timeout=10)
    md = (got.get("body") or {}).get("metadata") or {}
    assert md.get("_origin_turn") == 3, (
        f"turn 应为会话内第 3 轮，实得 {md.get('_origin_turn')!r} —— "
        "0 说明又去 int() 那个字符串 id 了")


def test_session_coverage_reports_its_denominator():
    """覆盖率必须说清分母含后台通路，否则读数会被误解。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "ducky" / "hot"
           / "health.py").read_text(encoding="utf-8")
    assert "epistemic_session_with_7d" in src, "只给了比例没给分子"
    assert "epistemic_session_coverage_note" in src, "没说明分母的构成"


# ══════════════════════════════════════════════════════════════════
# v21.2.0 第三条线：会话精华萃取（session_end）
# ══════════════════════════════════════════════════════════════════

def test_distill_lane_is_slow_decay_not_the_emotion_lane():
    """精华走独立慢衰减泳道，绝不能复用 emotion。

    emotion 是 150% 快衰减 —— 那是给「今天有点烦」这类日常波动用的，设计没错；
    但会话精华是「这一程最值得记住的」，让它比普通记忆忘得更快是荒谬的。
    也不能用 preference 的 0.0（永不衰减）：每会话一条，几百条后会淹没检索。
    """
    from ducky.salience.config import LANE_DECAY_MULTIPLIER as M
    assert "distill" in M, "没有独立的精华泳道"
    assert M["distill"] < M["general"], "精华没比普通记忆留得久"
    assert M["distill"] < M["emotion"], \
        "精华掉进了 emotion 的快衰减，正好和它的用途相反"
    assert M["distill"] > 0, "0 是 preference 的语义（永不衰减），精华不该永驻"


def test_distill_emotion_weight_comes_from_the_existing_wordlist():
    """情感权重必须回溯到既有词表，不许是拍脑袋的新分数。"""
    import ducky.session_distill as sd
    from ducky.salience.config import LANE_KEYWORDS
    src = (sd.__file__ and open(sd.__file__, encoding="utf-8").read()) or ""
    assert "LANE_KEYWORDS" in src, "情感命中没用既有词表，等于自造了一个新维度"
    sample = "今天很开心，也有点难过"
    hits = sd._emotion_hits(sample)
    manual = sum(1 for kw in LANE_KEYWORDS["emotion"] if kw in sample)
    assert hits == manual > 0, f"命中数与词表对不上：{hits} vs {manual}"
    assert sd._emotion_hits("端口 8767 已启动") == 0, "纯技术内容不该算情感命中"


def test_distill_endpoint_only_extracts_and_can_be_rerun():
    """/session/distill 必须是纯提炼（不落库），否则排查时重跑会重复写入。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "ducky" / "routes_v8.py") \
        .read_text(encoding="utf-8")
    # 判据走 AST 并**剥掉 docstring**：端点的说明文字里就写着「由调用方再
    # POST /add」，substring 会把这句解释当成代码，判成「端点内部落库了」。
    # （本仓老账：注释冒充代码，一轮绊三次。）
    import ast
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "session_distill")
    body = fn.body[1:] if (fn.body and isinstance(fn.body[0], ast.Expr)
                           and isinstance(fn.body[0].value, ast.Constant)) else fn.body
    code = "\n".join(ast.get_source_segment(src, n) or "" for n in body)
    assert "distill_session" in code, "端点没调萃取"
    for forbidden in ("mem.add", '"/add"', "add_memory"):
        assert forbidden not in code, f"端点内部落库了（{forbidden}），重跑会重复写入"


def test_distill_hook_does_both_steps_and_targets_the_vector_path(tmp_path):
    """精华钩子必须两步都做：先提炼、再走 /add 落库。

    只提炼不落库 = 精华进不了向量库 = 召回不到 = 等于没做（这正是反思产物
    落独立表的老问题）。
    """
    import subprocess
    from pathlib import Path
    hook = Path(__file__).resolve().parent.parent / "integrations" / "aidumem-distill.sh"
    assert hook.is_file() and os.access(hook, os.X_OK), "第三条线的脚本不存在或不可执行"
    subprocess.run(["bash", "-n", str(hook)], check=True, capture_output=True, timeout=10)
    src = hook.read_text(encoding="utf-8")
    assert "session_end" in src, "没声明挂在哪个事件"
    assert "--selftest" in src, "缺少能吵起来的自检路径"
    for key in ("AIDUMEM_API_TOKEN", "AIDUMEM_USER_ID", "_lookup_env_key"):
        assert key in src, f"没走与另两条线同源的 {key}"

    # 「两步都做了」必须用行为验 —— shell 没有 AST，源码里把落库那行注释掉，
    # 任何 substring 判据都照样绿（首版被自己的负向对照抓住）。
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    seen: list = []

    class _H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8") if n else ""
            seen.append({"path": self.path.split("?")[0],
                         "body": json.loads(raw) if raw.strip() not in ("", "{}") else {}})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if self.path.startswith("/session/distill"):
                self.wfile.write(json.dumps({
                    "status": "ok", "summary": "这一程的精华",
                    "mode": "llm", "source_count": 9, "emotion_hits": 2,
                    "user_id": "u1", "bank_id": "default",
                    "metadata": {"kind": "session_distill", "lane": "distill",
                                 "_origin_agent": "session-distill"},
                }).encode("utf-8"))
            else:
                self.wfile.write(b'{"status":"ok"}')

        def log_message(self, *a):
            return

    srv = HTTPServer(("127.0.0.1", 0), _H)
    port = srv.server_address[1]

    def _serve():
        for _ in range(2):
            srv.handle_request()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    proc = subprocess.run(
        ["bash", str(hook)], text=True, capture_output=True, timeout=40,
        input=json.dumps({"hook_event_name": "session_end", "session_id": "s-9"}),
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent",
             "AIDUMEM_DATA_DIR": str(tmp_path / "_iso_data"),
             "AIDUMEM_LOG_DIR": str(tmp_path / "_iso_logs"),
             "AIDUMEM_URL": f"http://127.0.0.1:{port}",
             "AIDUMEM_USER_ID": "u1", "AIDUMEM_HOOK_QUIET": "1"})
    assert proc.returncode == 0, f"钩子非 0 退出会拖累宿主：{proc.stderr[:200]}"
    t.join(timeout=15)
    paths = [x["path"] for x in seen]
    assert "/session/distill" in paths, f"第一步（提炼）没发生：{paths}"
    assert "/add" in paths, (
        f"第二步（落库）没发生：{paths} —— 精华进不了向量库就召回不到，等于没做")
    add_body = next(x["body"] for x in seen if x["path"] == "/add")
    assert add_body.get("messages") == "这一程的精华", "落库的不是提炼结果"
    assert (add_body.get("metadata") or {}).get("_origin_agent") == "session-distill", \
        "没带 session-distill 标记，探针数不到它"


def test_distill_liveness_probe_watches_the_third_wire():
    """第三条线也要有探针，否则又是一个绿着的空转。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "ducky" / "hot" / "health.py") \
        .read_text(encoding="utf-8")
    # 判「判决真的被赋值」，不是「这个词出现过」—— except 分支里的
    # probes["distill_liveness_ok"] = None 会让 substring 判据恒绿（首版如此）。
    assert 'probes["distill_liveness_ok"] = not _dis_bad' in src, \
        "探针没有把判决落进 probes（只留了异常分支的 None = 恒绿）"
    assert 'DegradationTracker.record_degradation(\n                    "distill_liveness"' in src, \
        "判红了却不记降级 = 没人看得见"
    assert "distill_sessions_24h" in src and "distill_made_24h" in src, \
        "判据的分子分母没同时暴露"
    assert "_DISTILL_MIN_SESSIONS" in src, "阈值没走 env_config（不可配置也不 fail-closed）"
    assert "session-distill" in src, "没按 origin_agent 把精华自己排除出会话计数"


def test_distill_skips_short_sessions_with_a_stated_reason():
    """短会话跳过是正常的，但必须说出原因 —— 不许静默。"""
    import ducky.session_distill as sd
    out = sd.distill_session("__no_such_session__", user_id="nobody")
    assert out["status"] == "skipped", f"不存在的会话应判 skipped，实得 {out}"
    assert out.get("reason"), "跳过没有给原因，「这次怎么没精华」会查不出来"
    assert "min_required" in out or "source_count" in out, "没给出判据数字"
