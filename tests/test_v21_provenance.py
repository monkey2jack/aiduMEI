"""
tests/test_v21_provenance.py — v21 F2 溯源三件套守卫
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
验收（任务书 §四-3.4，红→绿）：
  1. origin_context：set/get 往返、metadata 注入、extra/metadata 提取优先级
  2. track_knowledge_evolution：落库行带 origin 三件套；未设置时如实留空
  3. GET /knowledge/{id}/evolution：链+三件套可读；缺 scope 400；跨域 404
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp_dir = tempfile.mkdtemp(prefix="aidumem_v21_provenance_")

import ducky.utils as utils  # noqa: E402

utils.FACTS_DB = os.path.join(_tmp_dir, "facts.db")


@pytest.fixture(autouse=True)
def setup_test_db():
    fd, db_path = tempfile.mkstemp(prefix="facts_", suffix=".db", dir=_tmp_dir)
    os.close(fd)
    utils.FACTS_DB = db_path
    from ducky.schema_bootstrap import ensure_core_schema
    import ducky.memory_types as mt
    mt._checked = False  # 模块级一次性守卫：每条用例一个全新库文件，必须放行重建
    from ducky.memory_types import ensure_memory_types_schema
    ensure_core_schema(force=True)
    ensure_memory_types_schema()
    yield


# ── 1. origin_context 纯函数 ────────────────────────────────────────────────

def test_origin_set_get_roundtrip():
    from ducky.origin_context import get_origin, set_origin
    set_origin(agent="hermes", session_id="s-1", turn=7)
    assert get_origin() == ("hermes", "s-1", 7)
    set_origin()  # 总是可重置为空
    assert get_origin() == ("", "", 0)


def test_origin_from_metadata_and_extract():
    from ducky.origin_context import extract_origin_fields, set_origin_from_metadata, get_origin
    set_origin_from_metadata({"_origin_agent": "cron:tide", "_origin_session_id": "job-9", "_origin_turn": 0})
    assert get_origin() == ("cron:tide", "job-9", 0)
    # extra 优先于 metadata；hermes_session_id 兼容
    a, s, t = extract_origin_fields(
        {"session_id": "top-1"}, {"session_id": "md-1", "agent": "dudu"})
    assert (a, s, t) == ("dudu", "top-1", 0)
    a2, s2, _ = extract_origin_fields({}, {"hermes_session_id": "h-5"})
    assert (a2, s2) == ("", "h-5")
    # 非法 turn 不炸，如实归 0
    _, _, t3 = extract_origin_fields({"turn": "abc"}, {})
    assert t3 == 0


# ── 2. track_knowledge_evolution 落库三件套 ─────────────────────────────────

class _StubMemory:
    """最小 mem0 桩：返回一条与 new_text 中文词重叠的旧记忆。"""

    def __init__(self, old_id="old-uuid-1", old_text="用户决定用 围棋 开局"):
        self._old = {"id": old_id, "memory": old_text}

    def search(self, _text, filters=None, limit=5):
        return {"results": [dict(self._old)]}


def test_evolution_row_carries_origin():
    from ducky.layer1_selfcheck import track_knowledge_evolution
    from ducky.origin_context import set_origin
    set_origin(agent="hermes", session_id="sess-42", turn=12)
    track_knowledge_evolution(_StubMemory(), "dudu", "用户决定改用 围棋 后手", "new-uuid-9", bank_id="default")
    conn = utils.get_facts_conn()
    row = conn.execute(
        "SELECT origin_agent, origin_session_id, origin_turn FROM knowledge_evolution "
        "WHERE target_id='new-uuid-9'").fetchone()
    conn.close()
    assert tuple(row) == ("hermes", "sess-42", 12)


def test_evolution_row_without_context_stays_empty():
    from ducky.layer1_selfcheck import track_knowledge_evolution
    from ducky.origin_context import set_origin
    set_origin()  # 显式空上下文
    track_knowledge_evolution(_StubMemory(), "dudu", "用户决定改用 围棋 后手", "new-uuid-2", bank_id="default")
    conn = utils.get_facts_conn()
    row = conn.execute(
        "SELECT origin_agent, origin_session_id, origin_turn FROM knowledge_evolution "
        "WHERE target_id='new-uuid-2'").fetchone()
    conn.close()
    assert tuple(row) == ("", "", 0), "无上下文必须如实留空，不得编造"


# ── 3. 审计端点 ─────────────────────────────────────────────────────────────

def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ducky.routes_knowledge import register_knowledge_routes
    app = FastAPI()
    register_knowledge_routes(app)
    return TestClient(app)


def _seed_evolution():
    conn = utils.get_facts_conn()
    conn.execute(
        "INSERT INTO facts (category, fact_key, fact_value, user_id, bank_id, agent_id) "
        "VALUES ('general', 'k1', '内容', 'dudu', 'default', 'dudu')")
    conn.execute(
        "INSERT INTO knowledge_evolution (source_id, target_id, relation_type, confidence, reason,"
        " origin_agent, origin_session_id, origin_turn) "
        "VALUES ('old-1', 'fact:1', 'replaces', 0.9, 'test', 'hermes', 'sess-1', 3)")
    conn.execute(
        "INSERT INTO memory_types (memory_ref, memory_type, user_id, bank_id) "
        "VALUES ('uuid-x', 'semantic', 'dudu', 'default')")
    conn.execute(
        "INSERT INTO knowledge_evolution (source_id, target_id, relation_type, confidence, reason) "
        "VALUES ('uuid-y', 'uuid-x', 'enriches', 0.6, 'test')")
    conn.commit()
    conn.close()


def test_evolution_endpoint_returns_chain_with_provenance():
    _seed_evolution()
    r = _client().get("/knowledge/fact:1/evolution?user_id=dudu&bank_id=default")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    row = body["chain"][0]
    assert row["relation_type"] == "replaces"
    assert row["origin_agent"] == "hermes"
    assert row["origin_session_id"] == "sess-1"
    assert row["origin_turn"] == 3


def test_evolution_endpoint_requires_explicit_scope():
    _seed_evolution()
    c = _client()
    assert c.get("/knowledge/fact:1/evolution").status_code == 400
    assert c.get("/knowledge/fact:1/evolution?user_id=dudu").status_code == 400


def test_evolution_endpoint_cross_domain_is_404():
    _seed_evolution()
    c = _client()
    # fact: 数字 id 可枚举 → 保留域校验，跨域 404（不暴露存在性）
    assert c.get("/knowledge/fact:1/evolution?user_id=alice&bank_id=work").status_code == 404
    # UUID 不可枚举且载荷零正文 → 链上存在即可见（v21.0 收口改判据，
    # 生产用户 🟡-2：旧预检误杀 69% 真实查询）
    assert c.get("/knowledge/uuid-x/evolution?user_id=dudu&bank_id=default").status_code == 200
    assert c.get("/knowledge/uuid-x/evolution?user_id=alice&bank_id=work").status_code == 200
    # 链上不存在的 UUID → 404
    assert c.get("/knowledge/uuid-非存在/evolution?user_id=dudu&bank_id=default").status_code == 404
