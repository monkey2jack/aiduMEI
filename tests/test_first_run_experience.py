"""零配置首跑体验 —— **评委第一印象就在这几个响应里**（参赛前自查 WP-C）。

这组用例的由来：拿干净克隆按 README 走一遍，不填任何 key，第一个动作
（`POST /add` 写一条记忆）换来的是 HTTP 500 加一句 httpx 内部错误。
那句话是真的，但对拿到项目的人**没有用**。

判据一律落在**响应形状与可操作性**上，不落在措辞上 —— 措辞会改，
「不许把内部异常原文当成唯一内容」这条契约不该跟着改。
"""

import ast
import pathlib
import re

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent


# ── N-1：依赖未就绪时说人话 ───────────────────────────────────────────

@pytest.mark.parametrize("raw,expect_cause", [
    ("Using SOCKS proxy, but the 'socksio' package is not installed", "代理"),
    ("Incorrect API key provided: sk-***", "凭据"),
    ("Connection refused", "不可达"),
    ("some totally unknown failure", "初始化"),
])
def test_dependency_not_ready_message_is_actionable(raw, expect_cause):
    """四类失败都要给出**缺什么**，而不是只回抛内部异常。"""
    from ducky.mem0_runtime import _mem0_unavailable_detail

    msg = _mem0_unavailable_detail(Exception(raw))
    assert expect_cause in msg, f"没说清病因（期望提到「{expect_cause}」）：{msg[:120]}"
    # 三件事缺一不可：去哪配 / 怎么查 / 不配怎么办
    assert "mem0_config_local.json" in msg, "没说去哪配"
    assert "/health" in msg, "没说怎么查还缺什么"
    assert "/add/raw" in msg, (
        "没告诉他「不配 key 也有一条路能走」—— 那是这个项目零凭据下"
        "唯一还能用的入口，第一印象全靠它"
    )
    assert raw[:20] in msg, "原始错误必须保留（运维要靠它定位），只是不该是唯一内容"


def test_dependency_not_ready_is_503_not_500():
    """依赖未就绪是 **503**，不是 500。

    500 会让调用方以为撞上了 bug 去提 issue；503 才是「先去把依赖配好」。
    源码级判据：那一处 raise 必须是 503，且不能再把裸异常拼进 detail。
    """
    src = (_ROOT / "ducky" / "mem0_runtime.py").read_text(encoding="utf-8")
    assert 'raise HTTPException(503, _mem0_unavailable_detail(e))' in src, (
        "mem0 初始化失败的出口变了 —— 要么状态码退回 500，要么绕开了翻译函数"
    )
    assert 'HTTPException(500, f"mem0 不可用: {e}")' not in src, (
        "裸异常回抛的老写法又回来了"
    )


# ── N-2：degraded 与 degraded_details 必须同源 ─────────────────────────

def test_degraded_details_explains_every_degraded_entry():
    """`degraded` 里的每一项，`degraded_details` 都必须有一条对应。

    零配置首跑实测到的形态是：`degraded=['vector_backend','entity_keywords']`
    而 `degraded_details=None` —— **明细通道恰好在最需要它的时候是空的**。
    """
    from ducky.hot.health import _reconcile_degraded_details

    degraded = ["vector_backend", "entity_keywords", "mystery"]
    probes = {"vector_backend_error": "no credentials configured"}
    out = _reconcile_degraded_details(degraded, probes)

    assert {d["component"] for d in out} == set(degraded), (
        f"details 覆盖的组件与 degraded 对不上：{[d['component'] for d in out]}"
    )
    by = {d["component"]: d for d in out}
    assert by["vector_backend"]["reason"] == "no credentials configured"
    assert by["vector_backend"]["source"] == "probe_error"
    # 没有理由的那一条也必须说出「没有理由」，而不是留空让人猜
    assert by["mystery"]["reason"], "没有理由的降级也要有一句话，不能是空的"
    assert by["mystery"]["source"] == "probe_no_reason"


def test_degraded_details_survives_a_broken_tracker(monkeypatch):
    """明细来源坏掉时，/health 不许被带崩 —— 降级信息本身不该是新的故障源。"""
    from ducky.hot import health as H

    class _Boom:
        @staticmethod
        def get_degraded_details():
            raise RuntimeError("tracker 挂了（模拟）")

    monkeypatch.setattr(H, "DegradationTracker", _Boom)
    out = H._reconcile_degraded_details(["vector_backend"], {})
    assert [d["component"] for d in out] == ["vector_backend"], (
        "追踪器抛异常时应当回落到探针理由，而不是整个 /health 崩掉"
    )


# ── 首跑契约的元守卫 ─────────────────────────────────────────────────

def test_no_route_hands_a_bare_exception_to_the_caller():
    """**元守卫**：不许再有 `HTTPException(5xx, f"...{e}")` 这种把裸异常当正文的写法。

    这条抓的是「下一个同类」：N-1 不是孤例，它是一类写法。
    判据用 AST，不用 grep —— 注释里写着这种字样的地方不该被算进来
    （本仓在 v20.2.5 已经为「grep 分不清代码和注释」付过一次学费）。

    豁免：`detail` 里除了异常还给了可操作信息的（长度超过阈值、或包含路径
    /端点提示）不算 —— 判据要认的是「**只有**裸异常」。
    """
    offenders = []
    for path in sorted((_ROOT / "ducky").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name != "HTTPException" or len(node.args) < 2:
                continue
            code = node.args[0]
            if not (isinstance(code, ast.Constant) and isinstance(code.value, int)
                    and 500 <= code.value < 600):
                continue
            detail = node.args[1]
            text = ast.unparse(detail)
            # 只有裸异常（f"{e}" / str(e) / e 本身），没有任何指引
            bare = re.fullmatch(r"""f?['"]?\{?(str\()?e(xc)?\)?\}?['"]?""", text.strip()) \
                or re.fullmatch(r"""f['"][^'"]{0,24}\{(str\()?e(xc)?\)?\}['"]""", text.strip())
            if bare:
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        f"这些 5xx 出口把裸异常当成了全部正文：{offenders} —— "
        "调用方拿到的是内部实现细节，不是「他该做什么」。"
        "至少要说清缺什么、去哪配、有没有替代路径（见 N-1）。"
    )

def test_degraded_details_prefers_existing_detail_over_no_reason():
    """v20.3 外审：`_detail` 与 `_source` 也是探针留下的真理由。

    只认 `_error` 会让零配置首启在同一个 payload 里同时放着答案和“没有答案”。
    """
    from ducky.hot.health import _reconcile_degraded_details
    probes = {
        "vector_backend_detail": "mem0 vector singleton is not ready; not probed",
        "entity_keywords_source": "unset",
    }
    out = _reconcile_degraded_details(["vector_backend", "entity_keywords"], probes)
    by = {d["component"]: d for d in out}
    assert by["vector_backend"]["reason"] == "mem0 vector singleton is not ready; not probed"
    assert by["vector_backend"]["source"] == "probe_detail"
    assert by["entity_keywords"]["reason"] == "unset"
    assert by["entity_keywords"]["source"] == "probe_source"

def test_unprobed_vector_backend_is_warming_up_not_degraded():
    """Cold start is not an incident.

    `probed=false` means "not inspected yet"; treating it as failure makes the
    first healthy `/health` response say `degraded`, training operators to ignore alerts.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ducky.hot import health as H

    app = FastAPI()
    H.register_health_routes(app)
    # This route performs many real filesystem probes. The invariant under test
    # is vector-backend cold start, not the entire local environment, so inspect
    # the aggregate lists rather than requiring every unrelated probe to pass.
    with TestClient(app) as client:
        data = client.get("/health").json()
    assert "vector_backend" not in data["degraded"]
    if data.get("warming_up"):
        assert "vector_backend" in data["warming_up"]

# ══════════════════════════════════════════════════════════════════
# v20.3 外审 C-01：persona AI self must follow the two-dimensional scope
# ══════════════════════════════════════════════════════════════════

def _persona_scope_db(tmp_path):
    """Use the production facts DDL and production bank migration, not a hand-written schema."""
    import sqlite3
    from ducky.schema_bootstrap import _FACTS_DDL
    from ducky.bank_contract import ensure_memory_banks_schema
    path = tmp_path / "facts.db"
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_FACTS_DDL)
    ensure_memory_banks_schema(conn)
    conn.executemany(
        "INSERT INTO facts (category,fact_key,fact_value,peer,user_id,bank_id) "
        "VALUES ('self',?,?, 'ai', ?, ?)",
        [
            ("alice-key", "alice-value", "alice", "bank_a"),
            ("bob-key", "bob-value", "bob", "bank_a"),
        ],
    )
    conn.commit()
    return path, conn

def test_persona_ai_self_read_is_scope_filtered(tmp_path, monkeypatch):
    import ducky.extended.routes as routes
    path, _ = _persona_scope_db(tmp_path)
    import sqlite3
    # Route closes the connection it receives; provide a fresh connection per request.
    connections = []
    def _conn():
        c = sqlite3.connect(path, check_same_thread=False)
        c.row_factory = sqlite3.Row
        connections.append(c)
        return c
    monkeypatch.setattr(routes, "_gfc", _conn, raising=False)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    routes.register_extended_routes(
        app,
        _get_memory_fn=lambda: None,
        _get_db_fn=lambda: conn,
        _extract_entities_fn=lambda text: [],
    )
    client = TestClient(app)
    got = client.get("/persona/ai-self", params={"user_id": "alice", "bank_id": "bank_a"}).json()
    assert got["total_facts"] == 1
    assert got["traits"]["self"][0]["key"] == "alice-key"
    other = client.get("/persona/ai-self", params={"user_id": "bob", "bank_id": "bank_a"}).json()
    assert other["total_facts"] == 1
    assert other["traits"]["self"][0]["key"] == "bob-key"
    for c in connections:
        c.close()

# ══════════════════════════════════════════════════════════════════
# v20.3 外审 C-02/C-03：MCP SSE 与 code graph 安全边界
# ══════════════════════════════════════════════════════════════════

def test_mcp_sse_nonloopback_without_credentials_refuses_to_run(monkeypatch):
    mcp_server = pytest.importorskip("mcp_server")
    monkeypatch.delenv("AIDUMEM_API_TOKEN", raising=False)
    monkeypatch.delenv("AIDUMEM_ALLOW_INSECURE_PUBLIC", raising=False)
    monkeypatch.setattr(mcp_server, "api_auth_headers", lambda: {})
    assert mcp_server._sse_authorization_allowed() is False
    # The route is in `__main__`; keep the guard as a callable contract and
    # assert the policy helper rather than spawning uvicorn in tests.

def test_mcp_sse_nonloopback_with_credentials_is_allowed(monkeypatch):
    mcp_server = pytest.importorskip("mcp_server")
    monkeypatch.setenv("AIDUMEM_API_TOKEN", "test-token")
    monkeypatch.setattr(mcp_server, "api_auth_headers", lambda: {
        "Authorization": "Bearer test-token"
    })
    assert mcp_server._sse_authorization_allowed() is True

def test_code_graph_rejects_outside_workspace(monkeypatch):
    import ducky.code_graph as cg
    monkeypatch.setattr(cg, "BASE_DIR", "/tmp/aidumei-workspace", raising=False)
    with pytest.raises(Exception):
        cg._workspace_root("/etc")

def test_code_graph_max_files_has_upper_bound():
    import ducky.code_graph as cg
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        cg.ImpactRequest(changed_files=["x"], max_files=2001)

def test_cron_entry_quotes_all_variable_paths():
    text = (_ROOT / "scripts" / "update_crontab.sh").read_text(encoding="utf-8")
    assert 'cd "${REPO_ROOT}"' in text
    assert '"${PY}" scripts/consolidator.py' in text
    assert '>> "${LOG_DIR}/consolidator.log"' in text

def test_frontend_does_not_remask_raw_keys():
    """Server must be the only masking layer; the browser never holds raw keys."""
    text = (_ROOT / "frontend" / "js" / "panels.js").read_text(encoding="utf-8")
    assert "key.slice" not in text
    assert "m.api_key || m.config && m.config.api_key" in text

def test_frontend_github_update_check_is_opt_in():
    """Offline/self-hosted deployments must not leak client IP/version by default."""
    text = (_ROOT / "frontend" / "js" / "main.js").read_text(encoding="utf-8")
    assert "localStorage.getItem('aidumei.updateCheck') === '1'" in text
    assert "if (ENABLE_UPDATE_CHECK) checkLatestVersion()" in text

def test_ui_package_excludes_local_dev_proxy():
    """The console directory must not contain a proxy with intentionally open write endpoints."""
    assert not (_ROOT / "frontend" / "dev_server.py").exists()
    assert (_ROOT / "scripts" / "dev_server.py").exists()

def test_wheel_package_declaration_excludes_local_dev_proxy():
    """The source layout must keep the local proxy outside the UI directory."""
    import tomllib
    assert not (_ROOT / "frontend" / "dev_server.py").exists()
    assert (_ROOT / "scripts" / "dev_server.py").exists()
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = sum(data["tool"]["setuptools"]["package-data"].values(), [])
    assert "../frontend/**/*" in patterns
    assert all("dev_server.py" not in pattern for pattern in patterns)

def test_unauthenticated_health_uses_strict_allowlist(monkeypatch):
    """Public /health is a probe, not a reconnaissance report."""
    import os
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ducky.hot import health as H
    app = FastAPI()
    H.register_health_routes(app)
    client = TestClient(app)
    monkeypatch.setenv("AIDUMEM_API_TOKEN", "secret")
    response = client.get("/health")
    assert response.status_code == 200
    assert set(response.json()) == {
        "status", "version", "health_status", "degraded", "warming_up"
    }

def test_authenticated_health_returns_full_diagnostics(monkeypatch):
    import os
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ducky.hot import health as H
    app = FastAPI()
    H.register_health_routes(app)
    client = TestClient(app)
    token = "secret"
    monkeypatch.setenv("AIDUMEM_API_TOKEN", token)
    response = client.get("/health", headers={"Authorization": f"Bearer {token}"})
    data = response.json()
    assert "probes" in data
    assert "degraded_details" in data
