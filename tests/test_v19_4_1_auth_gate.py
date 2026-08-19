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
    ["/health", "/api/health", "/login/hint", "/api/login/hint"],
)
def test_always_public_paths_never_require_credentials(env, path):
    """健康检查与登录页必须**永久**免凭据

    登录是拿到凭据的唯一入口，健康检查是监控探针的依赖 ——
    锁死前者等于把自己关在门外，锁死后者会让服务「看起来挂了」。
    """
    env.set_token("tok-secret")
    env.set_ui_password("verystrongpassword")
    assert env.client().get(path).status_code == 200


@pytest.mark.parametrize("path", ["/docs", "/openapi.json", "/redoc"])
def test_doc_paths_are_protected_by_default(env, path, monkeypatch):
    """🟡-B（用户审计）：门禁启用时 API 文档默认一并保护

    这些路径会吐出全部端点清单（含参数与请求体结构），门禁开着却公开它们，
    等于给未授权访问者一份现成的攻击面地图。
    """
    monkeypatch.delenv("AIDUMEM_PUBLIC_DOCS", raising=False)
    env.set_token("tok-secret")
    client = env.client()
    assert client.get(path).status_code == 401, f"{path} 门禁开启时仍公开"
    # 带凭据仍可正常访问（不影响正常排障）
    assert client.get(path, headers={"Authorization": "Bearer tok-secret"}).status_code == 200


@pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
def test_doc_paths_can_be_opened_explicitly(env, path, monkeypatch):
    """需要公开文档的场景（本机开发 / 反代层另有保护）可显式放开"""
    env.set_token("tok-secret")
    monkeypatch.setenv("AIDUMEM_PUBLIC_DOCS", "1")
    assert env.client().get(path).status_code == 200


@pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
def test_doc_paths_open_when_gate_disabled(env, path, monkeypatch):
    """门禁未启用时（本机零配置）文档行为完全不变"""
    monkeypatch.delenv("AIDUMEM_PUBLIC_DOCS", raising=False)
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


# ═══════════════════════════════════════════════════════════════════
# 门禁开启后，所有内部调用方都必须带凭据（否则静默全挂）
# ═══════════════════════════════════════════════════════════════════

def test_all_http_callers_carry_credentials():
    """源码级守卫：凡走 HTTP 调本服务的脚本，都必须携带 Bearer token

    这类脚本由 cron / systemd / Hermes hook 驱动、失败只写日志（甚至不写）。
    门禁开启后若不带凭据，症状是「合并/健康检查/同步/召回悄悄不干活了」——
    没有报警、没人察觉，直到有人去翻日志。因此把「带凭据」变成源码级不变量。

    v19.4.2 扩面：原实现只扫 `scripts/`，而真正的 HTTP 调用方还分布在
    仓库根（mem0_sync.py、seed_*.py）和 integrations/（Hermes hook）。
    守卫射程小于缺陷分布 = 假绿灯：v19.4.1 就是带着全绿测试发布了
    「hook 每次 401、同步停摆 8 天」的版本。射程必须覆盖全部入口点。
    """
    import pathlib
    import re

    offenders = []

    # 覆盖集合 = scripts/ + 仓库根（排除服务端自身），见 _iter_credential_consumers
    for path in _iter_credential_consumers():
        text = path.read_text(encoding="utf-8")
        if _is_standalone_integration(path):
            if not _has_env_fallback_chain(text):
                offenders.append(f"{path.name}: 独立集成件的凭据兜底链不完整")
            continue
        if "_auth_headers" not in text:
            offenders.append(f"{path.name}: 未定义 _auth_headers")
            continue
        # 每个对本服务的 requests/urlopen 调用都应带上 headers
        for m in re.finditer(r"requests\.(get|post)\((?P<args>[^;]*?)\)\s*$", text, re.M):
            if "_auth_headers" not in m.group("args") and "Authorization" not in m.group("args"):
                offenders.append(f"{path.name}:{text[:m.start()].count(chr(10)) + 1} 调用未带凭据")

    for path in _iter_credential_consumer_shells():
        text = path.read_text(encoding="utf-8")
        if not re.search(_TARGETS_THIS_SERVICE, text):
            continue
        if "curl" in text and "AUTH_ARGS" not in text:
            offenders.append(f"{path.name}: curl 未带 AUTH_ARGS")
        # 走 python3 -c 发请求的 shell hook：必须构造 Authorization 头
        if "urllib.request" in text and "Authorization" not in text:
            offenders.append(f"{path.name}: urllib 调用未带 Authorization")

    assert not offenders, "门禁开启后这些调用方会静默 401: " + "; ".join(offenders)


def test_consolidator_logs_auth_failure_loudly():
    """401/403 必须显式记 error —— 静默 return {} 会让配置错误长期潜伏"""
    import pathlib

    text = pathlib.Path(_REPO_ROOT, "scripts", "consolidator.py").read_text(encoding="utf-8")
    assert "AIDUMEM_API_TOKEN" in text
    assert text.count("_auth_headers()") >= 2, "GET 与 POST 都要带凭据"
    assert "401" in text and "logger.error" in text, "鉴权失败必须响亮报错"


# ═══════════════════════════════════════════════════════════════════
# cron 场景：.env 兜底加载（门禁开启后的定时任务不能静默 401）
# ═══════════════════════════════════════════════════════════════════

def test_load_env_file_injects_without_override(tmp_path, monkeypatch):
    """.env 兜底加载：补空缺、不覆盖已显式设置的变量"""
    from ducky.utils import load_env_file

    env_file = tmp_path / ".env"
    env_file.write_text(
        "# 注释行\n"
        "\n"
        'AIDUMEM_API_TOKEN="token-from-file"\n'
        "AIDUMEM_SOME_OTHER=plain-value\n"
        "MALFORMED_LINE_NO_EQUALS\n",
        encoding="utf-8",
    )

    monkeypatch.delenv("AIDUMEM_API_TOKEN", raising=False)
    monkeypatch.setenv("AIDUMEM_SOME_OTHER", "already-set")

    injected = load_env_file(str(env_file))
    assert injected == 1, "只应注入缺失的那一个"
    assert os.environ["AIDUMEM_API_TOKEN"] == "token-from-file", "引号应被去掉"
    assert os.environ["AIDUMEM_SOME_OTHER"] == "already-set", "已存在的变量不得被覆盖"

    monkeypatch.delenv("AIDUMEM_API_TOKEN", raising=False)
    monkeypatch.delenv("AIDUMEM_SOME_OTHER", raising=False)


def test_load_env_file_is_silent_when_missing(tmp_path):
    """文件不存在时静默返回 0，绝不影响调用方"""
    from ducky.utils import load_env_file

    assert load_env_file(str(tmp_path / "nope.env")) == 0


def test_api_auth_headers_falls_back_to_env_file(tmp_path, monkeypatch):
    """cron 场景核心断言：环境变量为空时，凭据必须能从 .env 兜底读到

    这是 v19.4.1 部署体检发现的定时炸弹：服务进程靠 systemd EnvironmentFile
    拿到 token，但 **cron 不加载 .env**，其环境几乎是空的。门禁一开启，
    consolidator（每日 2:30）等定时任务会在下一次触发时集体 401 ——
    而它们失败只写日志，没有人被通知。
    """
    from ducky.utils import api_auth_headers

    env_file = tmp_path / ".env"
    env_file.write_text("AIDUMEM_API_TOKEN=cron-token\n", encoding="utf-8")

    monkeypatch.delenv("AIDUMEM_API_TOKEN", raising=False)
    monkeypatch.setenv("AIDUMEM_ENV_FILE", str(env_file))

    headers = api_auth_headers()
    assert headers == {"Authorization": "Bearer cron-token"}


def test_api_auth_headers_empty_when_no_token(tmp_path, monkeypatch):
    """未配置 token 时返回空 dict —— 行为与门禁未启用时完全一致"""
    from ducky.utils import api_auth_headers

    monkeypatch.delenv("AIDUMEM_API_TOKEN", raising=False)
    monkeypatch.setenv("AIDUMEM_ENV_FILE", str(tmp_path / "absent.env"))
    assert api_auth_headers() == {}


# 「指向本服务」的信号集合 —— 守卫用它判断一个文件是不是调用方。
#
# ⚠️ 这个正则**就是守卫的射程**，漏一个变量名 = 漏掉一整类调用方。
#    v19.4.2 首版只登记了 8767 / AIDUMEM_API_BASE / AIDUMEM_URL 三个信号，
#    于是 frontend/dev_server.py 用第四个名字 AIDUMEM_UPSTREAM + 第二个端口 8777，
#    从守卫眼皮底下整个溜了过去 —— 它是双重逃逸：既待在被跳过的目录里，
#    用的又是没被登记的变量名。今后新增指向本服务的环境变量必须同时登记到这里。
_TARGETS_THIS_SERVICE = r"8767|8777|AIDUMEM_API_BASE|AIDUMEM_URL|AIDUMEM_UPSTREAM"


def _iter_credential_consumers():
    """全仓「以客户端身份调用本服务 REST 接口」的 Python 文件。

    v19.4.2 扩面：`scripts/` + 仓库根 + `integrations/` + `frontend/`（含子目录）
    + `tests/` 下的运维件（integration_* / perf_* / smoke_*，非单元测试）。
    排除 api_server.py —— 它是服务端本身（门禁的实施者，不是通过门禁的人），
    只是因为源码里写了默认端口 8767 才被正则捞到。

    ⚠️ 改这个函数 = 改守卫的射程。tests/test_v19_4_2_auth_coverage.py 里有一条
    元测试会拿全仓实际的 HTTP 调用方来核对本函数的返回集合，缩小射程会立刻变红。
    """
    import pathlib
    import re

    files = (
        sorted(pathlib.Path(_REPO_ROOT, "scripts").glob("*.py"))
        + sorted(pathlib.Path(_REPO_ROOT).glob("*.py"))
        + sorted(pathlib.Path(_REPO_ROOT, "integrations").rglob("*.py"))
        + sorted(pathlib.Path(_REPO_ROOT, "frontend").rglob("*.py"))
        # tests/ 下的运维件（integration_* / perf_*）打的是真实服务，不是 TestClient，
        # 门禁一开同样 401。单元测试不在此列 —— 它们压根不经过门禁。
        + sorted(pathlib.Path(_REPO_ROOT, "tests").glob("integration_*.py"))
        + sorted(pathlib.Path(_REPO_ROOT, "tests").glob("perf_*.py"))
        + sorted(pathlib.Path(_REPO_ROOT, "tests").glob("smoke_*.py"))
    )
    out = []
    for path in files:
        if path.name == "api_server.py":      # 服务端，不是调用方
            continue
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(_TARGETS_THIS_SERVICE, text):
            out.append(path)
    return out


def _is_standalone_integration(path) -> bool:
    """判断是否为「会被拷出仓库独立运行」的集成件（integrations/ 下）。

    这类文件跑在宿主 Agent / 编辑器进程里，仓库代码不在它的 sys.path 上，
    所以不能要求它 import ducky —— 但凭据行为必须与仓库一致。
    """
    return "integrations" in path.parts


def _has_env_fallback_chain(text: str) -> bool:
    """独立集成件的凭据实现，必须覆盖与 ducky.utils 相同的兜底链。

    只带 Authorization 头是不够的：v19.4.1 的 Hermes 插件正是「代码里写了
    Bearer，但 token 从空环境里读出来是空字符串」—— 看着修了，实际全线 401。
    因此这里同时要求「取值链」存在，而不只是「有那个头」。
    """
    if "Authorization" not in text:
        return False
    if "AIDUMEM_API_TOKEN" not in text:
        return False
    return "AIDUMEM_ENV_FILE" in text and ".aidumem/.env" in text


def _iter_credential_consumer_shells():
    """同上，shell 侧：`scripts/` + `integrations/`（含子目录，如 cursor-hook/）。"""
    import pathlib
    import re

    files = (
        sorted(pathlib.Path(_REPO_ROOT, "scripts").glob("*.sh"))
        + sorted(pathlib.Path(_REPO_ROOT, "integrations").rglob("*.sh"))
    )
    return [p for p in files
            if re.search(_TARGETS_THIS_SERVICE, p.read_text(encoding="utf-8"))]


def test_scripts_share_single_credential_source():
    """源码守卫：脚本不得各自实现凭据读取，必须复用 utils.api_auth_headers

    各脚本自行 os.environ.get 会导致「有的带 .env 兜底、有的没有」，
    门禁开启后表现为部分任务莫名 401 —— 排查成本极高。
    """
    import pathlib
    import re

    offenders = []
    for path in _iter_credential_consumers():
        text = path.read_text(encoding="utf-8")
        if _is_standalone_integration(path):
            # integrations/ 下的文件会被拷进宿主 Agent / 编辑器配置独立运行，
            # 那里 import 不到 ducky。强行要求复用会换来一个「装上就崩」的插件。
            # 因此对它们放宽为：必须实现同一条 .env 兜底链（见 _has_env_fallback_chain）。
            assert _has_env_fallback_chain(text), (
                f"{path.name} 是独立集成件，允许自带凭据实现，"
                f"但必须实现与 ducky.utils 相同的 .env 兜底链（环境变量 → AIDUMEM_ENV_FILE → ~/.aidumem/.env）"
            )
            continue
        assert "api_auth_headers" in text, f"{path.name} 未复用统一凭据入口"
        # 不应再有本地定义
        if re.search(r"^def _auth_headers\(", text, re.M):
            offenders.append(path.name)
    assert not offenders, "以下脚本仍自带 _auth_headers 实现: " + ", ".join(offenders)


def test_scripts_add_repo_root_to_syspath():
    """cron / systemd / MCP 的 cwd 都不是仓库根：调本服务的脚本必须显式补 sys.path，
    否则 `import ducky` 直接 ImportError —— 而 systemd 下的表现是「进程起来就退」，
    unit 若没有 StartLimit* 还会伪装成 activating，谁都看不见。
    """
    offenders = []
    for path in _iter_credential_consumers():
        if _is_standalone_integration(path):
            continue      # 独立集成件不 import ducky，见 _is_standalone_integration
        text = path.read_text(encoding="utf-8")
        if "api_auth_headers" not in text:
            continue
        if "sys.path.insert" not in text:
            offenders.append(str(path.relative_to(_REPO_ROOT)))
    assert not offenders, "以下脚本缺 sys.path 修正，cron 下会 ImportError: " + ", ".join(offenders)
