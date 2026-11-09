"""f0.8 ③-1 SSRF 收口：外审 PoC 反向断言（修复前全部放行）。

DNS 一律走注入的假解析器，不依赖网络。
"""
from __future__ import annotations

import pytest

from ducky.security import egress


FAKE_DNS = {
    "10.0.0.5.nip.io": ["10.0.0.5"],
    "127.0.0.1.nip.io": ["127.0.0.1"],
    "169.254.169.254.nip.io": ["169.254.169.254"],
    "100-64-0-1.sslip.io": ["100.64.0.1"],
    "192-168-0-1.sslip.io": ["192.168.0.1"],
    "evil.example.com": ["93.184.216.34", "10.1.2.3"],     # 多记录里混一个私网  # release-scan:allow 公网地址替身(RFC2606 域的公开解析值,非部署地址)
    "v6evil.example.com": ["::ffff:10.0.0.1"],
    "ula.example.com": ["fd12:3456::1"],
    "linklocal6.example.com": ["fe80::1"],
    "api.public.example": ["93.184.216.34"],  # release-scan:allow 公网地址替身(同上)
    "gpu-box": ["192.168.1.50"],
    "meta-alias.example": ["169.254.169.254"],
    "localhost": ["127.0.0.1"],
}


def _fake(host):
    if host in FAKE_DNS:
        return FAKE_DNS[host]
    raise OSError("nxdomain")


@pytest.fixture(autouse=True)
def _dns(monkeypatch):
    monkeypatch.delenv("AIDUMEI_MODEL_ENDPOINT_ALLOW", raising=False)
    monkeypatch.delenv("AIDUMEI_EGRESS_ALLOW_LOOPBACK", raising=False)
    egress.set_resolver(_fake)
    yield
    egress.set_resolver(None)


BLOCKED = [
    # 外审 PoC：域名解析到私网/元数据/CGNAT
    "https://10.0.0.5.nip.io/v1",
    "https://127.0.0.1.nip.io:8443/v1",
    "https://169.254.169.254.nip.io/latest",
    "https://100-64-0-1.sslip.io/v1",
    "https://192-168-0-1.sslip.io/v1",
    "https://evil.example.com/v1",
    "https://v6evil.example.com/v1",
    "https://ula.example.com/v1",
    "https://linklocal6.example.com/v1",
    "https://meta-alias.example/v1",
    # 尾点
    "https://metadata.google.internal./v1",
    "https://169.254.169.254./latest",
    "https://10.0.0.5./v1",
    "https://2130706433./v1",
    "https://0177.0.0.1./v1",
    "http://localhost./v1".replace("localhost.", "10.0.0.5."),
    "https://x.internal./v1",
    "https://host../v1",
    # 数字写法
    "https://2130706433/v1",
    "https://0x7f000001/v1",
    "https://0x0a.0.0.1/v1",
    "https://012.0.0.1/v1",
    "https://3232235777/v1",           # 192.168.1.1
    "https://99999999999/v1",          # 全数字但不是合法 IP → fail-closed
    "https://0x1.0x2.0x3.0x4.0x5/v1",
    # IPv6 与内嵌 IPv4
    "https://[::ffff:10.0.0.1]/v1",
    "https://[::ffff:169.254.169.254]/v1",
    "https://[::ffff:7f00:1]/v1",
    "https://[2002:a00:1::]/v1",        # 6to4 内嵌 10.0.0.1
    "https://[fd00:ec2::254]/v1",
    "https://[fe80::1]/v1",
    "https://[fc00::1]/v1",
    "https://[::]/v1",
    # 其他
    "https://100.100.100.200/latest",   # 云厂商元数据
    "https://0.0.0.0/v1",
    "https://224.0.0.1/v1",
    "https://metadata/v1",
    "https://user:pw@api.public.example/v1",
    "ftp://api.public.example/v1",
    "http://api.public.example/v1",     # 远程明文 http
]


@pytest.mark.parametrize("url", BLOCKED)
def test_blocked_urls(url):
    from ducky.routes_config import _reject_private_endpoint
    assert _reject_private_endpoint(url), url


ALLOWED = [
    "https://api.public.example/v1",
    "https://api.unresolvable.example/v1",   # 解析失败不误杀（连不上自会失败）
    "http://127.0.0.1:8000/v1",
    "http://localhost:11434/v1",
    "http://localhost.:11434/v1",            # 尾点规范化后就是 localhost，同口径
    "http://[::1]:8000/v1",
    "https://127.0.0.1/v1",
]


@pytest.mark.parametrize("url", ALLOWED)
def test_allowed_urls(url):
    from ducky.routes_config import _reject_private_endpoint
    assert _reject_private_endpoint(url) is None, url


def test_loopback_can_be_disabled(monkeypatch):
    monkeypatch.setenv("AIDUMEI_EGRESS_ALLOW_LOOPBACK", "0")
    assert egress.check_url("http://127.0.0.1:8000/v1")
    assert egress.check_url("http://localhost:8000/v1")


def test_allowlist_names_ips_cidrs(monkeypatch):
    monkeypatch.setenv("AIDUMEI_MODEL_ENDPOINT_ALLOW", "gpu-box, 10.20.0.0/16 ,192.168.7.9")
    assert egress.check_url("http://gpu-box:8000/v1") is None
    assert egress.check_url("http://10.20.3.4:8000/v1") is None
    assert egress.check_url("https://192.168.7.9/v1") is None
    assert egress.check_url("https://192.168.7.10/v1")          # 不在名单
    assert egress.check_url("https://10.0.0.5.nip.io/v1")        # 名单外域名照拒


def test_allowlist_never_opens_metadata(monkeypatch):
    monkeypatch.setenv("AIDUMEI_MODEL_ENDPOINT_ALLOW",
                       "169.254.0.0/16,metadata.google.internal,meta-alias.example,100.100.100.200")
    assert egress.check_url("http://169.254.169.254/latest")
    assert egress.check_url("http://metadata.google.internal/latest")
    assert egress.check_url("http://meta-alias.example/latest")
    assert egress.check_url("http://100.100.100.200/latest")


def test_guard_url_raises_runtime_error_subclass():
    with pytest.raises(RuntimeError):
        egress.guard_url("https://10.0.0.5.nip.io/v1/chat/completions", "llm")
    # 运行时复检不因 scheme 打断存量配置，只看目的地址
    assert egress.guard_url("http://api.public.example/v1") == "http://api.public.example/v1"


def test_config_probe_does_not_call_out_to_blocked_endpoint(monkeypatch):
    """外审 muse M-09：/config/test 此前直接带配置 key POST 到文件里的端点。"""
    import requests
    from ducky import config_probe

    calls = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: calls.append(a) or None)
    monkeypatch.setattr(requests, "get", lambda *a, **k: calls.append(a) or None)
    monkeypatch.setattr(config_probe, "_raw_config", lambda: {
        "llm": {"provider": "openai", "config": {
            "model": "m", "api_key": "sk-canary", "openai_base_url": "https://169.254.169.254.nip.io/v1"}}})
    monkeypatch.setattr(config_probe, "_resolved", lambda raw, s: dict(raw[s]["config"]))
    monkeypatch.setattr("ducky.engine_mode.cloud_egress_allowed", lambda *_a, **_k: True)
    out = config_probe.probe("llm")
    assert out["ok"] is False and out["status"] == "blocked_by_egress_policy"
    assert calls == []
    assert "sk-canary" not in str(out)


def test_llm_client_runtime_guard(monkeypatch):
    import requests
    from ducky import llm_client
    calls = []
    monkeypatch.setattr(requests, "post", lambda *a, **k: calls.append(a))
    with pytest.raises(RuntimeError):
        llm_client._post_completion("https://10.0.0.5.nip.io/v1/chat/completions", "k", "m",
                                    [{"role": "user", "content": "x"}], 1, 0.0, 5)
    assert calls == []


def test_rerank_runtime_guard(monkeypatch):
    from ducky import mem0_runtime as rt
    monkeypatch.setattr("ducky.engine_mode.cloud_egress_allowed", lambda *_a, **_k: True)
    monkeypatch.setattr(rt, "_load_rerank_config", lambda: {
        "enabled": True, "api_key": "k", "base_url": "https://192-168-0-1.sslip.io/v1",
        "provider": "openai_compatible", "model": "r"})
    called = []
    monkeypatch.setitem(rt.RERANK_PROVIDERS, "openai_compatible", lambda *a: called.append(a) or [])
    assert rt.rerank("q", ["a", "b"]) == []
    assert called == []
    assert rt._rerank_tls.last["status"] == "error"


def test_native_embedder_guard_and_no_redirects():
    from ducky.memory_kernel.impl_native import HttpEmbedder
    e = HttpEmbedder({"provider": "openai", "config": {"openai_base_url": "https://10.0.0.5.nip.io/v1"}})
    import ducky.engine_mode as em
    orig = em.cloud_egress_allowed
    em.cloud_egress_allowed = lambda *_a, **_k: True
    try:
        with pytest.raises(RuntimeError):
            e.embed("hello")
    finally:
        em.cloud_egress_allowed = orig
    import inspect
    src = inspect.getsource(HttpEmbedder)
    assert "follow_redirects=False" in src


def test_vector_store_host_checked():
    from ducky.routes_config import _validate_config_fields
    err = _validate_config_fields("vector_store", {"host": "http://169.254.169.254.nip.io"}, {}, {})
    assert err and "vector_store.host" in err
    assert _validate_config_fields("vector_store", {"host": "http://127.0.0.1"}, {}, {}) is None


def test_stored_config_audit_flags_bad_endpoints():
    lines = egress.audit_config_endpoints({
        "llm": {"config": {"openai_base_url": "https://10.0.0.5.nip.io/v1"}},
        "embedder": {"config": {"openai_base_url": "https://api.public.example/v1"}},
        "vector_store": {"config": {"host": "metadata.google.internal"}},
    })
    assert any(x.startswith("llm.") for x in lines)
    assert not any(x.startswith("embedder.") for x in lines)
    assert any(x.startswith("vector_store.") for x in lines)


def test_all_outbound_requests_disable_redirects():
    """棘轮：ducky/ 下每个 requests.post/get 调用都显式 allow_redirects=False。"""
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parents[1] / "ducky"
    bad = []
    for p in root.rglob("*.py"):
        src = p.read_text(encoding="utf-8")
        for m in re.finditer(r"\b(?:requests\.(?:post|get)|req\.post)\(", src):
            window = src[m.start(): m.start() + 900]
            depth, end = 0, None
            for i, ch in enumerate(window):
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        end = i
                        break
            call = window[: end or len(window)]
            if "allow_redirects=False" not in call:
                bad.append(f"{p.name}:{src[:m.start()].count(chr(10)) + 1}")
    assert bad == [], bad
