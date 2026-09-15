"""v21.1「众神殿」会话域收口 —— 红→绿对照守卫。

每条断言对应改前的一个真实缺陷（改前必红）：
- WP-3 跨殿会话夺权：`session_start` 写侧此前无条件覆盖（memory_persistence.py:84），
  A 传 B 的 session_id 即可静默清空并夺走 B 的会话。改前无 SessionOwnerConflict 类。
- WP-5 session_id 零校验：改前仅 `.strip()`，超长/换行/URL·SQL 元字符原样收下。
  改前无 SessionIdError 类。
- WP-7 reflect 溯源上下文不复位：改前 `set_origin` 只 set 不 reset（reflect.py:514），
  反思跑完污染后续演化写入的 origin。
- WP-2 plugin 建/结束会话不带当前殿 user_id：改前 URL 只有 session_id，
  反思落 default 殿而非本 bot 的殿。
"""
import ast
import pathlib

import pytest


def _fresh_mp():
    from ducky.pipeline import memory_persistence as mp
    with mp._sessions_lock:
        mp._sessions.clear()
    return mp


# ── WP-3：跨殿会话夺权 ──────────────────────────────────────────────
def test_wp3_cross_domain_session_id_cannot_be_hijacked():
    mp = _fresh_mp()
    r1 = mp.session_start("athena", bank_id="default", session_id="shared_sid_001")
    assert r1["session_id"] == "shared_sid_001"
    assert r1["user_id"] == "athena"
    # zeus 殿传同一 session_id 企图覆盖夺权 —— 必须拒绝
    with pytest.raises(mp.SessionOwnerConflict):
        mp.session_start("zeus", bank_id="default", session_id="shared_sid_001")
    # athena 的会话毫发无损
    with mp._sessions_lock:
        assert mp._sessions["shared_sid_001"]["user_id"] == "athena"


def test_wp3_same_domain_restart_is_allowed():
    mp = _fresh_mp()
    mp.session_start("athena", session_id="sid_reconnect")
    # 同殿重复 start（重连/幂等）放行，不抛
    r = mp.session_start("athena", session_id="sid_reconnect")
    assert r["session_id"] == "sid_reconnect"


# ── WP-5：session_id 校验（含日志注入形态）────────────────────────
def test_wp5_session_id_rejects_illegal():
    mp = _fresh_mp()
    with pytest.raises(mp.SessionIdError):
        mp.session_start("athena", session_id="x" * 201)          # 超长
    with pytest.raises(mp.SessionIdError):
        mp.session_start("athena", session_id="sid\n[CRIT] fake")  # 换行=日志注入
    for bad in ["a b", "a&b", "a#b", "a/b", "a%00", "a'b"]:        # 空格/URL/路径/SQL 元字符
        with pytest.raises(mp.SessionIdError):
            mp.session_start("athena", session_id=bad)


def test_wp5_session_id_accepts_uuid_and_ses_fallback():
    mp = _fresh_mp()
    r = mp.session_start("athena", session_id="550e8400-e29b-41d4-a716-446655440000")
    assert r["session_id"] == "550e8400-e29b-41d4-a716-446655440000"
    mp2 = _fresh_mp()
    r2 = mp2.session_start("athena")           # 未传 → 服务端生成
    assert r2["session_id"].startswith("ses_")


# ── WP-7：reflect 跑完必复位 origin（无 stale leak）────────────────
def test_wp7_reflect_resets_origin_context(monkeypatch):
    from ducky import origin_context as oc
    from ducky import reflect
    # 桩掉 DB/检索依赖，让 run_reflect 走 early-return（no memories & no facts）
    monkeypatch.setattr(reflect, "ensure_reflect_schema", lambda: None)
    monkeypatch.setattr(reflect, "_gather_recent_memories", lambda *a, **k: [])
    monkeypatch.setattr(reflect, "_gather_topic_memories", lambda *a, **k: [])
    monkeypatch.setattr(reflect, "_gather_recent_facts", lambda *a, **k: [])
    base = oc.set_origin(agent="baseline", session_id="s0", turn=1)
    try:
        before = oc.get_origin()
        reflect.run_reflect(user_id="athena", source="test_wp7", save=False)
        after = oc.get_origin()
        assert after == before, f"reflect 跑完未复位 origin：{after} != {before}"
    finally:
        oc.reset_origin(base)


# ── WP-2：plugin 建/结束会话带当前殿 user_id + URL 编码 ────────────
def test_wp2_plugin_session_calls_carry_domain_and_encode():
    """AST 级守卫：限定在 initialize/on_session_end 方法体内检查，不吃别处代码/注释干扰。"""
    src = pathlib.Path(__file__).parent.parent.joinpath(
        "integrations/hermes-plugin/aidumem/__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    bodies = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in ("initialize", "on_session_end"):
            bodies[node.name] = ast.get_source_segment(src, node)
    for name in ("initialize", "on_session_end"):
        assert name in bodies, f"plugin 缺 {name} 方法"
        assert "user_id" in bodies[name], f"{name} 未带当前殿 user_id（反思会落 default 殿）"
        assert "quote" in bodies[name], f"{name} 未对 session_id 做 URL 编码"


# ── WP-6：evolution 端点跨殿脱敏 ──────────────────────────────────
def test_wp6_evolution_redacts_uuid_keeps_fact(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import ducky.routes_knowledge as rk

    class _Cur:
        description = [("id",), ("source_id",), ("target_id",), ("relation_type",),
                       ("confidence",), ("reason",), ("origin_session_id",)]
        def fetchall(self):
            return [(1, "uuid-x", "uuid-y", "replaces", 0.9, "敏感理由内容", "sess_secret")]

    class _Conn:
        def execute(self, *a, **k):
            return _Cur()
        def close(self):
            pass

    monkeypatch.setattr(rk, "get_facts_conn", lambda: _Conn())
    monkeypatch.setattr(rk, "_memory_visible_in_scope", lambda *a, **k: True)
    app = FastAPI()
    rk.register_knowledge_routes(app)
    client = TestClient(app)

    # UUID：可见但敏感字段脱敏，关系结构保留
    r = client.get("/knowledge/uuid-x/evolution", params={"user_id": "athena", "bank_id": "default"})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["redacted"] is True
    assert b["chain"][0]["reason"] == "" and b["chain"][0]["origin_session_id"] == ""
    assert b["chain"][0]["relation_type"] == "replaces"  # 结构不脱敏

    # fact:NNN：拥有本殿事实 → 完整返回
    r2 = client.get("/knowledge/fact:5/evolution", params={"user_id": "athena", "bank_id": "default"})
    b2 = r2.json()
    assert b2["redacted"] is False
    assert b2["chain"][0]["reason"] == "敏感理由内容"
