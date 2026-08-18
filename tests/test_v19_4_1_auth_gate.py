"""
tests/test_v19_4_1_auth_gate.py — v19.4.1 P0-1 鉴权贯通端到端回归

⚠️ 反假绿灯：鉴权必须按「部署形态」矩阵并测，不能只测一种配置。
    v19.4.0 的两个致命组合恰恰都不在任何测试覆盖内：
      · 只设 UI 口令  → API 全裸奔（未登录 GET /api/facts = 200）
      · 只设 API token → 控制台登录后全 401（前端不发 Authorization 头）

同时锁死「存量升级零破坏」：v19.4.0 及之前留下的 .ui_password_hash
（无 source 标记、由服务自动生成）不得凭空启用门禁 ——
否则既有的 hermes 插件 / MCP / cron（全走回环、从不带凭据）会集体 401。

⚠️ 测试隔离纪律（本版踩坑沉淀）
    本文件**不做 sys.modules 清洗式重载**。多个测试文件在 import 期通过
    `utils.FACTS_DB = ...` 重定向数据库，重载会把这些重定向连带抹掉，
    导致同目录下 governance / tombstone / opinion 等文件在全量跑时莫名失败
    （单独跑却是绿的）—— 典型的测试间污染。
    改为：app 只装载一次，部署形态用 monkeypatch 切环境变量与口令文件路径。
    这也要求被测代码在**请求时**读取凭据状态而非模块加载时定格。
"""

import hashlib
import os
import sys
import tempfile

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_tmp_dir = tempfile.mkdtemp(prefix="aidumem_v1941_auth_")
_TEST_FACTS_DB = os.path.join(_tmp_dir, "facts.db")
_TEST_TEXT_DB = os.path.join(_tmp_dir, "text_fts.db")

import ducky.utils as utils  # noqa: E402

utils.FACTS_DB = _TEST_FACTS_DB
utils.TEXT_FTS_DB = _TEST_TEXT_DB

import api_server  # noqa: E402
from ducky.schema_bootstrap import ensure_core_schema  # noqa: E402
from ducky.security import auth as auth_mod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def env(monkeypatch, tmp_path):
    """按需切换部署形态：清空凭据环境变量，口令文件指向独立临时路径。"""
    utils.FACTS_DB = _TEST_FACTS_DB
    utils.TEXT_FTS_DB = _TEST_TEXT_DB
    ensure_core_schema()

    monkeypatch.delenv("AIDUMEM_API_TOKEN", raising=False)
    monkeypatch.delenv("AIDUMEM_UI_PASSWORD", raising=False)

    hash_file = tmp_path / ".ui_password_hash"
    monkeypatch.setattr(auth_mod, "password_hash_path", lambda: str(hash_file))
    auth_mod.revoke_all_sessions()

    class _Env:
        path = str(hash_file)

        @staticmethod
        def client():
            return TestClient(api_server.app)

        @staticmethod
        def set_token(token):
            monkeypatch.setenv("AIDUMEM_API_TOKEN", token)

        @staticmethod
        def set_ui_password(password):
            monkeypatch.setenv("AIDUMEM_UI_PASSWORD", password)

        @staticmethod
        def write_legacy_hash(password):
            """模拟 v19.4.0 留下的旧格式文件（salt:sha256hex，无 source 行）。"""
            salt = "ab" * 16
            digest = hashlib.sha256((salt + password).encode()).hexdigest()
            hash_file.write_text(f"{salt}:{digest}", encoding="utf-8")

    yield _Env
    auth_mod.revoke_all_sessions()


# ═══════════════════════════════════════════════════════════════════
# 形态 A：只设 UI 口令（最自然的部署方式）
# ═══════════════════════════════════════════════════════════════════

def test_ui_password_only_actually_locks_rest_api(env):
    """v19.4.0 这里是 200 —— 部署方以为设了密码，实际全部记忆裸奔"""
    env.set_ui_password("verystrongpassword")
    client = env.client()
    assert client.get("/api/facts").status_code == 401
    assert client.get("/facts").status_code == 401
    assert client.post(
        "/api/facts/add", params={"fact_key": "x", "fact_value": "y"}
    ).status_code == 401


def test_login_issues_httponly_cookie_and_unlocks_console(env):
    """登录后前端不带任何 header 也能用 —— 靠服务端签发的会话 cookie"""
    env.set_ui_password("verystrongpassword")
    client = env.client()
    resp = client.post("/api/login", json={"password": "verystrongpassword"})
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert resp.json()["expires_in"] > 0

    set_cookie = resp.headers.get("set-cookie", "")
    assert auth_mod.SESSION_COOKIE_NAME in set_cookie
    assert "HttpOnly" in set_cookie, "会话 cookie 必须 HttpOnly（防 XSS 窃取）"
    assert "samesite=lax" in set_cookie.lower()

    # 关键断言：不带 Authorization 头也放行（这正是前端的调用方式）
    assert client.get("/api/facts").status_code == 200


def test_logout_revokes_session_server_side(env):
    """登出必须让服务端会话真的失效，而不只是删本地 cookie"""
    env.set_ui_password("verystrongpassword")
    client = env.client()
    client.post("/api/login", json={"password": "verystrongpassword"})
    assert client.get("/api/facts").status_code == 200
    assert client.post("/api/logout").json()["revoked"] is True
    assert client.get("/api/facts").status_code == 401


def test_wrong_password_is_rejected(env):
    env.set_ui_password("verystrongpassword")
    client = env.client()
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/login", json={}).status_code == 401
    assert client.get("/api/facts").status_code == 401


def test_forged_session_cookie_is_rejected(env):
    """伪造 cookie 不得放行（服务端只存会话 token 的哈希）"""
    env.set_ui_password("verystrongpassword")
    client = env.client()
    client.cookies.set(auth_mod.SESSION_COOKIE_NAME, "forged-token-value")
    assert client.get("/api/facts").status_code == 401


# ═══════════════════════════════════════════════════════════════════
# 形态 B：只设 API token
# ═══════════════════════════════════════════════════════════════════

def test_bearer_token_grants_access(env):
    env.set_token("tok-secret")
    client = env.client()
    assert client.get("/api/facts").status_code == 401
    assert client.get(
        "/api/facts", headers={"Authorization": "Bearer tok-secret"}
    ).status_code == 200
    assert client.get(
        "/api/facts", headers={"Authorization": "Bearer wrong"}
    ).status_code == 401


def test_x_api_token_header_also_accepted(env):
    env.set_token("tok-secret")
    client = env.client()
    assert client.get("/api/facts", headers={"X-API-Token": "tok-secret"}).status_code == 200
    assert client.get("/api/facts", headers={"X-API-Token": "nope"}).status_code == 401


# ═══════════════════════════════════════════════════════════════════
# 形态 C：两者都设 —— 一道门禁两把钥匙
# ═══════════════════════════════════════════════════════════════════

def test_cookie_and_bearer_are_interchangeable(env):
    env.set_token("tok-secret")
    env.set_ui_password("verystrongpassword")
    client = env.client()
    assert client.get("/api/facts").status_code == 401
    assert client.get(
        "/api/facts", headers={"Authorization": "Bearer tok-secret"}
    ).status_code == 200
    client.post("/api/login", json={"password": "verystrongpassword"})
    assert client.get("/api/facts").status_code == 200


# ═══════════════════════════════════════════════════════════════════
# 形态 D：存量升级零破坏
# ═══════════════════════════════════════════════════════════════════

def test_autogenerated_password_does_not_enable_gate(env):
    """自动生成的口令只守 UI 登录，不改变既有部署的 API 语义

    v19.2.0 起「未配口令就自动生成」是默认路径，因此几乎所有存量部署都有
    哈希文件。若据此启用门禁，回环调用方会在升级瞬间集体 401。
    """
    auth_mod.write_password_hash(auth_mod.hash_password("autogenerated"), source="auto")
    assert auth_mod.password_source() == "auto"
    assert auth_mod.ui_password_configured() is False
    assert env.client().get("/api/facts").status_code == 200, "存量回环调用被打断（破坏性变更）"


def test_legacy_hash_file_without_source_marker_is_treated_as_auto(env):
    """v19.4.0 留下的旧哈希文件（无 source 行）同样不得凭空启用门禁"""
    env.write_legacy_hash("oldpwd")
    client = env.client()
    assert client.get("/api/facts").status_code == 200, "旧哈希文件不该启用门禁"

    # 旧口令仍可登录，且登录后哈希自动升级为 PBKDF2
    assert client.post("/api/login", json={"password": "oldpwd"}).status_code == 200
    assert auth_mod.read_password_hash().startswith("pbkdf2_sha256$")
    assert auth_mod.password_source() == "auto", "自动升级不得把来源改成 user"


def test_changing_password_enables_gate_and_revokes_sessions(env):
    """改一次密 = 部署方显式意图 → 门禁启用 + 既有会话全部失效"""
    auth_mod.write_password_hash(auth_mod.hash_password("initialpassword"), source="auto")
    client = env.client()
    client.post("/api/login", json={"password": "initialpassword"})

    resp = client.post(
        "/api/config/password",
        json={
            "current_password": "initialpassword",
            "new_password": "brandnewpassword",
            "confirm_password": "brandnewpassword",
        },
    )
    body = resp.json()
    assert body["status"] == "ok"
    assert body["sessions_revoked"] >= 1, "改密必须撤销既有会话"
    assert auth_mod.password_source() == "user"

    # /config/password 会把新口令写进环境变量，这里清掉以验证「哈希文件即门禁」
    os.environ.pop("AIDUMEM_UI_PASSWORD", None)
    assert auth_mod.ui_password_configured() is True

    fresh = env.client()
    assert fresh.get("/api/facts").status_code == 401, "改密后门禁应启用"
    assert fresh.post("/api/login", json={"password": "brandnewpassword"}).status_code == 200
    assert fresh.get("/api/facts").status_code == 200


def test_short_password_rejected(env):
    """口令下限 8 位：4 位口令在本地爆破面前形同虚设"""
    auth_mod.write_password_hash(auth_mod.hash_password("initialpassword"), source="auto")
    resp = env.client().post(
        "/api/config/password",
        json={
            "current_password": "initialpassword",
            "new_password": "1234",
            "confirm_password": "1234",
        },
    )
    assert resp.json()["status"] == "error"


# ═══════════════════════════════════════════════════════════════════
# 公共路径与可观测性
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize(
    "path",
    ["/health", "/api/health", "/docs", "/openapi.json", "/login/hint", "/api/login/hint"],
)
def test_public_paths_never_require_credentials(env, path):
    """健康检查/文档/登录页必须免凭据 —— 否则监控与登录本身都会被锁死"""
    env.set_token("tok-secret")
    env.set_ui_password("verystrongpassword")
    assert env.client().get(path).status_code == 200


def test_login_hint_never_leaks_password(env):
    env.set_ui_password("verystrongpassword")
    assert env.client().get("/api/login/hint").json() == {"hint": None}


def test_health_exposes_auth_gate_state(env):
    """/health 必须让部署方一眼看清有没有门禁，且绝不吐凭据内容"""
    env.set_ui_password("verystrongpassword")
    client = env.client()
    probes = client.get("/health").json()["probes"]
    assert probes["auth_gate_enabled"] is True
    assert probes["auth_ui_password"] in {"user", "auto", "unset"}
    assert "verystrongpassword" not in client.get("/health").text, "/health 泄漏了口令明文"


def test_health_warns_when_gate_disabled(env):
    data = env.client().get("/health").json()
    assert data["probes"]["auth_gate_enabled"] is False
    assert any("auth_gate_disabled" in w for w in data["warnings"])


def test_frontend_sends_credentials():
    """源码级守卫：前端必须带 credentials，否则门禁一开控制台就报废"""
    import pathlib

    api_js = pathlib.Path(_REPO_ROOT, "frontend", "js", "api.js").read_text(encoding="utf-8")
    assert api_js.count("credentials: 'same-origin'") >= 2, "前端 get/post 都必须带 credentials"
    assert "handleAuthFailure" in api_js, "401 必须跳回登录页"

    login_html = pathlib.Path(_REPO_ROOT, "frontend", "login.html").read_text(encoding="utf-8")
    assert "credentials: 'same-origin'" in login_html, "登录请求不带 credentials 则 cookie 不会被保存"


def test_frontend_has_no_external_cdn():
    """P2-3：控制台不得再从第三方 CDN 拉脚本（离线可用 + 供应链完整性）"""
    import pathlib

    index = pathlib.Path(_REPO_ROOT, "frontend", "index.html").read_text(encoding="utf-8")
    assert "cdn.jsdelivr.net/npm/echarts" not in index.replace("此前从 cdn.jsdelivr.net", "")
    assert 'src="js/vendor/echarts.min.js"' in index
    assert pathlib.Path(_REPO_ROOT, "frontend", "js", "vendor", "echarts.min.js").exists()


def test_hermes_plugin_carries_api_token():
    """源码级守卫：hermes 插件必须携带 Bearer，否则门禁一开记忆层静默全挂"""
    import pathlib

    plugin = pathlib.Path(
        _REPO_ROOT, "integrations", "hermes-plugin", "aidumem", "__init__.py"
    ).read_text(encoding="utf-8")
    assert "AIDUMEM_API_TOKEN" in plugin
    assert 'headers["Authorization"] = f"Bearer {self.api_token}"' in plugin
