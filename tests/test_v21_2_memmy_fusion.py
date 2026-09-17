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
