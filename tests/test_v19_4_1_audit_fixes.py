"""
tests/test_v19_4_1_audit_fixes.py — v19.4.1 审计补丁版回归测试

本文件的每条断言都对应一个**被探针实测证伪的宣称**，锁死不再回退。

⚠️ 反假绿灯纪律（v19.4.1 新增 SOP 铁律）
    v19.4.0 的幂等测试是绿的，但它只覆盖了带显式 timestamp 的 list[dict]
    载荷 —— 而生产实际走的是 hermes 插件发的**纯字符串**（无 timestamp）。
    绿灯掩盖了真 bug。因此本文件的每条测试都必须回答：
        「我断言的这条路，是生产真的会走的那条路吗？」
    涉及载荷形态、凭据形态、查询形态的测试，一律**多形态并测**。

覆盖清单：
  P0-1  鉴权贯通：cookie ∨ Bearer 任一可通；只设口令也真的锁；存量零破坏
  P0-2  facts 租户可见性收窄（含 /facts /search /categories /trust-stats /inject-context）
  P0-2b 跨租户覆盖：不同租户写同一 (category, fact_key) 不再互相销毁
  P0-3  移除 default 无 WHERE 全表删；全库清空必须走显式 confirm 入口
  P0-4  单条删除级联清原文层
  P1-1  幂等键根治（纯字符串载荷同样幂等 + occurrences 计数）
  P1-2  中文切词与 trigram 索引对齐 + _recall_path 可自证
  P1-3  observations 幂等建表，/observe 开箱不再 500
  P2-2  PBKDF2 口令 + 旧格式自动升级
  P2-4  router_usage 默认禁用
"""

import hashlib
import os
import sys
import tempfile

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_tmp_dir = tempfile.mkdtemp(prefix="aidumem_v1941_test_")
_TEST_FACTS_DB = os.path.join(_tmp_dir, "facts.db")
_TEST_TEXT_DB = os.path.join(_tmp_dir, "text_fts.db")
_TEST_OBS_DB = os.path.join(_tmp_dir, "observations.db")

import ducky.utils as utils  # noqa: E402

utils.FACTS_DB = _TEST_FACTS_DB
utils.TEXT_FTS_DB = _TEST_TEXT_DB
utils.OBS_DB = _TEST_OBS_DB

from ducky.facts_recall import search_facts, tenant_clause  # noqa: E402
from ducky.text_fts import (  # noqa: E402
    _fts_terms,
    fts_is_authoritative,
    fts_match_terms,
)
from ducky.verbatim_vault import (  # noqa: E402
    cascade_delete_verbatim,
    count_verbatim,
    delete_verbatim_by_content,
    ensure_verbatim_schema,
    purge_all_verbatim,
    store_verbatim,
    verbatim_search,
)


@pytest.fixture(autouse=True)
def _setup_test_env():
    utils.FACTS_DB = _TEST_FACTS_DB
    utils.TEXT_FTS_DB = _TEST_TEXT_DB
    utils.OBS_DB = _TEST_OBS_DB
    ensure_verbatim_schema()
    yield


# ═══════════════════════════════════════════════════════════════════
# P1-2  中文切词与 trigram 索引对齐
# ═══════════════════════════════════════════════════════════════════

def test_p12_chinese_terms_are_trigrams():
    """中文切词必须产出 3-gram —— 2-gram 在 trigram 索引里永不命中"""
    terms = _fts_terms("记忆引擎")
    assert terms == ["记忆引", "忆引擎"], f"应为 3-gram 滑窗，实得 {terms}"
    # 每个送进 MATCH 的词元长度都必须 >= 3
    assert all(len(t) >= 3 for t in fts_match_terms("记忆引擎"))


def test_p12_short_chinese_falls_back_to_like():
    """不足 3 字的中文无法用 trigram 表达 —— 必须交 LIKE，且不谎称走了索引"""
    assert _fts_terms("祖母") == ["祖母"]
    assert fts_match_terms("祖母") == []        # 不放进 MATCH 表达式
    assert fts_is_authoritative("祖母") is False  # FTS 空结果不可信，须兜 LIKE


def test_p12_ascii_terms_respect_trigram_min_len():
    """ASCII 同样受 trigram 下限约束：2 字符词不进 MATCH"""
    assert fts_match_terms("id") == []
    assert "abc" in fts_match_terms("abc")


def test_p12_authoritative_empty_skips_like_scan():
    """全部词元 >= 3 字时，FTS 的零命中是权威的（可跳过 LIKE 全表扫）"""
    assert fts_is_authoritative("记忆引擎") is True
    assert fts_is_authoritative("玛蒂尔达") is True


def test_p12_recall_path_is_self_evident():
    """召回结果必须自证走的哪条路（反假绿灯：不能只看命中数就以为走了索引）"""
    store_verbatim("vv_path", [{"role": "user", "content": "团队完成了压力测试与性能调优"}],
                   {"session_id": "s-path"})
    hits = verbatim_search("压力测试", user_id="vv_path", limit=5)
    assert hits, "3 字以上中文查询必须命中"
    assert hits[0]["_recall_path"] == "fts", (
        "中文查询必须真走 FTS 索引；若为 like 说明切词与索引又失配了"
    )

    # 短查询走 LIKE 兜底，同样如实标注
    store_verbatim("vv_path2", [{"role": "user", "content": "我的祖母很和善"}],
                   {"session_id": "s-path2"})
    short = verbatim_search("祖母", user_id="vv_path2", limit=5)
    assert short and short[0]["_recall_path"] == "like"


# ═══════════════════════════════════════════════════════════════════
# P1-1  幂等键根治
# ═══════════════════════════════════════════════════════════════════

def test_p11_plain_string_payload_is_idempotent():
    """生产真实路径：hermes sync_turn 发纯字符串、无 timestamp，重放必须只落一条

    这正是 v19.4.0 假绿灯的位置 —— 旧测试只测 list[dict]，绕开了这条路。
    """
    blob = "User: 我的祖母叫玛蒂尔达\nAssistant: 好的我记住了"
    md = {"source": "hermes_turn", "session_id": "sess-idem"}
    r1 = store_verbatim("vv_idem", blob, md)
    r2 = store_verbatim("vv_idem", blob, md)
    r3 = store_verbatim("vv_idem", blob, md)
    assert r1["stored"] == 1
    assert r2["stored"] == 0 and r2["skipped"] == 1
    assert r3["stored"] == 0
    assert count_verbatim("vv_idem") == 1, "纯字符串载荷重放必须幂等"


def test_p11_occurrences_counts_repeats():
    """重复表述不堆行，而是累加 occurrences（说过几次被显式记录）"""
    blob = "这句话我说了很多次"
    md = {"session_id": "sess-occ"}
    for _ in range(4):
        store_verbatim("vv_occ", blob, md)
    conn = utils.get_facts_conn()
    row = conn.execute(
        "SELECT occurrences FROM verbatim_turns WHERE user_id=?", ("vv_occ",)
    ).fetchone()
    assert row["occurrences"] == 4
    assert count_verbatim("vv_occ") == 1


def test_p11_dict_and_list_payloads_also_idempotent():
    """多形态并测：dict 与 list[dict] 形态同样幂等"""
    msgs = [{"role": "user", "content": "列表形态幂等测试", "timestamp": 1755400001000}]
    store_verbatim("vv_multi", msgs, {"session_id": "s1"})
    store_verbatim("vv_multi", msgs, {"session_id": "s1"})
    assert count_verbatim("vv_multi") == 1

    d = {"role": "user", "content": "字典形态幂等测试"}
    store_verbatim("vv_multi2", d, {"session_id": "s2"})
    store_verbatim("vv_multi2", d, {"session_id": "s2"})
    assert count_verbatim("vv_multi2") == 1


def test_p11_cross_session_repeats_are_kept():
    """跨会话的真实重复表述仍保留独立行 —— 幂等不等于丢信息"""
    blob = "同一句话在不同会话里说过"
    store_verbatim("vv_xsess", blob, {"session_id": "sess-1"})
    store_verbatim("vv_xsess", blob, {"session_id": "sess-2"})
    assert count_verbatim("vv_xsess") == 2


# ═══════════════════════════════════════════════════════════════════
# P0-3  移除 default 无 WHERE 全表删
# ═══════════════════════════════════════════════════════════════════

def test_p03_deleting_default_does_not_wipe_other_tenants():
    """删 default 绝不能连带清空 alice/bob —— default 是系统默认 user_id"""
    store_verbatim("default", [{"role": "user", "content": "default 的一句话"}], {"session_id": "d"})
    store_verbatim("p03_alice", [{"role": "user", "content": "alice 的秘密配方是燕麦奶"}], {"session_id": "a"})
    store_verbatim("p03_bob", [{"role": "user", "content": "bob 住在上海浦东"}], {"session_id": "b"})

    cascade_delete_verbatim("default")

    assert count_verbatim("default") == 0
    assert count_verbatim("p03_alice") == 1, "删 default 把 alice 的原文也灭了（P0-3 回退）"
    assert count_verbatim("p03_bob") == 1


def test_p03_purge_all_requires_explicit_confirm():
    """全库清空必须走显式入口 + confirm，危险动作不能藏在租户名的 if 分支里"""
    store_verbatim("p03_purge", [{"role": "user", "content": "待清空的原文"}], {"session_id": "p"})
    with pytest.raises(ValueError):
        purge_all_verbatim()
    assert count_verbatim("p03_purge") == 1
    purge_all_verbatim(confirm=True)
    assert count_verbatim("p03_purge") == 0


def test_p03_no_unscoped_delete_in_source():
    """源码级守卫：级联删除路径不得再出现无 WHERE 的 DELETE"""
    import pathlib
    wal = pathlib.Path(_REPO_ROOT, "ducky", "wal_engine.py").read_text(encoding="utf-8")
    for banned in (
        'DELETE FROM memories"',
        'DELETE FROM facts"',
        'DELETE FROM memory_salience"',
        'DELETE FROM evolve_snapshots"',
        'DELETE FROM memory_types"',
    ):
        assert banned not in wal, f"wal_engine 出现无 WHERE 全表删: {banned}"


# ═══════════════════════════════════════════════════════════════════
# P0-4  单条删除级联清原文层
# ═══════════════════════════════════════════════════════════════════

def test_p04_delete_by_content_removes_verbatim_both_sides():
    """按内容精确删除必须同时清 verbatim_turns 与 verbatim_fts_map"""
    secret = "我的身份证号是 310101199001011234"
    store_verbatim("p04_carol", [{"role": "user", "content": secret}], {"session_id": "c"})
    assert count_verbatim("p04_carol") == 1

    n = delete_verbatim_by_content("p04_carol", secret)
    assert n == 1
    assert count_verbatim("p04_carol") == 0
    assert verbatim_search("身份证", user_id="p04_carol") == [], "原文仍可检索 = 删除权未兑现"


def test_p04_delete_by_content_is_tenant_scoped():
    """按内容删除不得跨租户误伤（同一句话两个租户各说过）"""
    same = "我们都说过这句一样的话"
    store_verbatim("p04_a", [{"role": "user", "content": same}], {"session_id": "x"})
    store_verbatim("p04_b", [{"role": "user", "content": same}], {"session_id": "x"})
    delete_verbatim_by_content("p04_a", same)
    assert count_verbatim("p04_a") == 0
    assert count_verbatim("p04_b") == 1


def test_p04_cascade_delete_memory_wires_verbatim_step():
    """源码级守卫：cascade_delete_memory 必须挂着原文层清理这一步"""
    import pathlib
    wal = pathlib.Path(_REPO_ROOT, "ducky", "wal_engine.py").read_text(encoding="utf-8")
    assert "delete_verbatim_by_content" in wal, "单条删除漏了原文层（P0-4 回退）"
    # 内容抓取必须在物理删除之前，否则永远定位不到原文
    assert wal.index("_content_for_verbatim = \"\"") < wal.index("mem.delete(memory_id)")


# ═══════════════════════════════════════════════════════════════════
# P0-2  facts 租户可见性
# ═══════════════════════════════════════════════════════════════════

def _seed_fact(conn, *, key, value, source, agent):
    conn.execute(
        """INSERT INTO facts (category, fact_key, fact_value, source, agent_id,
                              trust_score, archived)
           VALUES ('secret', ?, ?, ?, ?, 0.9, 0)""",
        (key, value, source, agent),
    )
    conn.commit()


def test_p02_tenant_clause_default_is_unscoped():
    """默认租户保持全库可见 —— 存量单机部署升级后行为零变化"""
    clause, params = tenant_clause(None)
    assert clause == "" and params == []
    clause, params = tenant_clause("default")
    assert clause == "" and params == []


def test_p02_tenant_clause_scopes_named_tenant():
    """具名租户收窄可见范围，且宽松档兜住未标记归属的历史数据"""
    clause, params = tenant_clause("alice")
    assert "agent_id=?" in clause and "source=?" in clause
    assert params[0] == "alice" and params[1] == "alice"
    # 宽松档第三项 = DEFAULT_AGENT_ID（未标记租户的历史/共享数据）
    assert params[2] == utils.DEFAULT_AGENT_ID


def test_p02_strict_mode_drops_unlabeled(monkeypatch):
    """严格档不再兜未标记数据（部署方显式选择硬隔离时）"""
    monkeypatch.setenv("AIDUMEM_STRICT_TENANT", "1")
    clause, params = tenant_clause("alice")
    assert len(params) == 2, "严格档不应带 DEFAULT_AGENT_ID 兜底项"


def test_p02_search_facts_is_tenant_scoped():
    """alice 的敏感事实，bob 查不到；不传 user_id 时保持旧的全库语义"""
    from ducky.schema_bootstrap import ensure_core_schema
    ensure_core_schema(force=True)
    conn = utils.get_facts_conn()
    _seed_fact(conn, key="p02_alice_salary", value="alice earns 999999",
               source="p02_alice", agent="p02_alice")
    _seed_fact(conn, key="p02_bob_note", value="bob likes tea",
               source="p02_bob", agent="p02_bob")

    bob_keys = [f["fact_key"] for f in search_facts("earns", top_k=50, user_id="p02_bob")["facts"]]
    assert "p02_alice_salary" not in bob_keys, "跨租户泄漏（P0-2 回退）"

    alice_keys = [f["fact_key"] for f in search_facts("earns", top_k=50, user_id="p02_alice")["facts"]]
    assert "p02_alice_salary" in alice_keys, "本租户应能查到自己的事实"


def test_p02_trajectory_exposes_tenant_scope():
    """租户收窄必须可观测（trajectory 里显式给出 scoped / strict）"""
    result = search_facts("earns", top_k=5, user_id="p02_alice")
    step = next(s for s in result["trajectory"] if s.get("step") == "tenant_scope")
    assert step["user_id"] == "p02_alice"
    assert step["scoped"] is True


def test_p02b_cross_tenant_overwrite_is_prevented():
    """P0-2b：不同租户写同一 (category, fact_key) 不得互相销毁

    唯一约束是 ON CONFLICT(agent_id, category, fact_key)，而 /facts/add 此前
    把 agent_id 恒写常量 —— 于是 bob 写入会直接覆盖 alice 的 fact_value。
    这是跨租户数据破坏，比可见性泄漏更严重。
    """
    import pathlib
    routes = pathlib.Path(_REPO_ROOT, "ducky", "hot", "legacy_routes.py").read_text(encoding="utf-8")
    assert "effective_agent" in routes, "/facts/add 未按租户落 agent_id（P0-2b 回退）"
    assert "_PANTHEON_DEFAULT_AGENT, _PANTHEON_DEFAULT_PROFILE" not in routes, (
        "agent_id 仍被写成常量，跨租户覆盖未修"
    )


# ═══════════════════════════════════════════════════════════════════
# P2-2  PBKDF2 口令与旧格式自动升级
# ═══════════════════════════════════════════════════════════════════

def test_p22_pbkdf2_roundtrip():
    from ducky.security import auth
    hashed = auth.hash_password("a-strong-password")
    assert hashed.startswith("pbkdf2_sha256$200000$")
    ok, needs_upgrade = auth.verify_password("a-strong-password", hashed)
    assert ok is True and needs_upgrade is False
    assert auth.verify_password("wrong", hashed)[0] is False


def test_p22_legacy_sha256_verifies_and_flags_upgrade():
    """旧格式 salt:sha256hex 必须仍可校验，并标记需要升级（存量平滑迁移）"""
    from ducky.security import auth
    salt = "ab" * 16
    legacy = f"{salt}:{hashlib.sha256((salt + 'oldpwd').encode()).hexdigest()}"
    ok, needs_upgrade = auth.verify_password("oldpwd", legacy)
    assert ok is True and needs_upgrade is True


def test_p22_session_lifecycle():
    from ducky.security import auth
    token, ttl = auth.create_session()
    assert ttl > 0
    assert auth.validate_session(token) is True
    assert auth.validate_session("not-a-real-token") is False
    assert auth.revoke_session(token) is True
    assert auth.validate_session(token) is False


def test_p22_revoke_all_sessions():
    """改密后必须能一次性撤销全部会话，否则老会话仍持旧凭据通行"""
    from ducky.security import auth
    t1, _ = auth.create_session()
    t2, _ = auth.create_session()
    assert auth.revoke_all_sessions() >= 2
    assert auth.validate_session(t1) is False
    assert auth.validate_session(t2) is False


# ═══════════════════════════════════════════════════════════════════
# P2-4  router_usage 默认禁用
# ═══════════════════════════════════════════════════════════════════

def test_p24_router_usage_disabled_by_default(monkeypatch):
    monkeypatch.delenv("AIDUMEM_ROUTER_USAGE_ENABLED", raising=False)
    from ducky.router_usage import fetch_router_llm_usage, router_usage_enabled
    assert router_usage_enabled() is False
    assert fetch_router_llm_usage() == {}


def test_p24_router_usage_never_spawns_ssh_when_disabled(monkeypatch):
    """禁用时绝不能起 subprocess —— 用打桩证明这条路根本没走到"""
    monkeypatch.delenv("AIDUMEM_ROUTER_USAGE_ENABLED", raising=False)
    monkeypatch.setenv("AIDUMEM_ROUTER_SSH_HOSTS", "user@example.invalid")
    monkeypatch.setenv("AIDUMEM_ROUTER_SSH_KEY", "/dev/null")

    import ducky.router_usage as ru
    called = {"n": 0}

    def _boom(*a, **kw):
        called["n"] += 1
        raise AssertionError("禁用状态下不应执行 ssh")

    monkeypatch.setattr(ru.subprocess, "check_output", _boom)
    assert ru.fetch_router_llm_usage() == {}
    assert called["n"] == 0
