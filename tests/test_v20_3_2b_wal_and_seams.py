"""v20.3.2-beta P1-3 / P1-5：WAL 对账不闭环 + MCP×REST 绑定错位。

**P1-3**（Gemini 3.7 Flash 报，本方实测复现）：`reconcile_startup()` 对
delete / delete_all 两支调完级联删除就 `report["recovered"] += 1`，
**从不给原条目 `mark_status(..., "committed")`**。而 `cascade_delete_*` 内部
铸的是**新**的 wal_id、committed 的也是那个新 id。于是原条目永久 pending：
每次重启重放一次、WAL 只增不减、`recovered` 是假账、`/health` 的 WAL 水位失真。

订正 Gemini 的定性：级联删除幂等，所以**不是**「数据损坏死循环」，
而是**账本永不收敛**。定级 P1，但必修 —— 一个报告「已恢复」却没闭合的账本，
比没有账本更坏：它让运维以为对账成功了。

f0.4 补充：以上幂等结论只适用于同一目标集合。旧格式裸 delete_all 无法证明
集合未增长，须保留待人工审查；新协议在持久写入闸门保护下才自动修复。

**P1-5**（Qwen 报，本方实测 body→422 / query→200）：MCP 工具把 `session_id`
发进 JSON body，而 REST 端点是裸标量参数、FastAPI 从 **query** 绑定 → 恒 422。
最难堪的是：**同一个提交** ad3ba6c 修了 `agent_integration_check.py` 的同型缺陷、
漏了 `mcp_server.py`，而结案陈词还把它写成「实机发现」。加完一处漏一处，第 N 次。

守卫为什么没拦住：`test_v20_2_4_mcp_contract.py` 的判据是「键名是否出现在端点
入参名里」—— 而缺陷只存在于**绑定来源**里。**守卫能看见键名，看不见 query/body 之分。**
"""
import pytest

pytest_plugins = ["test_f04_recovery"]


# ══════════════════════════════════════════════════════════
# P1-3 · WAL 启动对账必须闭环
# ══════════════════════════════════════════════════════════

@pytest.fixture()
def wal_env(world):
    # Complete mem0 adapter (get_all/get/delete) instead of object(). Unknown
    # snapshots must remain unknown; a broken test stub cannot imply absence.
    import ducky.wal_engine as we
    return we


def _pending_ids(we):
    return [e.wal_id for e in we.WALEngine.get_instance().get_pending_entries()]


def test_reconcile_closes_the_original_entry(wal_env):
    """**P1-3 靶心**：对账后原条目不许还在 pending 队列里。"""
    we = wal_env
    wal = we.WALEngine.get_instance()
    wal.append(we.WALEntry(
        wal_id="beta-wal-close-1", operation="delete", user_id="u1", bank_id="default",
        payload={"memory_id": "nope-1", "bank_id": "default"}, status="pending"))
    assert "beta-wal-close-1" in _pending_ids(we), "夹具前提破了：条目没进 pending"
    report = we.reconcile_startup()
    assert report["recovered"] == 1 and report["failed"] == 0
    assert report["remaining"] == 0
    assert "beta-wal-close-1" not in _pending_ids(we), (
        "对账报了 recovered 却没闭合原条目 —— 每次重启都会重放，账本永不收敛"
    )


def test_reconcile_is_idempotent_across_restarts(wal_env):
    """第二轮对账必须无事可做（模拟重启两次）。"""
    we = wal_env
    wal = we.WALEngine.get_instance()
    wal.append(we.WALEntry(
        wal_id="beta-wal-close-2", operation="delete_all", user_id="u2", bank_id="default",
        payload={"bank_id": "default"}, status="pending"))
    first = we.reconcile_startup()
    second = we.reconcile_startup()
    assert first["pending_count"] == first["recovered"] == 1
    assert first["remaining"] == first["failed"] == 0
    assert second["recovered"] == second["failed"] == 0
    assert second["pending_count"] == 0, (
        f"第二轮仍发现 {second['pending_count']} 条未决 —— 无限重放形态还在"
    )


def test_recovered_count_is_not_a_lie(wal_env):
    """`recovered` 必须等于**真正闭合**的条数，不是「调过删除的次数」。"""
    we = wal_env
    wal = we.WALEngine.get_instance()
    for i in range(3):
        wal.append(we.WALEntry(
            wal_id=f"beta-wal-count-{i}", operation="delete", user_id="u3",
            bank_id="default", payload={"memory_id": f"x{i}", "bank_id": "default"},
            status="pending"))
    rep = we.reconcile_startup()
    remaining = [w for w in _pending_ids(we) if w.startswith("beta-wal-count-")]
    assert rep["recovered"] == 3 - len(remaining)
    assert rep["failed"] == rep["remaining"] == 0
    assert not remaining, f"报 recovered={rep['recovered']} 但仍有 {remaining} 未闭合"


def test_legacy_bare_delete_all_requires_review_across_restarts(wal_env):
    from ducky import utils
    we = wal_env
    wal = we.WALEngine.get_instance()
    # Serialize the actual historical unprotected format, not wal.append(v2).
    entry = we.WALEntry(wal_id="legacy-broad", operation="delete_all", user_id="u2",
                        bank_id="default", payload={"bank_id": "default"})
    original = entry.to_json() + "\n"
    wal.wal_file.write_text(original)
    conn = utils.get_facts_conn()
    conn.execute("INSERT INTO facts(category,fact_key,fact_value,user_id,bank_id) VALUES(?,?,?,?,?)",
                 ('general', 'new-after-legacy-intent', 'preserve-new-record', 'u2', 'default'))
    conn.commit()
    for _ in range(2):
        report = we.reconcile_startup()
        assert report['pending_count'] == report['remaining'] == report['failed'] == 1
        assert report['recovered'] == 0
        assert report['review_required'] == ['legacy-broad']
        assert _pending_ids(we) == ['legacy-broad']
        assert conn.execute("SELECT fact_value FROM facts WHERE fact_key='new-after-legacy-intent'").fetchone()[0] == 'preserve-new-record'
    conn.close()
    assert wal.wal_file.with_name(wal.wal_file.name + '.pre-f04').read_text() == original


def test_failed_snapshot_is_not_counted_as_recovered(wal_env, monkeypatch):
    from ducky.mem0_runtime import get_memory
    we = wal_env
    wal = we.WALEngine.get_instance()
    wal.append(we.WALEntry(wal_id='snapshot-unknown', operation='delete', user_id='u1',
                           payload={'memory_id': 'x', 'bank_id': 'default'}))
    memory = get_memory()
    original = memory.get_all
    def unavailable(**kwargs):
        raise OSError('injected backend enumeration outage')
    monkeypatch.setattr(memory, 'get_all', unavailable)
    report = we.reconcile_startup()
    assert report['recovered'] == 0 and report['remaining'] == report['failed'] == 1
    assert _pending_ids(we) == ['snapshot-unknown']
    monkeypatch.setattr(memory, 'get_all', original)
    repaired = we.reconcile_startup()
    assert repaired['recovered'] == 1 and repaired['remaining'] == 0
    assert we.reconcile_startup()['recovered'] == 0


def test_unresolvable_entries_stay_visible_for_durable_owner(wal_env):
    """不能自动决议的写入计入 failed，保留原始未决意图供持久写入模块核验。"""
    we = wal_env
    wal = we.WALEngine.get_instance()
    wal.append(we.WALEntry(
        wal_id="beta-wal-unknown", operation="add", user_id="u4", bank_id="default",
        payload={}, status="pending"))
    rep = we.reconcile_startup()
    assert rep["failed"] >= 1
    assert "beta-wal-unknown" in _pending_ids(we)


# ══════════════════════════════════════════════════════════
# P1-5 · MCP×REST 绑定来源
# ══════════════════════════════════════════════════════════

def test_mcp_session_end_reaches_the_endpoint(tmp_path, monkeypatch):
    """**P1-5 靶心**：MCP 的 session_end 不许恒 422。"""
    monkeypatch.setenv("AIDUMEM_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("AIDUMEM_LOG_DIR", str(tmp_path / "l"))
    # 全量跑时前序用例可能留下凭据（本轮 P0-1 的负向对照就设了 token）——
    # 门禁一开，这里拿到的是 401 而不是 422/200，判据就此失真。**先确认世界形态。**
    for _k in ("AIDUMEM_API_TOKEN", "AIDUMEM_UI_PASSWORD"):
        monkeypatch.delenv(_k, raising=False)
    from fastapi.testclient import TestClient
    from api_server import app
    c = TestClient(app, raise_server_exceptions=False)
    body = c.post("/session/end", json={"session_id": "probe"})
    query = c.post("/session/end", params={"session_id": "probe"})
    assert query.status_code == 200, "夹具前提破了：query 形态本该通"
    assert body.status_code == 422, (
        "端点签名变了（现在能收 body）—— 若确已改成模型入参，请同步改本守卫判据"
    )
    # Exercise the actual tool -> HTTP binding -> route -> session store. A
    # route-only query test missed the MCP tool's omitted owner/bank arguments.
    import importlib.util
    import json
    from pathlib import Path
    from types import SimpleNamespace
    import ducky.routes_v8 as routes
    import ducky.pipeline.memory_persistence as persistence
    spec = importlib.util.spec_from_file_location(
        "_scoped_mcp_session", Path(__file__).resolve().parents[1] / "mcp_server.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    monkeypatch.setattr(tool, "httpx", SimpleNamespace(get=c.get, post=c.post))
    monkeypatch.setattr(tool, "API_BASE", "http://testserver")
    monkeypatch.setattr(tool, "_mcp_principal", lambda: "scope-owner")
    reflected = []
    monkeypatch.setattr(routes, "_trigger_session_end_reflect",
                        lambda user, **kw: reflected.append((user, kw["bank_id"])))
    monkeypatch.setattr(persistence, "_sessions", {})
    scope = {"user_id": "scope-owner", "bank_id": "scope-bank"}
    sid = "mcp-scope-lifecycle"
    assert json.loads(tool.session_start(**scope, session_id=sid))["status"] == "ok"
    for wrong in ({**scope, "user_id": "other-owner"}, {**scope, "bank_id": "other-bank"}):
        assert json.loads(tool.session_report(sid, **wrong)).get("status") != "ok"
        assert json.loads(tool.session_end(sid, **wrong)).get("status") != "ok"
        assert json.loads(tool.session_report(sid, **scope))["status"] == "ok"
    assert reflected == []
    assert json.loads(tool.session_end(sid, **scope))["status"] == "ok"
    assert reflected == [("scope-owner", "scope-bank")]
    assert json.loads(tool.session_report(sid, **scope)).get("status") != "ok"


def test_no_mcp_tool_sends_query_params_in_the_body():
    """**元守卫**：按 FastAPI 真实依赖图判绑定来源，不比对键名。

    这条判据由 Qwen 在审计中临时写出并跑通，直接采纳。它看的是
    `route.dependant.query_params` —— 端点**实际从哪里取值**，
    而不是「这个键名在不在入参列表里」（后者放行了 session_end 的错位）。
    """
    import ast
    import pathlib

    from fastapi.routing import APIRoute

    from api_server import app

    query_only: dict[str, set[str]] = {}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        dep = getattr(route, "dependant", None)
        if dep is None:
            continue
        names = {p.name for p in getattr(dep, "query_params", []) or []}
        if names:
            for method in route.methods or ():
                query_only[f"{method} {route.path}"] = names

    src = pathlib.Path(__file__).resolve().parents[1] / "mcp_server.py"
    text = src.read_text(encoding="utf-8")
    tree = ast.parse(text)
    problems = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, "id", "") in {"_api_post", "_api_put", "_api_patch"}):
            continue
        if len(node.args) < 2 or not isinstance(node.args[0], ast.Constant):
            continue
        path = node.args[0].value
        payload = node.args[1]
        if not isinstance(payload, ast.Dict):
            continue
        body_keys = {k.value for k in payload.keys
                     if isinstance(k, ast.Constant) and isinstance(k.value, str)}
        if not body_keys:
            continue
        method = "POST" if getattr(node.func, "id", "") == "_api_post" else "PUT"
        qnames = query_only.get(f"{method} {path}", set())
        misplaced = body_keys & qnames
        if misplaced:
            problems.append(
                f"mcp_server.py 第 {node.lineno} 行：{path} 把 {sorted(misplaced)} "
                f"发进 body，而端点从 query 绑定它们 → 该工具恒 422"
            )
    assert not problems, "MCP×REST 绑定来源错位：\n  " + "\n  ".join(problems)
