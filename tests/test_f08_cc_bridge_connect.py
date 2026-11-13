"""f0.8 ①：connect_host 挂 / 查 CC 自带记忆桥（PostToolUse, Write|Edit；f0.8+ 起去掉 MultiEdit，老 matcher 仍认）。"""
from __future__ import annotations

import json

from scripts import connect_host as C
from tests.test_f07_p3_connect_host import _cc, home, stub  # noqa: F401  复用 fixture

BRIDGE = "aidumem-cc-memory-bridge.sh"


def _bridge_groups(cfg):
    return [g for g in cfg["hooks"].get("PostToolUse", [])
            if any(BRIDGE in h.get("command", "") for h in g.get("hooks", []))]


def test_merge_adds_bridge_with_matcher_and_keeps_foreign(home):  # noqa: F811
    foreign = {"matcher": "Bash", "hooks": [{"type": "command", "command": "/opt/fmt.sh"}]}
    cfg = {"hooks": {"PostToolUse": [foreign]}}
    new, ch = C.merge_hooks(cfg, "claude-code", C.DEFAULT_URL, home / ".env")
    assert f"新增 PostToolUse → {BRIDGE}" in ch
    g = _bridge_groups(new)
    assert len(g) == 1 and g[0]["matcher"] == "Write|Edit"
    assert g[0]["hooks"][0]["timeout"] == 30
    assert str(C.REPO / "integrations/claude-code" / BRIDGE) in g[0]["hooks"][0]["command"]
    assert foreign in new["hooks"]["PostToolUse"]
    again, ch2 = C.merge_hooks(new, "claude-code", C.DEFAULT_URL, home / ".env")
    assert ch2 == [] and again == new
    assert C.cc_bridge_status(new)["wired"] is True


def test_wrong_matcher_is_repaired_once(home):  # noqa: F811
    new, _ = C.merge_hooks({}, "claude-code", C.DEFAULT_URL, home / ".env")
    _bridge_groups(new)[0]["matcher"] = ""
    assert C.cc_bridge_status(new)["wired"] is False
    assert C.wired_lines(new, "claude-code")["PostToolUse"] is False
    fixed, ch = C.merge_hooks(new, "claude-code", C.DEFAULT_URL, home / ".env")
    assert any("matcher" in c for c in ch) and not any(c.startswith("新增") for c in ch)
    assert [g["matcher"] for g in _bridge_groups(fixed)] == ["Write|Edit"]
    assert C.merge_hooks(fixed, "claude-code", C.DEFAULT_URL, home / ".env")[1] == []


def test_bridge_inside_shared_group_moves_out_keeping_neighbour(home):  # noqa: F811
    want = C.hook_command("claude-code", BRIDGE, C.DEFAULT_URL, home / ".env")
    other = {"type": "command", "command": "/opt/lint.sh"}
    cfg = {"hooks": {"PostToolUse": [{"matcher": "Write", "hooks": [other, {"type": "command", "command": want}]}]}}
    new, _ = C.merge_hooks(cfg, "claude-code", C.DEFAULT_URL, home / ".env")
    assert {"matcher": "Write", "hooks": [other]} in new["hooks"]["PostToolUse"]
    assert len(_bridge_groups(new)) == 1


def test_uninstall_removes_bridge_only(home):  # noqa: F811
    foreign = {"matcher": "Bash", "hooks": [{"type": "command", "command": "/opt/fmt.sh"}]}
    new, _ = C.merge_hooks({"hooks": {"PostToolUse": [foreign]}}, "claude-code", C.DEFAULT_URL, home / ".env")
    off, ch = C.merge_hooks(new, "claude-code", C.DEFAULT_URL, home / ".env", remove=True)
    assert f"摘除 PostToolUse → {BRIDGE}" in ch and off == {"hooks": {"PostToolUse": [foreign]}}


def test_check_reports_bridge_status(home, stub, capsys):  # noqa: F811
    (home / ".claude").mkdir()
    # 只有旧安装的三线：--check 报桥未挂并警告，不把正常的三线接入判成失败。
    old, _ = C.merge_hooks({}, "claude-code", stub, home / ".env")
    old["hooks"].pop("PostToolUse")
    _cc(home).write_text(json.dumps(old), encoding="utf-8")
    rc = C.main(["--host", "claude-code", "--url", stub, "--check", "--no-selftest", "--json"])
    rep = json.loads(capsys.readouterr().out)
    br = rep["hosts"][0]["cc_memory_bridge"]
    assert rc == 0 and br["wired"] is False and "未挂" in br["detail"] and br["script_present"]
    assert rep["warnings"] and rep["hosts"][0]["warnings"]
    assert json.loads(_cc(home).read_text()) == old  # --check 不改配置
    # 重跑 connect_host 补上后 --check 绿，文本报告里有桥那一行
    assert C.main(["--host", "claude-code", "--url", stub, "--no-selftest"]) == 0
    capsys.readouterr()
    rc = C.main(["--host", "claude-code", "--url", stub, "--check", "--no-selftest"])
    out = capsys.readouterr().out
    assert rc == 0 and "✅ 自带记忆桥 PostToolUse(Write|Edit): 已挂上" in out
    cfg = json.loads(_cc(home).read_text())
    assert set(cfg["hooks"]) == {"UserPromptSubmit", "Stop", "SessionEnd", "PostToolUse"}


def test_legacy_multiedit_matcher_still_counts_as_wired_and_is_upgraded(home):  # noqa: F811
    """f0.8+：老安装的 Write|Edit|MultiEdit 在 --check 里照样算挂上；重跑安装换成 Write|Edit。"""
    new, _ = C.merge_hooks({}, "claude-code", C.DEFAULT_URL, home / ".env")
    _bridge_groups(new)[0]["matcher"] = "Write|Edit|MultiEdit"
    assert C.cc_bridge_status(new)["wired"] is True
    assert C.wired_lines(new, "claude-code")["PostToolUse"] is True
    fixed, ch = C.merge_hooks(new, "claude-code", C.DEFAULT_URL, home / ".env")
    assert [g["matcher"] for g in _bridge_groups(fixed)] == ["Write|Edit"]
    assert not any(c.startswith("新增") for c in ch)
