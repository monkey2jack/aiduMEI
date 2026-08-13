"""
tests/test_p0_upgrades.py — aiduMEM v19.0 P0 认知层升级测试
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

覆盖 P0-1 ~ P0-4 四个升级项的纯函数/无网络路径：
    P0-1  时间戳记忆    — before/after 时间过滤参数归一化 + SQL 级过滤
    P0-2  记忆去重自编辑 — 编辑账本 schema / LLM 输出解析 / 回滚（mock memory）
    P0-3  Reflect 反思   — 洞察解析（JSON/纯文本兜底）、落库去重、上下文注入
    P0-4  检索升级       — 时间戳三级回退 / 时间衰减 / 时间窗口过滤

跑法：cd <仓库根> && python3 -m pytest tests/test_p0_upgrades.py -v
测试全部在临时 facts.db 上跑，绝不碰生产库；不发起真实网络请求。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── 关键：在导入任何 ducky 模块之前把 DB 指向临时库 ──
_tmp_dir = tempfile.mkdtemp(prefix="aidumem_p0_test_")
_TEST_DB = os.path.join(_tmp_dir, "facts.db")

import ducky.utils as utils  # noqa: E402

utils.FACTS_DB = _TEST_DB

# 跨文件并行/串行跑测试时，其他测试模块也可能在 import 阶段改掉
# utils.FACTS_DB。每个测试前再强制指回本文件的临时库，保证隔离。
@pytest.fixture(autouse=True)
def _bind_test_db():
    utils.FACTS_DB = _TEST_DB
    yield

_FACTS_DDL = """
CREATE TABLE facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL DEFAULT 'general',
    fact_key TEXT NOT NULL,
    fact_value TEXT NOT NULL,
    source TEXT DEFAULT 'local',
    confidence INTEGER DEFAULT 100,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    trust_score REAL DEFAULT 0.5,
    helpful_count INTEGER DEFAULT 0,
    unhelpful_count INTEGER DEFAULT 0,
    retrieval_count INTEGER DEFAULT 0,
    last_accessed_at TIMESTAMP,
    archived INTEGER DEFAULT 0,
    archived_at TIMESTAMP,
    summary TEXT,
    overview TEXT,
    level TEXT DEFAULT 'I',
    peer TEXT DEFAULT 'user',
    preference_score REAL DEFAULT 0.0,
    expires_at TEXT,
    valid_from TEXT,
    valid_to TEXT,
    agent_id TEXT DEFAULT 'local',
    profile TEXT DEFAULT 'default',
    memory_tier TEXT DEFAULT 'semantic',
    recorded_at TIMESTAMP,
    tags TEXT DEFAULT '',
    decay_at TEXT,
    shared INTEGER DEFAULT 1,
    sensitivity TEXT DEFAULT 'internal'
);
"""


@pytest.fixture(autouse=True)
def _fresh_facts_db():
    """每个测试都重建 facts / reflections / memory_edits 表，隔离测试数据。

    同时重置各模块的 _checked 幂等标志，避免「表已建」短路导致 DROP 后缺表。
    """
    import ducky.reflect as reflect_mod
    import ducky.self_edit as self_edit_mod

    conn = sqlite3.connect(_TEST_DB)
    for table in ("facts", "reflections", "memory_edits"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute(_FACTS_DDL)
    conn.commit()
    conn.close()
    reflect_mod._checked = False
    self_edit_mod._checked = False
    yield


# ════════════════════════════════════════════════════════════════
# P0-1  时间戳记忆：时间过滤参数归一化
# ════════════════════════════════════════════════════════════════

def test_time_bound_year():
    from ducky.facts_recall import _parse_time_bound
    assert _parse_time_bound("2026") == "2026-01-01"
    assert _parse_time_bound("2026", is_before=True) == "2026-12-31"


def test_time_bound_month():
    from ducky.facts_recall import _parse_time_bound
    assert _parse_time_bound("2026-06") == "2026-06-01"
    assert _parse_time_bound("2026-06", is_before=True) == "2026-06-30"
    assert _parse_time_bound("2026-02", is_before=True) == "2026-02-28"  # 闰年边界


def test_time_bound_day_and_iso():
    from ducky.facts_recall import _parse_time_bound
    assert _parse_time_bound("2026-06-15") == "2026-06-15"
    assert _parse_time_bound("2026-06-15T10:00:00+00:00") == "2026-06-15"
    assert _parse_time_bound("2026-06-15 10:00:00") == "2026-06-15"
    assert _parse_time_bound("") == ""


def _seed_facts():
    conn = sqlite3.connect(_TEST_DB)
    rows = [
        # (category, fact_key, fact_value, valid_from, valid_to, recorded_at, trust)
        ("偏好", "语言", "喜欢 Python", "2026-01-01T00:00:00+00:00", None, "2026-01-01 10:00:00", 0.9),
        ("偏好", "语言", "喜欢 Go", "2026-07-01T00:00:00+00:00", None, "2026-07-01 10:00:00", 0.9),
        ("偏好", "旧偏好", "喜欢 React (2026-03 止)", "2026-03-01T00:00:00+00:00", "2026-05-31T00:00:00+00:00", "2026-03-01 10:00:00", 0.7),
    ]
    conn.executemany(
        "INSERT INTO facts (category, fact_key, fact_value, valid_from, valid_to, recorded_at, trust_score) "
        "VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_search_facts_before_filter():
    _seed_facts()
    from ducky.facts_recall import search_facts
    res = search_facts("偏好", before="2026-03", top_k=10)
    keys = {f["fact_key"] for f in res["facts"]}
    # before=2026-03：只应出现 2026-03 之前（含）就已存在的事实
    assert "语言" in keys          # 喜欢 Python（valid_from=2026-01）
    assert "旧偏好" in keys        # valid_from=2026-03-01（恰好在界内）
    # 喜欢 Go（valid_from=2026-07-01）在 2026-03 之后才生效，必须被排除
    go_rows = [f for f in res["facts"] if f["fact_value"] == "喜欢 Go"]
    assert go_rows == []


def test_search_facts_after_filter():
    _seed_facts()
    from ducky.facts_recall import search_facts
    res = search_facts("偏好", after="2026-06", top_k=10)
    # after=2026-06：只召回「2026-06 之后仍有效」的事实
    # 喜欢 Python（valid_to 为空=持续有效）→ 保留
    # 喜欢 Go（valid_to 为空=持续有效）→ 保留
    # 旧偏好（valid_to=2026-05-31 < 2026-06-01）→ 排除
    values = {f["fact_value"] for f in res["facts"]}
    assert "喜欢 Python" in values
    assert "喜欢 Go" in values
    assert "喜欢 React (2026-03 止)" not in values


def test_search_facts_no_filter_returns_all():
    _seed_facts()
    from ducky.facts_recall import search_facts
    res = search_facts("偏好", top_k=10)
    assert res["count"] == 3
    assert res["time_filter"] == {"before": "", "after": ""}


def test_search_facts_time_filter_in_trajectory():
    _seed_facts()
    from ducky.facts_recall import search_facts
    res = search_facts("语言", after="2026-01")
    steps = {t["step"] for t in res["trajectory"]}
    assert "time_filter" in steps


# ════════════════════════════════════════════════════════════════
# P0-2  记忆去重自编辑：解析 + 账本 + 回滚（mock memory）
# ════════════════════════════════════════════════════════════════

class _FakeMemory:
    """极简 mem0 替身：支持 search/get_all/update/delete，不发网络。"""

    def __init__(self):
        self.store = {
            "m1": {"memory": "用户喜欢 Python", "created_at": "2026-01-01T00:00:00+00:00"},
            "m2": {"memory": "用户喜欢 Go", "created_at": "2026-07-01T00:00:00+00:00"},
        }

    def search(self, query, filters=None, limit=3):
        # 返回所有候选（按 id 排序，稳定）
        results = [
            {"id": mid, "memory": v["memory"], "score": 0.9, "created_at": v["created_at"]}
            for mid, v in self.store.items()
        ]
        return {"results": results[:limit]}

    def get_all(self, filters=None, limit=10000):
        return {"results": [
            {"id": mid, "memory": v["memory"], "created_at": v["created_at"]}
            for mid, v in self.store.items()
        ]}

    def update(self, memory_id, data, metadata=None):
        self.store[memory_id]["memory"] = data
        return {"results": [{"id": memory_id, "memory": data}]}

    def add(self, messages, user_id=None, metadata=None):
        mid = f"m{len(self.store) + 1}"
        text = messages if isinstance(messages, str) else str(messages)
        self.store[mid] = {"memory": text, "created_at": datetime.now(timezone.utc).isoformat()}
        return {"results": [{"id": mid, "memory": text}]}


def test_self_edit_parse_decision_json():
    from ducky.self_edit import _parse_decision
    d = _parse_decision('{"decision":"duplicate","memory_id":"m1","merged_content":"合并","confidence":0.8,"reason":"同主题"}')
    assert d["decision"] == "duplicate"
    assert d["memory_id"] == "m1"


def test_self_edit_parse_decision_wrapped():
    from ducky.self_edit import _parse_decision
    d = _parse_decision('前缀说明\n{"decision":"conflict","memory_id":"m2","merged_content":"旧：A | 新：B"}\n尾注')
    assert d["decision"] == "conflict"


def test_self_edit_parse_decision_invalid():
    from ducky.self_edit import _parse_decision
    assert _parse_decision("没有 JSON 输出") is None
    assert _parse_decision("") is None


def test_self_edit_log_and_list_and_rollback(monkeypatch):
    from ducky import self_edit
    from ducky.self_edit import ensure_self_edit_schema, _log_edit, list_edits, rollback_edit

    ensure_self_edit_schema()
    mem = _FakeMemory()

    # 先记一笔编辑账本（模拟 duplicate 合并）
    edit_id = _log_edit("m1", "default", "duplicate", "用户喜欢 Python", "用户喜欢 Python 与 Go", "同主题合并", 0.8)
    assert edit_id > 0

    edits = list_edits(user_id="default")
    assert len(edits) == 1
    assert edits[0]["action"] == "duplicate"
    assert edits[0]["old_content"] == "用户喜欢 Python"

    # 回滚：把 m1 恢复到旧内容
    res = rollback_edit(edit_id, memory=mem)
    assert res["status"] == "ok"
    assert mem.store["m1"]["memory"] == "用户喜欢 Python"

    # 二次回滚应失败（已回滚）
    res2 = rollback_edit(edit_id, memory=mem)
    assert res2["status"] == "error"


def test_self_edit_extract_text():
    from ducky.self_edit import _extract_text
    assert _extract_text("纯文本") == "纯文本"
    assert _extract_text([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]) == "a b"
    assert _extract_text({"role": "user", "content": "dict文本"}) == "dict文本"


# ════════════════════════════════════════════════════════════════
# P0-3  Reflect 反思：解析 + 落库去重 + 上下文注入
# ════════════════════════════════════════════════════════════════

def test_reflect_parse_json_array():
    from ducky.reflect import _parse_insights
    raw = json_dumps = '[{"type":"pattern","content":"重复提到定位","confidence":0.8,"evidence":["m1"]}]'
    insights = _parse_insights(raw)
    assert len(insights) == 1
    assert insights[0]["type"] == "pattern"
    assert insights[0]["confidence"] == 0.8


def test_reflect_parse_single_object():
    from ducky.reflect import _parse_insights
    insights = _parse_insights('{"type":"gap","content":"缺时区信息","confidence":0.6}')
    assert len(insights) == 1
    assert insights[0]["type"] == "gap"


def test_reflect_parse_wrapped_json():
    from ducky.reflect import _parse_insights
    raw = '好的，以下是洞察：\n[{"type":"prediction","content":"下一步可能部署","confidence":0.7}]\n希望有帮助'
    insights = _parse_insights(raw)
    assert len(insights) == 1
    assert insights[0]["type"] == "prediction"


def test_reflect_parse_plain_text_fallback():
    from ducky.reflect import _parse_insights
    raw = "- 模式识别：用户频繁提到部署\n- 知识缺口：缺少联系方式"
    insights = _parse_insights(raw)
    assert len(insights) == 2
    assert all(i["confidence"] == 0.5 for i in insights)


def test_reflect_parse_invalid():
    from ducky.reflect import _parse_insights
    assert _parse_insights("") == []
    assert _parse_insights("没有任何结构化内容") == []


def test_reflect_save_dedup_and_list():
    from ducky.reflect import ensure_reflect_schema, save_insights, get_reflections

    ensure_reflect_schema()
    insights = [
        {"type": "pattern", "content": "用户近期专注产品定位", "confidence": 0.8, "evidence": ["m1"]},
    ]
    added = save_insights(insights, user_id="default", source="test")
    assert added == 1

    # 重复内容不重复入库
    added2 = save_insights(insights, user_id="default", source="test")
    assert added2 == 0

    rows = get_reflections(user_id="default", limit=10)
    assert len(rows) == 1
    assert rows[0]["type_label"] == "模式识别"


def test_reflect_inject_context():
    from ducky.reflect import ensure_reflect_schema, save_insights, inject_reflections

    ensure_reflect_schema()
    save_insights(
        [{"type": "prediction", "content": "用户接下来可能要部署到生产", "confidence": 0.75}],
        user_id="default", source="test",
    )
    ctx = inject_reflections(user_id="default", limit=5)
    assert "[Reflections" in ctx
    assert "部署到生产" in ctx


def test_reflect_run_no_llm_returns_empty(monkeypatch):
    """LLM 不可用时 run_reflect 应返回空洞察而非抛异常。"""
    import ducky.reflect as reflect_mod

    # 直接注入一个假 memory，并让 call_llm 返回 None（模拟无网络）
    monkeypatch.setattr(reflect_mod, "call_llm", lambda *a, **k: None)
    fake = _FakeMemory()
    report = reflect_mod.run_reflect(memory=fake, user_id="default", top_k=5, source="test")
    assert report["status"] == "ok"
    assert report["llm_used"] is False
    assert report["insights"] == []


# ════════════════════════════════════════════════════════════════
# P0-4  检索升级：时间戳回退 + 时间衰减 + 时间窗口过滤
# ════════════════════════════════════════════════════════════════

def test_engine_extract_timestamp_priority():
    from ducky.engine import extract_timestamp
    item = {
        "created_at": "2026-08-01T00:00:00+00:00",
        "metadata": {"recorded_at": "2026-08-10T00:00:00+00:00"},
    }
    # created_at 优先
    assert extract_timestamp(item) > 0


def test_engine_extract_timestamp_fallback_to_metadata():
    from ducky.engine import extract_timestamp
    item = {"created_at": "", "metadata": {"recorded_at": "2026-08-10T00:00:00+00:00"}}
    assert extract_timestamp(item) > 0
    item2 = {"created_at": "", "metadata": {"valid_from": "2026-08-11T00:00:00+00:00"}}
    assert extract_timestamp(item2) > 0


def test_engine_extract_timestamp_unknown():
    from ducky.engine import extract_timestamp
    assert extract_timestamp({}) == 0.0
    assert extract_timestamp({"created_at": "garbage"}) == 0.0


def test_engine_time_decay_newer_higher():
    """时间衰减：新记忆衰减分必须高于旧记忆。"""
    import math
    from ducky import engine as engine_mod

    now = engine_mod.time.time()
    lam = engine_mod.RECENCY_LAMBDA
    new_score = math.exp(-lam * 1)     # 1 天前
    old_score = math.exp(-lam * 365)   # 1 年前
    assert new_score > old_score


def test_engine_search_time_window_filter(monkeypatch):
    """before/after 时间窗口过滤应剔除窗口外的候选。"""
    from ducky import engine as engine_mod

    class _Mem:
        def search(self, query, filters=None, limit=10):
            return {"results": [
                {"id": "a", "memory": "旧记忆", "score": 0.9, "created_at": "2025-01-01T00:00:00+00:00"},
                {"id": "b", "memory": "新记忆", "score": 0.9, "created_at": "2026-08-01T00:00:00+00:00"},
            ]}

    eng = engine_mod.RecallEngine(memory_instance=_Mem())
    # 禁用 rerank（无网络）
    monkeypatch.setattr(engine_mod, "RERANK_WEIGHT", 0.0)

    # before=2026-01：只保留 2026-01 之前产生的记忆 → 只剩 "旧记忆"
    results = eng.search("记忆", "default", limit=10, before="2026-01")
    ids = {r["id"] for r in results}
    assert "a" in ids
    assert "b" not in ids

    # after=2026-01：只保留 2026-01 之后产生的记忆 → 只剩 "新记忆"
    results2 = eng.search("记忆", "default", limit=10, after="2026-01")
    ids2 = {r["id"] for r in results2}
    assert "b" in ids2
    assert "a" not in ids2


def test_engine_search_no_time_filter_returns_all(monkeypatch):
    from ducky import engine as engine_mod

    class _Mem:
        def search(self, query, filters=None, limit=10):
            return {"results": [
                {"id": "a", "memory": "旧记忆", "score": 0.9, "created_at": "2025-01-01T00:00:00+00:00"},
                {"id": "b", "memory": "新记忆", "score": 0.9, "created_at": "2026-08-01T00:00:00+00:00"},
            ]}

    eng = engine_mod.RecallEngine(memory_instance=_Mem())
    monkeypatch.setattr(engine_mod, "RERANK_WEIGHT", 0.0)
    results = eng.search("记忆", "default", limit=10)
    assert len(results) == 2
