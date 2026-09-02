"""v20.3.2-beta P2 批次：外审若干条低风险高价值整改。

P2-11（WorkBuddy M-2）改密后明文口令写进 os.environ
P2-13（GLM F-4 / DeepSeek）HTTP 安全响应头全缺
P2-8 （Gemini 3.7）ConflictResolver 生产规则残留脱敏占位符且逃生阀未接线
P2-9 （Gemini 3.7）calc_bm25_score 不是 BM25
P2-15（DeepSeek P0-3 / MiniMax）多 worker 无守卫
"""
import pytest


# ══════════════ P2-11 · 明文口令不进进程环境 ══════════════

def test_password_change_does_not_put_plaintext_into_environ():
    """改密后不许把明文写进 os.environ。

    外审 M-2：`os.environ["AIDUMEM_UI_PASSWORD"] = new` 让明文常驻
    `/proc/<pid>/environ`，并被**所有子进程**（mem0、ffmpeg…）继承。
    上一行已经写了哈希文件，这一行**既多余又有害**。
    """
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "ducky" / "routes_config.py"
    text = src.read_text(encoding="utf-8")
    offenders = [ln for i, ln in enumerate(text.splitlines(), 1)
                 if "os.environ[" in ln and "UI_PASSWORD" in ln and "=" in ln
                 and not ln.strip().startswith("#")]
    assert not offenders, (
        "改密路径又把明文口令写进环境变量：\n  " + "\n  ".join(offenders))


# ══════════════ P2-13 · 安全响应头 ══════════════

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDUMEM_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("AIDUMEM_LOG_DIR", str(tmp_path / "l"))
    for k in ("AIDUMEM_API_TOKEN", "AIDUMEM_UI_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    from fastapi.testclient import TestClient
    from api_server import app
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("header,expected", [
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", None),
    ("Content-Security-Policy", None),
])
def test_security_headers_are_present(client, header, expected):
    """自带 Web 控制台的自托管服务必须给浏览器侧兜底（约 15 行中间件）。"""
    r = client.get("/health")
    assert header in r.headers, f"缺少安全响应头 {header}"
    if expected:
        assert r.headers[header] == expected


def test_csp_allows_the_console_to_work(client):
    """CSP 不许把自家控制台打死：现状有 inline style，必须显式容纳。"""
    csp = client.get("/health").headers.get("Content-Security-Policy", "")
    assert "default-src" in csp
    assert "'self'" in csp
    assert "style-src" in csp and "unsafe-inline" in csp, (
        f"CSP 未容纳现状 inline style，前端会白屏：{csp}")


# ══════════════ P2-8 · 消解规则不留占位符 ══════════════

def test_conflict_rules_carry_no_desensitization_placeholders():
    """默认互斥规则表里不许有 `*_placeholder` —— 它永不匹配任何真实文本。

    外审：脱敏时只做了字符串替换，于是 4 条规则里 2 条成了死代码；
    而逃生阀 `load_custom_exclusion_patterns()` 存在却**全仓无人调用**
    （CHANGELOG 写着「供 api_server 启动时配置」）—— 「定义了不接线」同型病。
    """
    from ducky.conflict_resolver import MUTUAL_EXCLUSION_PATTERNS
    dead = [p for p in MUTUAL_EXCLUSION_PATTERNS
            if any("placeholder" in str(x).lower() for x in p)]
    assert not dead, f"默认规则表仍带脱敏占位符（永不匹配）：{dead}"
    assert MUTUAL_EXCLUSION_PATTERNS, "规则表被清空了 —— 状态开关那两条是有效的，不许一起删"


def test_custom_exclusion_injection_is_wired_and_works():
    """逃生阀必须可用：注入后规则真的生效（不是「定义了不接线」）。"""
    import ducky.conflict_resolver as cr
    before = list(cr.MUTUAL_EXCLUSION_PATTERNS)
    try:
        cr.load_custom_exclusion_patterns([(r"(域名|url)", r"old\.invalid", r"new\.invalid")])
        assert any("old\\.invalid" in str(p[1]) for p in cr.MUTUAL_EXCLUSION_PATTERNS), \
            "注入的规则没进表"
    finally:
        cr.MUTUAL_EXCLUSION_PATTERNS = before


# ══════════════ P2-9 · 算法命名与实现一致 ══════════════

def test_token_overlap_score_is_not_called_bm25():
    """名不副实的算法名必须改掉。

    外审：`calc_bm25_score` 只算查询词元在文本中的**覆盖率**——
    无 IDF、无 TF 饱和、无文档长度归一，三个 BM25 的灵魂一个都没有。
    它在融合分里占 0.25 权重，且对外文档大张旗鼓叫「BM25 词频」。
    诚实的名字是 token overlap；要真 BM25 就接 FTS5 的 bm25()。
    """
    from ducky import scoring
    assert hasattr(scoring, "calc_token_overlap_score"), "未提供诚实命名的函数"
    assert scoring.calc_token_overlap_score("网关 端口", "网关端口是多少") > 0
    # 兼容别名可以留（存量调用方），但不许再声称是 BM25
    import inspect
    src = inspect.getsource(scoring)
    assert "def calc_bm25_score" not in src or "别名" in src or "alias" in src.lower(), (
        "calc_bm25_score 仍是主实现名 —— 名不副实")


def test_no_public_surface_calls_it_bm25_without_qualification():
    """对外文档不许再把它说成 BM25 而不加限定。"""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    for name in ("README.md", "README_EN.md"):
        text = (root / name).read_text(encoding="utf-8")
        for line in text.splitlines():
            if "BM25" not in line:
                continue
            low = line.lower()
            assert ("fts5" in low or "覆盖率" in line or "overlap" in low
                    or "trigram" in low or "词频" in line), (
                f"{name} 里这行把打分说成 BM25 而未限定：{line.strip()[:110]}")


# ══════════════ P2-15 · 多 worker 守卫 ══════════════

def test_multi_worker_is_refused_at_startup(monkeypatch):
    """单进程契约必须由机器守住，不能只写在注释里。

    外审：会话表 / 限流窗口 / 用量计数全是**进程内**状态，而 docstring
    把 `gunicorn -k uvicorn.workers.UvicornWorker` 列为起法之一。
    任何人加 `--workers 4` 都会得到「登录时好时坏」这类最难排查的症状，
    且**没有任何警告**。判据落在启动期，与既有公网熔断同一风格。
    """
    import api_server as A
    assert hasattr(A, "_enforce_single_process_policy"), "没有多进程守卫"
    monkeypatch.setenv("WEB_CONCURRENCY", "4")
    with pytest.raises(RuntimeError) as got:
        A._enforce_single_process_policy()
    msg = str(got.value)
    assert "WEB_CONCURRENCY" in msg and ("单进程" in msg or "single" in msg.lower())
    assert "aidumem-worker" in msg or "1" in msg, "拒绝时没给唯一合规路径"


def test_single_worker_and_unset_are_allowed(monkeypatch):
    """**回归**：未设或设 1 必须放行，不许把正常部署拦死。"""
    import api_server as A
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    A._enforce_single_process_policy()
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    A._enforce_single_process_policy()


def test_gunicorn_worker_flag_is_also_caught(monkeypatch):
    """argv 里的 `--workers 4` 同样要拦 —— 环境变量看不到它（与 F-01 同理）。"""
    import sys

    import api_server as A
    monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
    monkeypatch.setattr(sys, "argv", ["gunicorn", "-k", "uvicorn.workers.UvicornWorker",
                                      "--workers", "4", "api_server:app"])
    with pytest.raises(RuntimeError):
        A._enforce_single_process_policy()


def test_single_process_guard_is_wired_into_both_startup_paths():
    """**元守卫**：守卫必须接线，且**两条路都接**。

    「定义了不接线」在本仓已出现三次（VectorBackend 契约、
    load_custom_exclusion_patterns、resolve_latest）。而 F-01 的教训是：
    lifespan 与 main() 是两条独立入口，只接一条就等于漏了「没人跑的那条」。
    """
    import ast
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "api_server.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    callers = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call)
                    and getattr(sub.func, "id", "") == "_enforce_single_process_policy"):
                callers.add(node.name)
    assert len(callers) >= 2, (
        f"单进程守卫只在 {sorted(callers)} 接了线 —— lifespan 与 main() 两条入口都要接"
    )
