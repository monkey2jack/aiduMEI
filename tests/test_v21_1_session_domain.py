"""v21.1「众神殿」会话域收口 —— 红→绿对照守卫。

每条断言对应改前的一个真实缺陷（改前必红）：
（f0.6+++：WP-3/WP-5 会话表守卫随 /session/* 路由与 pipeline 会话表一并移除。）
- WP-7 reflect 溯源上下文不复位：改前 `set_origin` 只 set 不 reset（reflect.py:514），
  反思跑完污染后续演化写入的 origin。
- WP-2 plugin 建/结束会话不带当前殿 user_id：改前 URL 只有 session_id，
  反思落 default 殿而非本 bot 的殿。
"""
import ast
import pathlib


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
