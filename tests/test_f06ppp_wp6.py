"""f0.6+++ WP6 回归：CoreMemory 注入 403 / cc-cc- 双前缀 / --port / rerank 连接复用。"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import contextmanager
import threading
from urllib.parse import parse_qs, urlparse

from tests.test_f04pp_cc_adaptation import (
    _CC_HOOKS, _TEST_UID, _hook_env, _make_transcript, _run_hook, _wait_for,
    _capture_server,
)

_CODEX_HOOKS = _CC_HOOKS.parent / "codex"


_MIXED_BLOCKS = {
    "core_user": {"content": "用户喜欢简洁"},
    "core_current_project": {"content": "[自动草稿·待确认] 是的用户！草稿不该注入"},
    "core_key_decisions": {"content": "（尚未填写）已达成的关键决策与约定"},
}


@contextmanager
def _core_server(blocks=None):
    """模拟服务端契约：Bearer 调用 GET /api/core-memory 不带 caller_user_id → 403。"""
    calls: list = []

    class H(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            calls.append(self.path)
            u = urlparse(self.path)
            if u.path == "/health":
                return self._send(200, {"status": "ok"})
            q = parse_qs(u.query)
            if u.path == "/api/core-memory":
                if q.get("caller_user_id") != q.get("user_id"):
                    return self._send(403, {"detail": "API token 调用方必须声明 caller_user_id"})
                return self._send(200, {"blocks": _MIXED_BLOCKS if blocks is None else blocks})
            self._send(404, {})

        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self._send(200, {"results": []})

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv.server_port, calls
    finally:
        srv.shutdown()
        srv.server_close()


def _inject(script, tmp_path, blocks=None):
    with _core_server(blocks) as (port, calls):
        r = _run_hook(script, {"prompt": "我喜欢什么风格", "session_id": "wp6"},
                      _hook_env(port, tmp_path), timeout=20)
    return r, calls


def test_cc_inject_declares_caller_and_injects_core_memory(tmp_path):
    for script in (_CC_HOOKS / "aidumem-cc-inject.sh",
                   _CODEX_HOOKS / "aidumem-codex-inject.sh"):
        r, calls = _inject(script, tmp_path)
        assert r.returncode == 0
        core = [c for c in calls if c.startswith("/api/core-memory")]
        assert core and parse_qs(urlparse(core[0]).query)["caller_user_id"] == [_TEST_UID]
        out = r.stdout.decode()
        assert "用户喜欢简洁" in out, (script.name, r.stdout)
        # 只注入已确认块：自动草稿 / 占位跳过；全是草稿则不注入 CoreMemory
        assert "自动草稿" not in out and "尚未填写" not in out, (script.name, out)
        drafts = {k: v for k, v in _MIXED_BLOCKS.items() if k != "core_user"}
        r2, _ = _inject(script, tmp_path, blocks=drafts)
        assert r2.returncode == 0 and "CoreMemory" not in r2.stdout.decode(), (script.name, r2.stdout)


def test_negative_control_inject_without_caller_gets_nothing(tmp_path):
    src = (_CC_HOOKS / "aidumem-cc-inject.sh").read_text(encoding="utf-8")
    old = src.replace(' \\\n  --data-urlencode "caller_user_id=$_uid" "$AIDUMEM_URL/api/core-memory"',
                      ' \\\n  "$AIDUMEM_URL/api/core-memory"')
    assert old != src
    copy = tmp_path / "old_inject.sh"
    copy.write_text(old, encoding="utf-8")
    r, _ = _inject(copy, tmp_path)
    assert "CoreMemory" not in r.stdout.decode()


def _distill_sid(tmp_path, script, sid):
    def route(path):
        return {"status": "skipped", "reason": "too_short", "source_count": 0, "min_required": 3}
    with _capture_server(route) as (port, calls):
        _run_hook(script, {"session_id": sid, "hook_event_name": "SessionEnd"},
                  _hook_env(port, tmp_path, AIDUMEI_DISTILL_FOREGROUND="1"), timeout=40)
        assert _wait_for(calls, lambda p: p.startswith("/session/distill"))
    p = next(p for p, _ in calls if p.startswith("/session/distill"))
    return parse_qs(urlparse(p).query)["session_id"][0]


def _ingest_sid(tmp_path, script, sid):
    tp = _make_transcript(tmp_path, "前缀测试", "好")
    with _capture_server(lambda p: {"status": "accepted"}) as (port, calls):
        _run_hook(script, {"session_id": sid, "transcript_path": str(tp),
                           "hook_event_name": "Stop"}, _hook_env(port, tmp_path))
        assert _wait_for(calls, lambda p: p.startswith("/add"))
    body = next(b for p, b in calls if p.startswith("/add"))
    return json.loads(body.decode())["metadata"]["_origin_session_id"]


def test_session_prefix_is_idempotent(tmp_path):
    cc_d, cc_i = _CC_HOOKS / "aidumem-cc-distill.sh", _CC_HOOKS / "aidumem-cc-ingest.sh"
    for sid in ("abc-123", "cc-abc-123"):
        assert _distill_sid(tmp_path, cc_d, sid) == "cc-abc-123"
        assert _ingest_sid(tmp_path, cc_i, sid) == "cc-abc-123"
    cx_d = _CODEX_HOOKS / "aidumem-codex-distill.sh"
    assert _distill_sid(tmp_path, cx_d, "codex-x1") == "codex-x1"


def _main_port(monkeypatch, argv, env):
    import api_server
    for k in ("AIDUMEM_API_PORT", "MEM0_API_PORT", "PORT"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(sys, "argv", ["api_server.py", *argv])
    monkeypatch.setattr(api_server, "_enforce_public_binding_policy", lambda: None)
    monkeypatch.setattr(api_server, "_enforce_single_process_policy", lambda: None)
    seen = {}
    monkeypatch.setattr(api_server.uvicorn, "run", lambda app, **kw: seen.update(kw))
    api_server.main()
    return seen["port"]


def test_cli_port_wins_and_env_still_works(monkeypatch):
    assert _main_port(monkeypatch, ["--port", "8799"], {"AIDUMEM_API_PORT": "8777"}) == 8799
    assert _main_port(monkeypatch, ["--port=8798"], {}) == 8798
    assert _main_port(monkeypatch, [], {"AIDUMEM_API_PORT": "8777"}) == 8777
    assert _main_port(monkeypatch, [], {}) == 8767


def test_rerank_reuses_one_http_session(monkeypatch):
    from ducky import mem0_runtime as rt
    monkeypatch.setattr(rt, "_rerank_session", None)
    a = rt._rerank_http()
    assert a is rt._rerank_http(), "每次新建会话 = 每次重做 TLS 握手（生产 ~0.4s/次）"
    sent = []

    class R:
        status_code = 200
        def json(self):
            return {"results": [{"index": 0, "relevance_score": 0.9}]}

    monkeypatch.setattr(a, "post", lambda *x, **k: sent.append(k) or R())
    cfg = {"base_url": "https://x.invalid/v1", "api_key": "k", "model": "m"}
    rt._rerank_openai_compatible(cfg, "q", ["d"], 1)
    rt._rerank_openai_rerank(cfg, "q", ["d"], 1)
    assert len(sent) == 2 and all(k["allow_redirects"] is False for k in sent)
