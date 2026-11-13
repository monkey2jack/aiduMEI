"""f0.8+（外审 sesame R3-07）：出站校验钉住首次解析，堵 DNS rebinding 的校验-连接时间差。

审计原用例（附录 A test_r3_07_dns_rebind.py）先 check_url 再裸 requests.get——那条路径没有任何钩子，
修不了也不该修（check_url 只是判定函数）。真正的外呼点都走 ``guard_url``，所以这里按外呼点的真实
写法改成 guard_url → requests/httpx：第一次解析拿到公网地址，之后的解析都指向本机桩服务，
请求必须到不了桩服务。合成数据，本机桩。
"""
from __future__ import annotations

import http.server
import socket
import threading

import httpx
import pytest
import requests

from ducky.security import egress

_PUBLIC = "93.184.216.34"  # release-scan:allow synthetic security fixture, no production credential/address


@pytest.fixture()
def stub():
    hits = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"internal")

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], hits
    srv.shutdown()
    srv.server_close()


@pytest.fixture()
def rebinding(monkeypatch):
    """rebind.example：第一次解析给公网地址，之后都给 127.0.0.1。"""
    real = socket.getaddrinfo
    answers = iter([_PUBLIC])

    def fake(host, *a, **k):
        if host == "rebind.example":
            ip = next(answers, "127.0.0.1")
            port = a[0] if a and a[0] else k.get("port") or 0
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]
        return real(host, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    monkeypatch.delenv("AIDUMEI_EGRESS_ALLOW", raising=False)
    monkeypatch.delenv("AIDUMEI_MODEL_ENDPOINT_ALLOW", raising=False)
    egress.clear_pins()
    yield
    egress.clear_pins()


def test_r3_07_requests_after_guard_cannot_be_rebound(stub, rebinding):
    port, hits = stub
    url = f"http://rebind.example:{port}/internal"
    assert egress.guard_url(url, "test") == url            # 守卫看到公网地址，放行
    try:
        requests.get(url, timeout=1, allow_redirects=False)
    except requests.RequestException:
        pass
    assert hits == [], "守卫放行公网地址之后，请求却打到了 127.0.0.1"


def test_r3_07_httpx_after_guard_cannot_be_rebound(stub, rebinding):
    port, hits = stub
    url = f"http://rebind.example:{port}/internal"
    egress.guard_url(url, "test")
    with httpx.Client(timeout=1, follow_redirects=False) as c:
        try:
            c.get(url)
        except httpx.HTTPError:
            pass
    assert hits == []


def test_r3_07_unresolvable_at_check_time_fails_closed(monkeypatch):
    real = socket.getaddrinfo
    calls = []

    def fake(host, *a, **k):
        if host == "flaky.example":
            calls.append(host)
            if len(calls) == 1:
                raise socket.gaierror(socket.EAI_NONAME, "synthetic")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
        return real(host, *a, **k)
    monkeypatch.setattr(socket, "getaddrinfo", fake)
    egress.clear_pins()
    egress.guard_url("http://flaky.example/x", "test")     # 校验时解析失败：不拦，但钉成空集
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("flaky.example", 80)
    egress.clear_pins()


def test_r3_07_recheck_bypasses_pins_and_pins_expire(monkeypatch):
    """复检自己的解析不读钉住表（一次失败不会粘住）；钉住过期后透传。"""
    real = socket.getaddrinfo
    state = {"n": 0}

    def fake(host, *a, **k):
        if host == "later.example":
            state["n"] += 1
            if state["n"] == 1:
                raise socket.gaierror(socket.EAI_NONAME, "synthetic")
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC, 443))]
        return real(host, *a, **k)
    monkeypatch.setattr(socket, "getaddrinfo", fake)
    egress.clear_pins()
    egress.guard_url("https://later.example/x", "test")
    egress.guard_url("https://later.example/x", "test")     # 第二次复检真解析到了
    assert socket.getaddrinfo("later.example", 443)[0][4][0] == _PUBLIC
    egress.pin_host("later.example", ["198.51.100.7"], ttl=-1)  # 已过期
    assert socket.getaddrinfo("later.example", 443)[0][4][0] == _PUBLIC
    egress.clear_pins()


def test_r3_07_pins_are_per_thread(monkeypatch):
    egress.clear_pins()
    egress.pin_host("only-here.example", ["198.51.100.9"])
    assert socket.getaddrinfo("only-here.example", 80)[0][4][0] == "198.51.100.9"
    seen = {}

    def other():
        seen["pin"] = egress._pinned("only-here.example")
    t = threading.Thread(target=other)
    t.start()
    t.join()
    assert seen["pin"] is None
    egress.clear_pins()


def test_r3_07_blocked_targets_still_refused():
    with pytest.raises(egress.EgressBlocked):
        egress.guard_url("http://169.254.169.254/latest/meta-data", "test")


def test_r3_07_mem0_sdk_httpx_client_is_guarded():
    """mem0 SDK 内部 openai 客户端：挂上 request 钩子后，打元数据地址的请求在发出前就被拦。"""
    c = httpx.Client(timeout=1)
    assert egress.attach_httpx_guard(c, "mem0_llm") is True
    assert egress.attach_httpx_guard(c, "mem0_llm") is True       # 幂等
    assert len(c.event_hooks["request"]) == 1
    with pytest.raises(egress.EgressBlocked):
        c.get("http://169.254.169.254/latest/meta-data")
    c.close()


def test_r3_07_mem0_runtime_attaches_guard_to_sdk_clients():
    from ducky import mem0_runtime as M

    class _SDK:
        def __init__(self):
            self._client = httpx.Client()

    class _Part:
        def __init__(self):
            self.client = _SDK()

    class _Mem:
        llm = _Part()
        embedding_model = _Part()
    assert M._guard_sdk_clients(_Mem()) == ["llm", "embedding_model"]
