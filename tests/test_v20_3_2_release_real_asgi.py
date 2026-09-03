"""v20.3.2 正式版 · P0-4 / P0-5：守卫必须与生产 ASGI 栈同构。

**为什么这份测试不用 TestClient**（外审 GLM-5.3 F-1，本机真 uvicorn 复现）：

    v20.3.2-beta 的 P0-1 修复给了逃生阀 AIDUMEI_TRUST_PROXY。9 条守卫全绿。
    生产上它恒 503。

原因：uvicorn 默认 `proxy_headers=True`，自带的 ProxyHeadersMiddleware 会用
X-Forwarded-For 的**值**覆盖 `request.client.host`。于是「对端是不是回环」这条判据
在生产路径上读到的是**伪造 IP**，而不是 TCP 对端 —— 正是全仓反复说「不采信 XFF 值」
要防的那件事，被 uvicorn 替我们采信了。TestClient 不经过 uvicorn 那一层，所以守卫
看不见。**守卫全绿的唯一原因，是它与生产 ASGI 栈不同构。**

同一批还有 GLM F-2 / Codex F-08：中间件真实顺序与注释相反。`_require_credentials`
注册最晚 = 最外层，于是它直接返回的 401/503 **不经过**计数中间件与安全头中间件 ——
我上一轮加的四个安全头，没盖到我上一轮加的拒绝分支。现有守卫是 AST 结构扫描，
测不到这个行为。

所以本文件的纪律：**凡与请求来源 / 请求头 / 中间件层次有关的判据，一律起真
uvicorn.Server + 真 socket 来验。** TestClient 只许测路由内部逻辑。
"""
import os
import socket
import threading
import time
import urllib.error
import urllib.request

# 回环请求不许受宿主 *_PROXY 环境影响（生产 .env 带代理时 127.0.0.1 会被送进代理 → 超时/失败）
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

import pytest


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _RealServer:
    """起一个真 uvicorn.Server（默认参数，与 main() 同形态），真 socket 打请求。"""

    def __init__(self, app, **uvicorn_kwargs):
        import uvicorn
        self.port = _free_port()
        cfg = uvicorn.Config(app, host="127.0.0.1", port=self.port,
                             log_level="error", lifespan="off", **uvicorn_kwargs)
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        # 就绪判据是「端口接受 TCP 连接」，不是 /health 200：/health 在带真后端配置的机器上
        # 一次探针可能远超 0.5s（云端可达性探测），用它轮询会把「服务已起」判成「没起来」。
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return self
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("真 uvicorn 没起来（端口 10s 内未接受连接）")

    def __exit__(self, *_):
        self.server.should_exit = True
        self.thread.join(5)

    def get(self, path, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers or {})
        try:
            r = _NO_PROXY_OPENER.open(req, timeout=5)
            return r.status, dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers)


@pytest.fixture()
def bare_app(tmp_path, monkeypatch):
    """零凭据实例（自造世界：先 import 让 .env 副作用发生，再清环境、钉哈希路径）。"""
    monkeypatch.setenv("AIDUMEM_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("AIDUMEM_LOG_DIR", str(tmp_path / "l"))
    import api_server as A  # noqa: F401
    for k in ("AIDUMEM_API_TOKEN", "AIDUMEM_UI_PASSWORD", "AIDUMEI_TRUST_PROXY"):
        monkeypatch.delenv(k, raising=False)
    import ducky.security.auth as _auth
    hp = tmp_path / "ui_password_hash"
    monkeypatch.setattr(_auth, "password_hash_path", lambda: str(hp))
    A._ensure_ui_password()
    assert A._auth_enabled() is False, "夹具前提破了：本该是无凭据形态"
    return A


@pytest.fixture()
def token_app(tmp_path, monkeypatch):
    """有凭据实例。"""
    monkeypatch.setenv("AIDUMEM_DATA_DIR", str(tmp_path / "d2"))
    monkeypatch.setenv("AIDUMEM_LOG_DIR", str(tmp_path / "l2"))
    import api_server as A  # noqa: F401
    monkeypatch.setenv("AIDUMEM_API_TOKEN", "probe-token-not-a-real-secret")
    monkeypatch.delenv("AIDUMEI_TRUST_PROXY", raising=False)
    assert A._auth_enabled() is True
    return A


# ══════════════════════════════════════════════════════════════
# P0-4 · 逃生阀必须在真 uvicorn 下生效
# ══════════════════════════════════════════════════════════════

def test_main_runs_uvicorn_without_trusting_proxy_headers():
    """结构守卫：main() 的 uvicorn.run 必须显式 proxy_headers=False。

    默认 True 会让 uvicorn 用 XFF 的**值**改写 client.host —— 全仓「不采信 XFF 值」
    的哲学被启动参数一行推翻。这条钉的是启动参数，下面那条钉的是行为。
    """
    import ast
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "api_server.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    runs = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and n.func.attr == "run"
            and getattr(n.func.value, "id", "") == "uvicorn"]
    assert runs, "找不到 uvicorn.run 调用 —— 守卫失去着力点"
    for call in runs:
        kw = {k.arg: k.value for k in call.keywords}
        assert "proxy_headers" in kw, "uvicorn.run 未显式设置 proxy_headers（默认 True 会采信 XFF 值）"
        v = kw["proxy_headers"]
        assert isinstance(v, ast.Constant) and v.value is False, "proxy_headers 必须为 False"


def test_trust_proxy_escape_hatch_works_under_real_uvicorn(bare_app, monkeypatch):
    """**P0-4 靶心**：TRUST_PROXY=1 + 带 XFF，在**真 uvicorn**下必须放行。

    修复前：uvicorn 默认 proxy_headers=True → client.host 被改成 XFF 的值（非回环）
    → 即使 TRUST_PROXY=1 也 503。TestClient 下同一用例是绿的。
    """
    monkeypatch.setenv("AIDUMEI_TRUST_PROXY", "1")
    with _RealServer(bare_app.app, proxy_headers=_production_proxy_headers()) as s:
        code, _ = s.get("/facts?user_id=probe&bank_id=default",
                        headers={"X-Forwarded-For": "203.0.113.7"})
    assert code != 503, (
        f"逃生阀在真 uvicorn 下失效（{code}）：部署方按文档设了 TRUST_PROXY=1 仍被拒 —— "
        "uvicorn 的 ProxyHeadersMiddleware 用 XFF 值覆盖了 client.host"
    )


def test_no_trust_proxy_still_refuses_under_real_uvicorn(bare_app, monkeypatch):
    """**负向对照**：未设 TRUST_PROXY + 带 XFF → 真 uvicorn 下仍 503（fail-closed 没破）。"""
    monkeypatch.delenv("AIDUMEI_TRUST_PROXY", raising=False)
    with _RealServer(bare_app.app, proxy_headers=_production_proxy_headers()) as s:
        code, _ = s.get("/facts?user_id=probe&bank_id=default",
                        headers={"X-Forwarded-For": "203.0.113.7"})
    assert code == 503, f"无凭据 + 反代痕迹 + 未声明信任，真 uvicorn 下放行了（{code}）"


def test_direct_loopback_ok_under_real_uvicorn(bare_app):
    """**回归**：无代理头的本机直连在真 uvicorn 下照旧放行（hermes/MCP/cron 形态）。"""
    with _RealServer(bare_app.app, proxy_headers=_production_proxy_headers()) as s:
        code, _ = s.get("/facts?user_id=probe&bank_id=default")
    assert code != 503


def _production_proxy_headers() -> bool:
    """读 main() 实际传给 uvicorn.run 的 proxy_headers 值 —— 测的必须是生产那一条。"""
    import ast
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "api_server.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "run" and getattr(n.func.value, "id", "") == "uvicorn"):
            for k in n.keywords:
                if k.arg == "proxy_headers" and isinstance(k.value, ast.Constant):
                    return bool(k.value.value)
    return True  # uvicorn 默认


# ══════════════════════════════════════════════════════════════
# P0-5 · 中间件顺序：401/503 必须被计数、必须带安全头
# ══════════════════════════════════════════════════════════════

_SECURITY_HEADERS = ("x-content-type-options", "x-frame-options",
                     "referrer-policy", "content-security-policy")


def test_401_is_counted_and_carries_security_headers(token_app):
    """**P0-5 靶心**：被门禁挡下的 401 也是一次真实结局。

    修复前：`_require_credentials` 注册最晚 = 最外层，它直接 return 的 401
    不经过内层的计数与安全头中间件 —— 计数增量 1 而不是 2，401 无 CSP。
    注释写的是「放在鉴权外面是刻意的」，与事实相反。
    """
    from ducky import http_metrics
    before = http_metrics.snapshot().get("total", 0)
    with _RealServer(token_app.app, proxy_headers=_production_proxy_headers()) as s:
        code, headers = s.get("/facts?user_id=probe&bank_id=default")
    assert code == 401, f"夹具前提破了：有凭据实例的无凭据请求应 401，得 {code}"
    after = http_metrics.snapshot().get("total", 0)
    assert after - before >= 1, "401 没进 http_metrics —— 「凭据链断了满屏 401」这种事故形态从统计里消失"
    lower = {k.lower() for k in headers}
    missing = [h for h in _SECURITY_HEADERS if h not in lower]
    assert not missing, f"401 响应缺安全头 {missing} —— 安全头中间件没盖到拒绝分支"


def test_503_no_credential_refusal_carries_security_headers(bare_app):
    """无凭据拒绝（503）同样要带安全头、进计数。"""
    from ducky import http_metrics
    before = http_metrics.snapshot().get("total", 0)
    with _RealServer(bare_app.app, proxy_headers=_production_proxy_headers()) as s:
        code, headers = s.get("/facts?user_id=probe&bank_id=default",
                              headers={"X-Forwarded-For": "203.0.113.7"})
    assert code == 503
    assert http_metrics.snapshot().get("total", 0) - before >= 1, "503 拒绝没进计数"
    lower = {k.lower() for k in headers}
    assert all(h in lower for h in _SECURITY_HEADERS), f"503 缺安全头：{sorted(lower)}"


def test_middleware_order_comment_matches_reality():
    """注释说「计数在鉴权外面」—— 那就必须真的在外面。

    Starlette：后注册者在更外层。所以计数与安全头必须注册在 `_require_credentials`
    **之后**。这条钉源码顺序，上面两条钉行为；两条都要，因为顺序对了行为也可能因
    别的原因不对，行为对了顺序也可能是碰巧。
    """
    import ast
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "api_server.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    order = []
    for n in tree.body:
        if isinstance(n, ast.AsyncFunctionDef) and any(
                isinstance(d, ast.Call) and getattr(getattr(d.func, "attr", None), "__str__", lambda: "")() == "middleware"
                for d in n.decorator_list):
            order.append(n.name)
    assert "_require_credentials" in order and "_record_http_outcome" in order and "_security_headers" in order, order
    assert order.index("_require_credentials") < order.index("_security_headers") < order.index("_record_http_outcome"), (
        f"中间件注册顺序 {order}：鉴权必须最早注册（=最内层），计数最晚注册（=最外层）"
    )
