"""
tests/test_v19_4_verbatim_vault.py — v19.4.0 明镜工程 Phase 1 原文保真层回归测试

覆盖内容：
1. Verbatim Vault 建表幂等（facts.db verbatim_turns + text_fts.db verbatim_fts）
2. 原文逐字保真写入（list / dict / 纯字符串三种 messages 形态）
3. 幂等去重（同租户同内容同时间戳重放只落一条）
4. 租户硬隔离（A 的原文 B 搜不到、删不掉）
5. 原文全文检索（FTS trigram 命中 + LIKE 兜底）
6. 原文证据融合（重合打标 / 配额保留 / limit 约束 / 相关度门槛 🟡-3）
7. 级联删除（facts.db + text_fts.db 双侧清干净，default 全清语义）
8. 主链路挂钩存在性（/add /search cascade_delete_all 三处钩子不得被误删）
"""

import os
import sys
import tempfile

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_tmp_dir = tempfile.mkdtemp(prefix="aidumem_v19_4_test_")
_TEST_FACTS_DB = os.path.join(_tmp_dir, "facts.db")
_TEST_TEXT_DB = os.path.join(_tmp_dir, "text_fts.db")

import ducky.utils as utils
utils.FACTS_DB = _TEST_FACTS_DB
utils.TEXT_FTS_DB = _TEST_TEXT_DB

from ducky.verbatim_vault import (
    cascade_delete_verbatim,
    count_verbatim,
    count_verbatim_all,
    ensure_verbatim_schema,
    fuse_verbatim,
    store_verbatim,
    verbatim_search,
)


@pytest.fixture(autouse=True)
def _setup_test_env():
    utils.FACTS_DB = _TEST_FACTS_DB
    utils.TEXT_FTS_DB = _TEST_TEXT_DB
    ensure_verbatim_schema()
    yield


# ─────────────────────────────────────────────────────────────
# 1. 建表幂等
# ─────────────────────────────────────────────────────────────

def test_schema_idempotent():
    """重复建表不报错，表结构稳定存在"""
    ensure_verbatim_schema()
    ensure_verbatim_schema()
    fconn = utils.get_facts_conn()
    row = fconn.execute(
        "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table' AND name='verbatim_turns'"
    ).fetchone()
    assert row["n"] == 1
    tconn = utils.get_text_conn()
    row2 = tconn.execute(
        "SELECT COUNT(*) AS n FROM sqlite_master WHERE type='table' AND name='verbatim_fts_map'"
    ).fetchone()
    assert row2["n"] == 1


# ─────────────────────────────────────────────────────────────
# 2. 原文逐字保真写入
# ─────────────────────────────────────────────────────────────

def test_store_verbatim_list_messages():
    """list[dict] 形态逐条落库，内容一字不丢"""
    msgs = [
        {"role": "user", "content": "我今天早上喝了一杯热拿铁，不加糖", "timestamp": 1755400000000},
        {"role": "assistant", "content": "好的，我记住了你喜欢热拿铁不加糖"},
        {"role": "user", "content": ""},  # 空内容不落库
    ]
    res = store_verbatim("test_vv_user", msgs, {"session_id": "sess-001"})
    assert res["stored"] == 2
    assert count_verbatim("test_vv_user") == 2

    hits = verbatim_search("热拿铁", user_id="test_vv_user", limit=5)
    assert any("热拿铁" in (h.get("memory") or "") for h in hits)
    # 时间戳归一为 ISO
    hit = next(h for h in hits if "热拿铁" in (h.get("memory") or ""))
    assert hit["recorded_at"] and "T" in str(hit["recorded_at"])
    assert hit["session_id"] == "sess-001"


def test_store_verbatim_dict_and_string():
    """dict 与纯字符串形态也能落库"""
    res1 = store_verbatim("test_vv_user2", {"role": "user", "content": "我的生日是5月20日"})
    assert res1["stored"] == 1
    res2 = store_verbatim("test_vv_user2", "纯字符串形态的记忆原文")
    assert res2["stored"] == 1
    assert count_verbatim("test_vv_user2") == 2


# ─────────────────────────────────────────────────────────────
# 3. 幂等去重
# ─────────────────────────────────────────────────────────────

def test_store_verbatim_idempotent():
    """同租户同内容同时间戳重放只落一条"""
    msgs = [{"role": "user", "content": "重复写入测试句", "timestamp": 1755400001000}]
    r1 = store_verbatim("test_vv_dup", msgs)
    r2 = store_verbatim("test_vv_dup", msgs)
    assert r1["stored"] == 1
    assert r2["skipped"] == 1
    assert count_verbatim("test_vv_dup") == 1


# ─────────────────────────────────────────────────────────────
# 4. 租户硬隔离
# ─────────────────────────────────────────────────────────────

def test_tenant_isolation():
    """A 的原文 B 搜不到也删不掉"""
    store_verbatim("test_vv_alice", [{"role": "user", "content": "Alice 的秘密配方是燕麦奶"}])
    store_verbatim("test_vv_bob", [{"role": "user", "content": "Bob 喜欢美式咖啡"}])

    # B 搜不到 A 的内容
    hits_b = verbatim_search("燕麦奶", user_id="test_vv_bob", limit=5)
    assert not any("Alice" in (h.get("memory") or "") for h in hits_b)

    # 删 B 不影响 A
    cascade_delete_verbatim("test_vv_bob")
    assert count_verbatim("test_vv_bob") == 0
    assert count_verbatim("test_vv_alice") == 1


# ─────────────────────────────────────────────────────────────
# 5. 原文全文检索
# ─────────────────────────────────────────────────────────────

def test_verbatim_search_chinese_bigram():
    """中文 2-gram 切词命中"""
    store_verbatim("test_vv_search", [
        {"role": "user", "content": "团队近期完成了压力测试"},
        {"role": "user", "content": "今天天气晴朗适合爬山"},
    ])
    hits = verbatim_search("压力测试", user_id="test_vv_search", limit=5)
    assert hits
    assert "压力测试" in hits[0]["memory"]
    assert hits[0]["_verbatim"] is True
    assert hits[0]["id"].startswith("verbatim:")


def test_verbatim_search_empty_query():
    """空查询干净返回空列表"""
    assert verbatim_search("", user_id="test_vv_search") == []
    assert verbatim_search("任何内容", user_id="") == []


# ─────────────────────────────────────────────────────────────
# 6. 原文证据融合
# ─────────────────────────────────────────────────────────────

def test_fuse_verbatim_dedup_marks():
    """与主干重合的原文只打标不重复追加"""
    main = [{"id": "m1", "memory": "用户喜欢热拿铁"}]
    hits = [{"id": "verbatim:1", "memory": "用户喜欢热拿铁", "_verbatim": True}]
    merged = fuse_verbatim(main, hits, limit=10)
    assert len(merged) == 1
    assert merged[0].get("_has_verbatim") is True


def test_fuse_verbatim_quota_and_limit():
    """原文证据保留配额，总数不超 limit"""
    main = [{"id": f"m{i}", "memory": f"主干记忆 {i}"} for i in range(10)]
    hits = [{"id": f"verbatim:{i}", "memory": f"原文证据 {i}", "_verbatim": True} for i in range(6)]
    merged = fuse_verbatim(main, hits, limit=10)
    assert len(merged) == 10
    verbatim_items = [m for m in merged if m.get("_verbatim")]
    assert 1 <= len(verbatim_items) <= max(2, 10 // 4)
    # 主干优先：原文追加在尾部
    assert merged[-1].get("_verbatim") is True


def test_fuse_verbatim_no_hits_returns_main():
    """无原文命中时原样返回主干"""
    main = [{"id": "m1", "memory": "x"}]
    assert fuse_verbatim(main, [], limit=10) == main


def test_fuse_verbatim_relevance_gate_filters_low_overlap():
    """🟡-3 相关度门槛：查询词足够多时，低重合原文不配占位"""
    main = [{"id": "m1", "memory": "主干记忆甲"}]
    hits = [
        # 与查询词重合 2 个（热拿/拿铁）→ 应入选
        {"id": "verbatim:1", "memory": "用户喜欢热拿铁", "_verbatim": True},
        # 与查询词零重合 → 应被门槛拦下
        {"id": "verbatim:2", "memory": "今天天气不错呀", "_verbatim": True},
    ]
    merged = fuse_verbatim(main, hits, limit=10, query="热拿铁 咖啡")
    verbatim_items = [m for m in merged if m.get("_verbatim")]
    assert len(verbatim_items) == 1
    assert verbatim_items[0]["id"] == "verbatim:1"


def test_fuse_verbatim_relevance_gate_off_for_short_query():
    """🟡-3 短查询不设卡：查询词少于门槛值时命中照常融合（本身已是 BM25 召回）"""
    main = [{"id": "m1", "memory": "主干记忆甲"}]
    hits = [{"id": "verbatim:1", "memory": "今天天气不错呀", "_verbatim": True}]
    merged = fuse_verbatim(main, hits, limit=10, query="天气")
    assert any(m.get("_verbatim") for m in merged)


def test_fuse_verbatim_quota_constants_respected():
    """🟡-3 配额走模块级常量：quota = max(MIN_QUOTA, limit // QUOTA_RATIO)"""
    from ducky.verbatim_vault import (
        VERBATIM_FUSE_MIN_QUOTA,
        VERBATIM_FUSE_QUOTA_RATIO,
    )
    main = [{"id": f"m{i}", "memory": f"主干记忆 {i}"} for i in range(20)]
    hits = [
        {"id": f"verbatim:{i}", "memory": f"原文证据 {i}", "_verbatim": True}
        for i in range(10)
    ]
    for limit in (5, 8, 20):
        merged = fuse_verbatim(main[:limit], hits, limit=limit)
        n_verbatim = sum(1 for m in merged if m.get("_verbatim"))
        expected_quota = max(VERBATIM_FUSE_MIN_QUOTA, limit // VERBATIM_FUSE_QUOTA_RATIO)
        assert n_verbatim <= expected_quota
        assert len(merged) <= limit


# ─────────────────────────────────────────────────────────────
# 7. 级联删除
# ─────────────────────────────────────────────────────────────

def test_cascade_delete_verbatim_cleans_both_stores():
    """级联删除同时清空 facts.db 与 text_fts.db 双侧"""
    store_verbatim("test_vv_cascade", [
        {"role": "user", "content": "待清除的原文甲"},
        {"role": "user", "content": "待清除的原文乙"},
    ])
    assert count_verbatim("test_vv_cascade") == 2

    deleted = cascade_delete_verbatim("test_vv_cascade")
    assert deleted == 2
    assert count_verbatim("test_vv_cascade") == 0

    # FTS 侧也清干净，搜不到了
    assert verbatim_search("待清除", user_id="test_vv_cascade", limit=5) == []


def test_cascade_delete_empty_user_rejected():
    """空 user_id 直接返回 0，绝不误删"""
    assert cascade_delete_verbatim("") == 0
    assert cascade_delete_verbatim("   ") == 0


# ─────────────────────────────────────────────────────────────
# 8. 主链路挂钩存在性（防误删钩子）
# ─────────────────────────────────────────────────────────────

def test_hooks_present_in_hot_paths():
    """/add /search wal_engine 三处钩子必须存在"""
    with open(os.path.join(_REPO_ROOT, "ducky", "hot", "add.py"), encoding="utf-8") as f:
        add_src = f.read()
    assert "store_verbatim" in add_src

    with open(os.path.join(_REPO_ROOT, "ducky", "hot", "search.py"), encoding="utf-8") as f:
        search_src = f.read()
    assert "verbatim_search" in search_src
    assert "fuse_verbatim" in search_src

    with open(os.path.join(_REPO_ROOT, "ducky", "wal_engine.py"), encoding="utf-8") as f:
        wal_src = f.read()
    assert "cascade_delete_verbatim" in wal_src


# ─────────────────────────────────────────────────────────────
# 9. 全库计数与 /health 探针（UI 补丁）
# ─────────────────────────────────────────────────────────────

def test_count_verbatim_all_aggregates_tenants():
    """count_verbatim_all 汇总全库原文条数（跨租户），供 /health 探针"""
    store_verbatim("test_vv_all_a", [{"role": "user", "content": "全库计数甲"}])
    store_verbatim("test_vv_all_b", [
        {"role": "user", "content": "全库计数乙"},
        {"role": "assistant", "content": "全库计数丙"},
    ])
    total = count_verbatim_all()
    assert total >= 3  # 至少包含本次写入的 3 条（测试库可能有其他用例残留）
    assert count_verbatim("test_vv_all_a") == 1
    assert count_verbatim("test_vv_all_b") == 2

    # 级联清掉本用例数据，避免污染其他用例的 total 断言
    cascade_delete_verbatim("test_vv_all_a")
    cascade_delete_verbatim("test_vv_all_b")


def test_health_probe_emits_verbatim_and_raw_drawer():
    """/health 必须产出 verbatim_count / verbatim_ok 与 raw_drawer_count / raw_drawer_ok
    （前端 STORAGE LAYERS 面板依赖这些探针，杜绝再次出现哑显示）"""
    with open(os.path.join(_REPO_ROOT, "ducky", "hot", "health.py"), encoding="utf-8") as f:
        health_src = f.read()
    assert "verbatim_count" in health_src
    assert "verbatim_ok" in health_src
    assert "count_verbatim_all" in health_src
    assert "raw_drawer_count" in health_src
    assert "raw_drawer_ok" in health_src


def test_frontend_panels_render_verbatim_layer():
    """前端 STORAGE LAYERS 面板必须渲染「原文保真 VERBATIM」层并读取探针"""
    with open(os.path.join(_REPO_ROOT, "frontend", "js", "panels.js"), encoding="utf-8") as f:
        panels_src = f.read()
    assert "原文保真" in panels_src
    assert "VERBATIM" in panels_src
    assert "probes.verbatim_count" in panels_src
