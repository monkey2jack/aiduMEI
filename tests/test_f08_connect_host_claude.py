"""f0.8.0：connect_host 的 claude -p 验收要真召回暗号；CLAUDE_CONFIG_DIR 与 --project-dir。

合成数据：假 claude 可执行文件 + 本地服务桩，不碰真实宿主配置。
"""
from __future__ import annotations

import json
import os
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

    def fake_ingest(host, url, env_file, nonce):
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
