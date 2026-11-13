"""f0.8.0：connect_host 的 claude -p 验收要真召回暗号；CLAUDE_CONFIG_DIR 与 --project-dir。

合成数据：假 claude 可执行文件 + 本地服务桩，不碰真实宿主配置。
"""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from scripts import connect_host as C
from tests.test_f07_p3_connect_host import _Mem, home, stub  # noqa: F401  复用桩与 HOME 沙箱

_NONCE = "AIDU-ABC123"


def _fake_claude(tmp: Path, monkeypatch, reply: str, rc: int = 0) -> Path:
    bindir = tmp / "fakebin"
    bindir.mkdir(exist_ok=True)
    exe = bindir / "claude"
    exe.write_text(f"#!/bin/sh\nprintf '%s\\n' {json.dumps(reply)}\nexit {rc}\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    return exe


@pytest.fixture()
def probe(home, stub, monkeypatch):  # noqa: F811
    """固定暗号；写线直接往桩里存一条（真钩子写线另有用例覆盖）。"""
    monkeypatch.setattr(C.secrets, "token_hex", lambda n=3: "abc123")

    def fake_ingest(host, url, env_file, nonce, *_a, **_k):
        C._post(url, "/add", {"messages": [{"role": "user", "content": f"我的接入暗号是 {nonce}。"}]}, "")
    monkeypatch.setattr(C, "_ingest_probe", fake_ingest, raising=False)
    return stub


# ═══════════════ bug 2：claude_test 必须看到暗号 ═══════════════

def test_claude_test_fails_when_reply_lacks_probe_token(probe, home, monkeypatch):  # noqa: F811
    """★ 反向用例：退出码 0、有输出，但回答里没有暗号 → 必须红（f0.7 判绿）。"""
    _fake_claude(home, monkeypatch, "我能看到 aiduMEI 注入的记忆。")
    r = C.claude_test(url=probe, env_file=home / ".env", wait_s=3)
    assert r["ok"] is False, r
    assert r["nonce"] == _NONCE


def test_claude_test_passes_only_when_reply_contains_written_token(probe, home, monkeypatch):  # noqa: F811
    _fake_claude(home, monkeypatch, f"你的接入暗号是 {_NONCE}")
    r = C.claude_test(url=probe, env_file=home / ".env", wait_s=3)
    assert r["ok"] is True, r
    assert not any(_NONCE in m["memory"] for m in _Mem.store), "测试记忆没清掉"


def test_claude_test_token_but_nonzero_exit_is_red(probe, home, monkeypatch):  # noqa: F811
    _fake_claude(home, monkeypatch, f"{_NONCE}", rc=1)
    assert C.claude_test(url=probe, env_file=home / ".env", wait_s=3)["ok"] is False


def test_claude_test_not_called_when_token_never_searchable(home, stub, monkeypatch):  # noqa: F811
    """写线没写进去：不去问 claude（否则它可能凭空编个答案）。"""
    monkeypatch.setattr(C.secrets, "token_hex", lambda n=3: "abc123")
    monkeypatch.setattr(C, "_ingest_probe", lambda *a, **k: None, raising=False)
    exe = _fake_claude(home, monkeypatch, _NONCE)
    marker = home / "called"
    exe.write_text(f"#!/bin/sh\ntouch {marker}\necho {_NONCE}\n", encoding="utf-8")
    r = C.claude_test(url=stub, env_file=home / ".env", wait_s=0)
    assert r["ok"] is False and not marker.exists()


def test_cli_claude_test_wires_url_and_env(probe, home, monkeypatch, capsys):  # noqa: F811
    _fake_claude(home, monkeypatch, "看不到记忆")
    rc = C.main(["--host", "claude-code", "--url", probe, "--no-selftest", "--claude-test", "--wait", "3",
                 "--json"])
    rep = json.loads(capsys.readouterr().out)
    assert rep["claude"]["ok"] is False and rc == 1


def test_claude_process_real_hook_uses_selected_service_and_identity(home, monkeypatch):  # noqa: F811
    """A stale process URL/identity must not send the CLI's real hook elsewhere."""
    selected_calls, other_calls = [], []

    def handler(calls, selected):
        class Service(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reply(self, obj):
                raw = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):  # noqa: N802
                calls.append((self.path, {}))
                self.reply({"blocks": {}})

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                calls.append((self.path, body))
                allowed = selected and body.get("user_id") == body.get("caller_user_id") == "cli-user"
                self.reply({"results": [{"memory": _NONCE}] if allowed and self.path == "/search" else []})
        return Service

    selected = HTTPServer(("127.0.0.1", 0), handler(selected_calls, True))
    other = HTTPServer(("127.0.0.1", 0), handler(other_calls, False))
    for server in (selected, other):
        threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{selected.server_port}"
    monkeypatch.setenv("AIDUMEM_URL", f"http://127.0.0.1:{other.server_port}")
    monkeypatch.setenv("AIDUMEM_USER_ID", "other-user")
    monkeypatch.setattr(C.secrets, "token_hex", lambda n=3: "abc123")
    monkeypatch.setattr(C, "_ingest_probe", lambda *_a, **_k: None)
    bindir = home / "real-hook-bin"
    bindir.mkdir()
    exe = bindir / "claude"
    inject = C.REPO / "integrations/claude-code/aidumem-cc-inject.sh"
    exe.write_text(f"#!{sys.executable}\n" +
                   "import json,os,subprocess,sys\n" +
                   "payload=json.dumps({'session_id':'cli-scope','prompt':'我的接入暗号是什么？','cwd':os.getcwd()})\n" +
                   f"r=subprocess.run(['bash',{str(inject)!r}],input=payload,text=True,capture_output=True)\n" +
                   "print(r.stdout);sys.exit(r.returncode)\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    try:
        report = C.claude_test(url, home / ".env", wait_s=0, user_id="cli-user")
        assert report["ok"], report
        assert not other_calls, "CLI hook contacted the inherited service"
        assert any(body.get("query") == "我的接入暗号是什么？" and body.get("user_id") == "cli-user"
                   for path, body in selected_calls if path == "/search")
    finally:
        for server in (selected, other):
            server.shutdown()
            server.server_close()


def test_cli_model_selection_disables_tools_and_does_not_echo_other_context(probe, home, monkeypatch, capsys):  # noqa: F811
    """Use the operator-selected model and prevent native-memory writes in the probe."""
    exe = _fake_claude(home, monkeypatch, _NONCE)
    captured = home / "cli-arguments.json"
    exe.write_text(f"#!{sys.executable}\nimport json,sys\n" +
                   f"open({str(captured)!r},'w').write(json.dumps(sys.argv[1:]))\n" +
                   f"print({_NONCE!r}+' PRIVATE_UNRELATED_REPLY')\n")
    rc = C.main(["--host", "claude-code", "--url", probe, "--no-selftest", "--claude-test",
                 "--claude-model", "configured-model", "--wait", "3", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert rc == 0 and report["claude"]["ok"]
    args = json.loads(captured.read_text())
    assert args[args.index("--model") + 1] == "configured-model"
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--disallowedTools") + 1] == "mcp__*"
    prompt = args[args.index("-p") + 1]
    assert "PROBE-" in prompt and _NONCE not in prompt
    assert args.index(prompt) < args.index("--tools")
    assert "PRIVATE_UNRELATED_REPLY" not in json.dumps(report)


def test_probe_stores_lookup_label_with_answer_through_real_ingest_hook(home, stub):  # noqa: F811
    C._ingest_probe("claude-code", stub, home / ".env", _NONCE,
                    user_id="cli-user", lookup_id="PROBE-lookup-only")
    assert any("PROBE-lookup-only" in row["memory"] and _NONCE in row["memory"] for row in _Mem.store)


# ═══════════════ bug 4：CLAUDE_CONFIG_DIR 与 --project-dir ═══════════════

def test_install_and_check_honor_claude_config_dir(home, stub, monkeypatch):  # noqa: F811
    """★ 反向用例：设了 CLAUDE_CONFIG_DIR，钩子写进 $CLAUDE_CONFIG_DIR/settings.json（旧代码写 ~/.claude）。"""
    cfgdir = home / "cc-config"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfgdir))
    assert C.main(["--host", "claude-code", "--url", stub, "--no-selftest"]) == 0
    assert (cfgdir / "settings.json").exists()
    assert not (home / ".claude" / "settings.json").exists()
    assert C.main(["--host", "claude-code", "--url", stub, "--check", "--no-selftest"]) == 0


def test_check_under_claude_config_dir_ignores_default_home(home, stub, monkeypatch):  # noqa: F811
    """~/.claude 里接好了、CLAUDE_CONFIG_DIR 那份没接 → --check 必须红（CC 读的是后者）。"""
    assert C.main(["--host", "claude-code", "--url", stub, "--no-selftest"]) == 0
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "cc-config"))
    assert C.main(["--host", "claude-code", "--url", stub, "--check", "--no-selftest"]) == 1


def test_default_path_unchanged_without_env(home):  # noqa: F811
    assert C.host_config_path("claude-code") == home / ".claude" / "settings.json"
    assert C.host_config_path("codex") == home / ".codex" / "hooks.json"


def test_same_rule_as_cc_memory_bridge(home, monkeypatch):  # noqa: F811
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "cc_memory_bridge_rule", C.REPO / "integrations" / "claude-code" / "cc_memory_bridge.py")
    B = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(B)
    for val in (None, str(home / "x")):
        if val:
            monkeypatch.setenv("CLAUDE_CONFIG_DIR", val)
        assert C.claude_home() == B.claude_home()


def test_project_dir_install_and_check(home, stub, monkeypatch):  # noqa: F811
    """--project-dir 写/验收项目级 .claude/settings.json，用户级配置不动。"""
    proj = home / "proj"
    proj.mkdir()
    assert C.main(["--host", "claude-code", "--url", stub, "--no-selftest", "--project-dir", str(proj)]) == 0
    cfg = json.loads((proj / ".claude" / "settings.json").read_text())
    assert all(C.wired_lines(cfg, "claude-code").values())
    assert not (home / ".claude" / "settings.json").exists()
    assert C.main(["--host", "claude-code", "--url", stub, "--check", "--no-selftest",
                   "--project-dir", str(proj)]) == 0
    # 用户级没接 → 不带 --project-dir 的 check 仍是红的（默认行为不变）
    assert C.main(["--host", "claude-code", "--url", stub, "--check", "--no-selftest"]) == 1


def test_project_dir_uninstall_only_touches_project(home, stub):  # noqa: F811
    proj = home / "proj"
    proj.mkdir()
    C.main(["--host", "claude-code", "--url", stub, "--no-selftest", "--project-dir", str(proj)])
    C.main(["--host", "claude-code", "--url", stub, "--no-selftest"])
    assert C.main(["--host", "claude-code", "--uninstall", "--url", stub, "--project-dir", str(proj)]) == 0
    assert "hooks" not in json.loads((proj / ".claude" / "settings.json").read_text())
    user = json.loads((home / ".claude" / "settings.json").read_text())
    assert all(C.wired_lines(user, "claude-code").values())
