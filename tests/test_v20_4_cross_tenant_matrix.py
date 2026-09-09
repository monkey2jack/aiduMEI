"""v20.4.0（三方审计 · 总验收门槛 4）：跨租户负向矩阵。

Codex+GPT-5 的系统性诊断：「认证成功没有转化为被认证主体是谁」——
checkpoint / session / jobs / refine / update / tree / entities 七个面
各自为政地吃请求自报字段。本文件是把 README「单机部署内的记忆所有权」
承诺焊死的那张矩阵：租户 A 与租户 B 交叉打每一类资源面，全部必须
404 / error / 空结果；且带一条**区分力负向对照**（把校验关掉，越权
必须成功 —— 证明探针测的是防线，不是路由本身不通）。

矩阵登记表（资源 × 操作 × 期望）在 MATRIX 常量里；新增所有权资源面
必须在此登记 —— test_matrix_covers_all_registered_surfaces 比对防漏。
"""

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

A = {"user_id": "mtx-tenant-a", "bank_id": "default"}
B = {"user_id": "mtx-tenant-b", "bank_id": "default"}
SECRET = "MTX_TENANT_A_SECRET_20260909"

# 资源面登记表：本轮铺轴的七个面，一个都不许缺
MATRIX = [
    "checkpoint", "session", "jobs", "refine_rollback",
    "vector_update", "tree_nodes", "entities",
]


@pytest.fixture()
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDUMEM_LOG_DIR", str(tmp_path / "logs"))

    store: dict[str, dict] = {}

    class _MemStub:
        """替身签名对齐生产（mem0 2.x 面）：get/update/add/search/get_all。"""

        @staticmethod
        def add(content, user_id=None, metadata=None, infer=False, **kw):
            mid = f"stub-{abs(hash(str(content))) % 10**8}"
            store[mid] = {"id": mid, "memory": str(content),
                          "user_id": user_id, "metadata": dict(metadata or {})}
            return {"results": [{"id": mid, "memory": str(content)}]}

        @staticmethod
        def get(memory_id):
            return store.get(memory_id)

        @staticmethod
        def update(memory_id, data=None, metadata=None, **kw):
            rec = store.setdefault(memory_id, {"id": memory_id, "metadata": {}})
            if data is not None:
                rec["memory"] = data
            if metadata:
                rec["metadata"].update(metadata)
            return {"status": "ok"}

        @staticmethod
        def search(query, user_id=None, limit=5, **kw):
            return {"results": []}

        @staticmethod
        def get_all(user_id=None, **kw):
            return {"results": []}

    import ducky.mem0_runtime as mr
    monkeypatch.setattr(mr, "get_memory", lambda: _MemStub(), raising=False)
    import ducky.hot.add as add_mod
    monkeypatch.setattr(add_mod, "get_memory", lambda: _MemStub(), raising=False)
    monkeypatch.setattr(add_mod, "patch_llm_for_speed", lambda mem: None, raising=False)
    import ducky.hot.crud as crud_mod
    monkeypatch.setattr(crud_mod, "get_memory", lambda: _MemStub(), raising=False)
    import ducky.routes_v8 as rv8
    monkeypatch.setattr(rv8, "get_memory", lambda: _MemStub(), raising=False)

    from ducky.hot.add import register_add_routes
    from ducky.hot.crud import register_crud_routes
    from ducky.routes_clotho import register_clotho_routes
    from ducky.routes_v8 import register_v8_routes
    app = FastAPI()
    register_add_routes(app)
    register_crud_routes(app)
    register_clotho_routes(app)
    register_v8_routes(app)
    return TestClient(app), store


def test_matrix_covers_all_registered_surfaces():
    """登记表防漏：本文件必须对 MATRIX 里每个面各有至少一个测试函数。"""
    import inspect
    import sys
    src = inspect.getsource(sys.modules[__name__])
    missing = [m for m in MATRIX if f"test_{m}" not in src]
    assert not missing, f"矩阵登记的资源面缺测试: {missing}"


# ── checkpoint ────────────────────────────────────────────────────────

def test_checkpoint_cross_tenant_read_is_blocked(rig):
    client, _ = rig
    w = client.post("/api/checkpoint", json={
        "session_id": "mtx-a-session", **A,
        "blocks": {"cp_active_intent": SECRET},
    })
    assert w.status_code == 200, w.text[:200]

    # B 的 latest / inject / 指名 get 都取不到 A 的内容（动态审计 🟡-1 实测复现路径）
    latest_b = client.get("/api/checkpoint/latest", params=B).json()
    assert SECRET not in str(latest_b), "B 的 latest 读到了 A 的快照"
    inject_b = client.post("/api/checkpoint/inject", params=B).json()
    assert SECRET not in str(inject_b), "B 的 inject 把 A 的快照注入了自己的上下文"
    get_b = client.get("/api/checkpoint/mtx-a-session", params=B)
    assert get_b.status_code == 404, "B 指名读 A 的 session 快照没被拒"

    # A 自己读得到（防假红：探针不是路由不通）
    latest_a = client.get("/api/checkpoint/latest", params=A).json()
    assert SECRET in str(latest_a), "A 自己都读不到 —— 探针失去区分力"

    # B 的 cleanup 删不到 A 的行
    client.delete("/api/checkpoint/cleanup", params=B)
    still_a = client.get("/api/checkpoint/mtx-a-session", params=A)
    assert still_a.status_code == 200, "B 的 cleanup 把 A 的快照清掉了"


# ── session ──────────────────────────────────────────────────────────

def test_session_ops_require_owner_scope(rig):
    client, _ = rig
    sid = client.post("/session/start", params=A).json()["session_id"]

    for op, req in [
        ("report", lambda: client.get("/session/report", params={"session_id": sid, **B})),
        ("pin", lambda: client.post("/session/pin", params={"session_id": sid, "memory_id": "m1", **B})),
        ("unpin", lambda: client.post("/session/unpin", params={"session_id": sid, "memory_id": "m1", **B})),
        ("end", lambda: client.post("/session/end", params={"session_id": sid, **B})),
    ]:
        body = req().json()
        assert body.get("status") == "error", f"B 的 session {op} 越权成功: {body}"

    # search（POST body 带 B 的 scope）
    sb = client.post("/session/search", params={"session_id": sid},
                     json={"query": "q", **B}).json()
    assert sb.get("status") == "error", f"B 的 session search 越权成功: {sb}"

    # A 自己全部可用，end 之后会话确实没了
    ra = client.get("/session/report", params={"session_id": sid, **A}).json()
    assert ra.get("status") == "ok"
    ea = client.post("/session/end", params={"session_id": sid, **A}).json()
    assert ea.get("status") == "ok"


# ── jobs ─────────────────────────────────────────────────────────────

def test_jobs_scoped_status(rig, monkeypatch):
    client, _ = rig
    r = client.post("/add", json={
        "messages": [{"role": "user", "content": f"async job probe {SECRET}"}],
        **A, "infer": False, "async_mode": True,
    })
    assert r.status_code == 200, r.text[:200]
    job_id = r.json().get("job_id")
    assert job_id

    rb = client.get(f"/add/job/{job_id}", params=B)
    assert rb.status_code == 404, f"B 读到了 A 的 job（含 120 字预览）: {rb.text[:150]}"
    ra = client.get(f"/add/job/{job_id}", params=A)
    assert ra.status_code == 200, ra.text[:200]


# ── refine rollback ──────────────────────────────────────────────────

def test_refine_rollback_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDUMEM_LOG_DIR", str(tmp_path / "logs"))
    import ducky.utils as _u  # v20.4.0：隔离 FACTS_DB 到 tmp，避开 _done 单例跨测试泄漏
    monkeypatch.setattr(_u, "FACTS_DB", str(tmp_path / "facts.db"))
    from ducky.schema_bootstrap import ensure_core_schema
    ensure_core_schema(force=True)  # 自造世界：facts 表必须先在（P2-31 纲领）
    from ducky import refine_memory as rm
    from ducky.utils import get_facts_conn
    monkeypatch.setattr(rm, "_checked", False, raising=False)  # 绕 ensure_refine_schema 的 _checked 单例
    rm.ensure_refine_schema()
    conn = get_facts_conn()
    conn.execute(
        "INSERT INTO refined_memories (user_id, bank_id, category, source_ids, summary, state) "
        "VALUES (?, ?, 'general', '[]', 'mtx summary', 'applied')",
        (A["user_id"], A["bank_id"]))
    conn.commit()
    rid = conn.execute(
        "SELECT refine_id FROM refined_memories WHERE user_id=? ORDER BY refine_id DESC LIMIT 1",
        (A["user_id"],)).fetchone()[0]
    conn.close()

    # B 带自己的 scope 枚举 A 的 refine_id → 拒
    res_b = rm.rollback_refinement(rid, user_id=B["user_id"], bank_id=B["bank_id"])
    assert res_b["status"] == "error", f"B 越权回滚成功: {res_b}"
    # 半个作用域 → 拒（F-08 口径）
    res_half = rm.rollback_refinement(rid, user_id=A["user_id"], bank_id="")
    assert res_half["status"] == "error" and "两轴齐全" in res_half["detail"]
    # A 全 scope → 成
    res_a = rm.rollback_refinement(rid, user_id=A["user_id"], bank_id=A["bank_id"])
    assert res_a["status"] == "ok", res_a


# ── /update 归属先验 ─────────────────────────────────────────────────

def test_vector_update_ownership_precheck(rig):
    client, store = rig
    # A 落一条向量（同步 local 语义绕不过 —— 直接种 store，模拟 A 的存量记忆）
    store["mem-of-a"] = {"id": "mem-of-a", "memory": SECRET,
                         "user_id": A["user_id"], "metadata": {"bank_id": A["bank_id"]}}

    rb = client.post("/update", json={"memory_id": "mem-of-a",
                                      "content": "hijacked-by-b", **B})
    assert rb.status_code == 404, f"B 改写 A 的向量未被拒: {rb.status_code} {rb.text[:150]}"
    assert store["mem-of-a"]["memory"] == SECRET, "拒绝响应下向量仍被改了（响应在说谎）"
    assert store["mem-of-a"]["metadata"]["bank_id"] == A["bank_id"], "B 把 A 的记忆搬进了自己的 bank"

    # 负向对照（区分力）：把先验关掉，同一探针必须能改成功 ——
    # 证明上面拦住它的是归属校验，不是路由/替身本身不通。
    ra = client.post("/update", json={"memory_id": "mem-of-a",
                                      "content": "legit-update-by-a", **A})
    assert ra.status_code == 200, ra.text[:200]
    assert store["mem-of-a"]["memory"] == "legit-update-by-a"


def test_vector_update_negative_control_proves_probe_power(rig, monkeypatch):
    """区分力证明：绕过校验后，跨租户改写这条路是通的。"""
    client, store = rig
    store["mem-of-a2"] = {"id": "mem-of-a2", "memory": SECRET,
                          "user_id": A["user_id"], "metadata": {"bank_id": A["bank_id"]}}
    import ducky.hot.crud as crud_mod
    # 关校验：把 mem.get 替换成恒返回「属于请求方」的形状
    from ducky.mem0_runtime import get_memory  # noqa: F401
    def _blind_get(mid):
        return {"id": mid, "memory": SECRET,
                "user_id": B["user_id"], "metadata": {"bank_id": B["bank_id"]}}
    stub = crud_mod.get_memory()
    monkeypatch.setattr(stub.__class__, "get", staticmethod(_blind_get), raising=False)
    rb = client.post("/update", json={"memory_id": "mem-of-a2",
                                      "content": "bypass-proof", **B})
    assert rb.status_code == 200, (
        f"负向对照失败：关掉校验探针仍打不通（{rb.status_code}），"
        "说明正向测试拦住它的可能不是归属校验 —— 探针无区分力")
    assert store["mem-of-a2"]["memory"] == "bypass-proof"


# ── tree ─────────────────────────────────────────────────────────────

def test_tree_nodes_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDUMEM_LOG_DIR", str(tmp_path / "logs"))
    from ducky.schema_bootstrap import ensure_core_schema
    ensure_core_schema()
    from ducky import tree_memory as tm
    ra = tm.add_tree_node("mtx-secret-project", "/projects",
                          "A 的私有项目", user_id=A["user_id"], bank_id=A["bank_id"])
    assert "error" not in ra, ra
    # B 看不到 A 的节点
    sub_b = tm.get_subtree("/projects", user_id=B["user_id"], bank_id=B["bank_id"])
    assert all("mtx-secret-project" not in n["node_path"] for n in sub_b), (
        "B 的子树里出现了 A 的节点路径")
    # B 建同名路径不撞（(user,bank,path) 逻辑唯一键）
    rb = tm.add_tree_node("mtx-secret-project", "/projects",
                          "B 的同名项目", user_id=B["user_id"], bank_id=B["bank_id"])
    assert "error" not in rb, f"跨租户同名路径仍在撞全局唯一键: {rb}"
    # A 看得到自己的（区分力）
    sub_a = tm.get_subtree("/projects", user_id=A["user_id"], bank_id=A["bank_id"])
    assert any("mtx-secret-project" in n["node_path"] for n in sub_a)


# ── entities ─────────────────────────────────────────────────────────

def test_entities_dedup_is_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDUMEM_LOG_DIR", str(tmp_path / "logs"))
    import ducky.utils as _u  # v20.4.0：隔离 FACTS_DB 到 tmp，避开 _done 单例跨测试泄漏
    monkeypatch.setattr(_u, "FACTS_DB", str(tmp_path / "facts.db"))
    from ducky.schema_bootstrap import ensure_core_schema
    from ducky.utils import get_facts_conn
    ensure_core_schema(force=True)  # 绕 _done 单例，在隔离 db 上真建
    conn = get_facts_conn()
    # 迁移已把 user_id/bank_id 列补上（schema v3）
    from ducky.bank_contract import table_columns
    assert {"user_id", "bank_id"} <= table_columns(conn, "entities"), (
        "entities 表没有租户轴 —— schema v3 迁移没跑")
    from ducky.hot.legacy_helpers import _auto_extract_and_link
    conn.execute("INSERT INTO facts (category, fact_key, fact_value, user_id, bank_id) "
                 "VALUES ('general','mtx:e1','ProjectPhoenix 上线', ?, ?)",
                 (A["user_id"], A["bank_id"]))
    fid_a = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    _auto_extract_and_link(fid_a, '发布 "ProjectPhoenix" 上线', conn,
                           user_id=A["user_id"], bank_id=A["bank_id"])
    conn.execute("INSERT INTO facts (category, fact_key, fact_value, user_id, bank_id) "
                 "VALUES ('general','mtx:e2','ProjectPhoenix 复盘', ?, ?)",
                 (B["user_id"], B["bank_id"]))
    fid_b = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    _auto_extract_and_link(fid_b, '回顾 "ProjectPhoenix" 复盘', conn,
                           user_id=B["user_id"], bank_id=B["bank_id"])
    rows = conn.execute(
        "SELECT user_id FROM entities WHERE name='ProjectPhoenix'").fetchall()
    owners = {r[0] for r in rows}
    conn.close()
    # 英文引号形态必被 _RE_QUOTED 命中（已直测验证）—— 空集就是缺陷，不再 skip
    assert owners >= {A["user_id"], B["user_id"]}, (
        f"同名实体没有按域分行（owners={owners}）—— 仍在全局共享一个节点")
