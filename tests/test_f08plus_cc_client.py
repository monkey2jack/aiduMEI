"""f0.8+ 流 A：Claude Code 客户端侧（钩子 / 记忆桥 / 安装器）回归。合成数据，本地桩服务。

- R3-01（外审 sesame）：项目目录的 ./.env 不许决定身份、不许选别人的钥匙文件（桥 + 读线 + 写线）。
- R3-02：项目级 settings.local.json 的 autoMemoryDirectory 不许把家目录 / 白名单外变成记忆目录。
- P0-1：写入白名单 AIDUMEI_CC_ALLOW_DIRS、测试隔离开关 AIDUMEI_TEST_URL、总开关 AIDUMEI_HOOKS。
- 安装器把 AIDUMEM_USER_ID 显式写进用户级配置；重跑不悄悄换身份、丢白名单。
- Stop 钩子读完 stdin 立即返回（CC 2.1.322 hook_cancelled）。
"""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from scripts import cc_memory_sweep as S
from scripts import connect_host as C
from scripts.cc_memory_sweep import B
from tests.test_f04pp_cc_adaptation import (_CC_HOOKS, _capture_server, _hook_env, _make_transcript,
                                            _run_hook, _wait_for)
from tests.test_f08_cc_memory_bridge import FakeClient, _topic, cc  # noqa: F401

REPO = Path(__file__).resolve().parents[1]
INJECT = REPO / "integrations" / "claude-code" / "aidumem-cc-inject.sh"
_SWITCHES = ("AIDUMEI_HOOKS", "AIDUMEI_TEST_URL", "AIDUMEI_TEST_TOKEN", "AIDUMEI_CC_ALLOW_DIRS",
             "AIDUMEI_CC_MEMORY_ROOTS", "AIDUMEI_CC_HOOK_BG", "AIDUMEI_HOOK_INPUT")


def _clean_env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AIDUMEM_", "AIDUMEI_"))}
    env.update(extra)
    return env


# ═══════════════ R3-01：./.env 不决定身份 ═══════════════

def test_r3_01_cwd_env_must_not_choose_identity(cc, tmp_path, monkeypatch):  # noqa: F811
    user_env = tmp_path / "user.env"
    user_env.write_text("AIDUMEM_API_TOKEN=main-token\n")      # 典型安装：没有 AIDUMEM_USER_ID 这一行
    monkeypatch.setenv("AIDUMEM_ENV_FILE", str(user_env))
    evil = tmp_path / "evilrepo"
    evil.mkdir()
    (evil / ".env").write_text("AIDUMEM_USER_ID=victim\nAIDUMEI_BANK_ID=default\n")
    monkeypatch.chdir(evil)                                     # CC 以项目目录为 cwd 跑钩子
    ident = B.identity()
    assert ident["user_id"] != "victim"
    assert ident["token"] == "main-token"
    assert ident["explicit"] is False                           # 兜底 default 不算显式身份


def test_r3_01_cwd_env_must_not_pick_other_agents_key(cc, tmp_path, monkeypatch):  # noqa: F811
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "victim.key").write_text("amk_victim_key\n")
    monkeypatch.setenv("AIDUMEI_KEYS_DIR", str(keys))
    evil = tmp_path / "evilrepo"
    evil.mkdir()
    (evil / ".env").write_text("AIDUMEM_USER_ID=victim\n")
    monkeypatch.chdir(evil)
    assert B.identity()["token"] != "amk_victim_key"


def _victim_stub():
    seen = []

    class H(http.server.BaseHTTPRequestHandler):
        def _reply(self, obj):
            body = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            seen.append(("GET", self.path, self.headers.get("Authorization")))
            self._reply({"status": "ok", "blocks": []})

        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            seen.append(("POST", self.path, self.headers.get("Authorization"), body.get("user_id")))
            mem = "VICTIM_SECRET_MEMORY" if self.headers.get("Authorization") == "Bearer amk_victim_key" \
                else "own memory"
            self._reply({"results": [{"memory": mem, "score": 0.9, "id": "m1"}]})

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


def _victim_home(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    (home / ".aidumei-keys").mkdir(parents=True)
    (home / ".aidumei-keys" / "victim.key").write_text("amk_victim_key\n")
    os.chmod(home / ".aidumei-keys" / "victim.key", 0o600)
    (home / ".aidumem").mkdir()
    (home / ".aidumem" / ".env").write_text("AIDUMEM_API_TOKEN=main-token\n")   # 没写 AIDUMEM_USER_ID
    evil = tmp_path / "evilrepo"
    evil.mkdir()
    (evil / ".env").write_text("AIDUMEM_USER_ID=victim\n")
    return home, evil


def test_r3_01_inject_must_not_read_as_cwd_env_identity(tmp_path):
    srv, seen = _victim_stub()
    home, evil = _victim_home(tmp_path)
    env = _clean_env(HOME=str(home), AIDUMEM_URL=f"http://127.0.0.1:{srv.server_address[1]}",
                     TMPDIR=str(tmp_path))
    payload = json.dumps({"prompt": "what do you remember about my plans", "session_id": "s1", "cwd": str(evil),
                          "hook_event_name": "UserPromptSubmit"})
    try:
        r = subprocess.run(["bash", str(INJECT)], input=payload, capture_output=True, text=True, cwd=evil,
                           env=env, timeout=30)
    finally:
        srv.shutdown()
    assert "VICTIM_SECRET_MEMORY" not in r.stdout
    assert all("amk_victim_key" not in str(x) for x in seen)
    assert all(x[3] != "victim" for x in seen if x[0] == "POST")


def test_r3_01_ingest_must_not_write_as_cwd_env_identity(tmp_path):
    srv, seen = _victim_stub()
    home, evil = _victim_home(tmp_path)
    tp = _make_transcript(tmp_path, "hello", "hi")
    raw = json.dumps({"session_id": "s1", "transcript_path": str(tp), "cwd": str(evil)})
    env = _clean_env(HOME=str(home), AIDUMEM_URL=f"http://127.0.0.1:{srv.server_address[1]}",
                     AIDUMEM_LOG_DIR=str(tmp_path / "logs"), AIDUMEI_CC_HOOK_BG="1", AIDUMEI_HOOK_INPUT=raw)
    try:
        subprocess.run(["bash", str(_CC_HOOKS / "aidumem-cc-ingest.sh")], input=raw, capture_output=True,
                       text=True, cwd=evil, env=env, timeout=30)
    finally:
        srv.shutdown()
    adds = [x for x in seen if x[0] == "POST" and x[1] == "/add"]
    assert adds, "前台模式下写线应该写一条（身份兜底 default）"
    assert all(x[2] == "Bearer main-token" and x[3] == "default" for x in adds), adds


def test_r3_01_no_hook_lists_dot_env_in_cwd():
    """所有宿主钩子的 .env 候选链里都不再有 ./.env（结构守卫，防回退）。"""
    bad = []
    for f in (REPO / "integrations").rglob("*.sh"):
        src = f.read_text(encoding="utf-8")
        if '"./.env"' in src:
            bad.append(str(f.relative_to(REPO)))
    for f in (REPO / "integrations").rglob("*.py"):
        src = f.read_text(encoding="utf-8")
        if '\n                 ".env"' in src or '\n            ".env",' in src or ', ".env"):' in src:
            bad.append(str(f.relative_to(REPO)))
    assert not bad, bad


# ═══════════════ R3-02：项目级 autoMemoryDirectory ═══════════════

def test_r3_02_home_must_not_become_memory_root(cc, tmp_path):  # noqa: F811
    repo = tmp_path / "evilrepo"
    (repo / ".claude").mkdir(parents=True)
    (repo / ".claude" / "settings.local.json").write_text(json.dumps({"autoMemoryDirectory": "~/"}))
    notes = tmp_path / "otherproject" / "NOTES.md"
    notes.parent.mkdir()
    notes.write_text("# Notes\n\nordinary project notes, nothing to do with memory\n")
    payload = {"session_id": "s1", "cwd": str(repo), "hook_event_name": "PostToolUse",
               "tool_name": "Write", "tool_input": {"file_path": str(notes)}}
    client = FakeClient()
    res = B.run_hook(payload, client=client)
    assert res.get("action") != "ingested"
    assert not client.adds


def test_r3_02_project_local_outside_whitelist_ignored_even_if_safe_looking(cc, tmp_path, monkeypatch):  # noqa: F811
    repo = tmp_path / "proj"
    (repo / ".claude").mkdir(parents=True)
    other = tmp_path / "elsewhere-mem"
    other.mkdir()
    (repo / ".claude" / "settings.local.json").write_text(json.dumps({"autoMemoryDirectory": str(other)}))
    assert B.auto_memory_setting(str(repo)) is None
    monkeypatch.setenv("AIDUMEI_CC_MEMORY_ROOTS", str(tmp_path / "allowed-roots"))
    assert B.auto_memory_setting(str(repo)) is None
    monkeypatch.setenv("AIDUMEI_CC_MEMORY_ROOTS", f"{tmp_path / 'allowed-roots'}:{other}")
    assert B.auto_memory_setting(str(repo)) == other


def test_r3_02_whitelist_cannot_launder_home_or_project(cc, tmp_path, monkeypatch):  # noqa: F811
    """白名单写成家目录也不行：家目录、根目录、罩住项目的目录一律拒（用户级设置同样）。"""
    repo = cc["home"] / "work" / "proj"
    (repo / ".claude").mkdir(parents=True)
    monkeypatch.setenv("AIDUMEI_CC_MEMORY_ROOTS", str(cc["home"]))
    for v in ("~/", str(cc["home"] / "work"), str(repo), str(repo / "sub")):
        (repo / ".claude" / "settings.local.json").write_text(json.dumps({"autoMemoryDirectory": v}))
        assert B.auto_memory_setting(str(repo)) is None, v
    (repo / ".claude" / "settings.local.json").unlink()
    (cc["home"] / ".claude" / "settings.json").write_text(json.dumps({"autoMemoryDirectory": "~/"}))
    assert B.auto_memory_setting(str(repo)) is None


def test_r3_02_git_tracked_local_settings_ignored(cc, tmp_path, monkeypatch):  # noqa: F811
    repo = tmp_path / "tracked"
    (repo / ".claude").mkdir(parents=True)
    mine = tmp_path / "mem-ok"
    mine.mkdir()
    monkeypatch.setenv("AIDUMEI_CC_MEMORY_ROOTS", str(mine))
    f = repo / ".claude" / "settings.local.json"
    f.write_text(json.dumps({"autoMemoryDirectory": str(mine)}))
    assert B.auto_memory_setting(str(repo)) == mine
    g = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
         "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env={**os.environ, **g})
    subprocess.run(["git", "-C", str(repo), "add", "-f", ".claude/settings.local.json"], check=True,
                   env={**os.environ, **g})
    assert B.auto_memory_setting(str(repo)) is None, "仓库自带（被 git 跟踪）的 local 设置不该采信"


# ═══════════════ P0-1：写入白名单 / 测试隔离 / 总开关 ═══════════════

def test_p0_bridge_outside_allowlist_redacts_but_does_not_ingest(cc, monkeypatch):  # noqa: F811
    f = _topic(cc["mem"], "a.md", "# 合成\n普通笔记\n")
    monkeypatch.setenv("AIDUMEI_CC_ALLOW_DIRS", "/nonexistent-allow")
    client = FakeClient()
    res = B.run_hook(cc["payload"](f), client=client)
    assert res["action"] == "redact_only" and res["reason"] == "dir_not_allowed"
    assert not client.adds


def test_p0_bridge_inside_allowlist_ingests(cc, tmp_path, monkeypatch):  # noqa: F811
    work = tmp_path / "work" / "demo"
    work.mkdir(parents=True)
    mem = cc["home"] / ".claude" / "projects" / B.project_slug(str(work)) / "memory"
    mem.mkdir(parents=True)
    f = _topic(mem, "a.md", "# 合成\n普通笔记\n")
    monkeypatch.setenv("AIDUMEI_CC_ALLOW_DIRS", str(tmp_path / "work"))
    pl = {"session_id": "s1", "cwd": str(work), "hook_event_name": "PostToolUse", "tool_name": "Write",
          "tool_input": {"file_path": str(f)}}
    client = FakeClient()
    assert B.run_hook(pl, client=client)["action"] == "ingested"


def test_p0_bridge_hooks_off(cc, monkeypatch):  # noqa: F811
    f = _topic(cc["mem"], "a.md", "# 合成\n普通笔记\n")
    monkeypatch.setenv("AIDUMEI_HOOKS", "off")
    client = FakeClient()
    assert B.run_hook(cc["payload"](f), client=client)["reason"] == "hooks_off"
    assert not client.adds


def test_p0_test_url_isolates_credentials(cc, tmp_path, monkeypatch):  # noqa: F811
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "agent-a.key").write_text("amk_prod_key\n")
    envf = tmp_path / "user.env"
    envf.write_text("AIDUMEM_API_TOKEN=prod-token\nAIDUMEM_USER_ID=agent-a\n")
    monkeypatch.setenv("AIDUMEI_KEYS_DIR", str(keys))
    monkeypatch.setenv("AIDUMEM_ENV_FILE", str(envf))
    monkeypatch.setenv("AIDUMEM_URL", "http://127.0.0.1:8767")
    assert B.identity()["token"] == "amk_prod_key"
    monkeypatch.setenv("AIDUMEI_TEST_URL", "http://127.0.0.1:18999/")
    ident = B.identity()
    assert ident["url"] == "http://127.0.0.1:18999" and ident["token"] == "" and ident["test"]
    monkeypatch.setenv("AIDUMEI_TEST_TOKEN", "t-only")
    assert B.identity()["token"] == "t-only"
    monkeypatch.setenv("AIDUMEI_CC_ALLOW_DIRS", "/nonexistent-allow")
    assert B.dir_allowed("/anywhere") is True        # 测试实例不受白名单限制


def test_p0_test_url_is_not_read_from_env_file(cc, tmp_path, monkeypatch):  # noqa: F811
    envf = tmp_path / "user.env"
    envf.write_text("AIDUMEI_TEST_URL=http://127.0.0.1:18999\n")
    monkeypatch.setenv("AIDUMEM_ENV_FILE", str(envf))
    assert B.test_url() == ""


def test_p0_inject_outside_allowlist_needs_explicit_identity(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    outside = tmp_path / "scratch"
    outside.mkdir()
    with _capture_server(lambda p: {"results": [{"memory": "合成记忆", "id": "m1"}]}) as (port, calls):
        base = _hook_env(port, tmp_path, AIDUMEI_CC_ALLOW_DIRS=str(work), TMPDIR=str(tmp_path))
        base.pop("AIDUMEM_USER_ID")
        pl = {"prompt": "我之前说过什么", "session_id": "s", "cwd": str(outside)}
        r = _run_hook(INJECT, pl, base)
        assert r.returncode == 0 and r.stdout == b""
        assert not any(p.startswith("/search") for p, _ in calls), "兜底身份 + 白名单外：读线不该去搜"
        r = _run_hook(INJECT, {**pl, "cwd": str(work)}, base)
        assert "合成记忆" in r.stdout.decode()
        r = _run_hook(INJECT, pl, {**base, "AIDUMEM_USER_ID": "agent-a"})
        assert "合成记忆" in r.stdout.decode(), "身份显式时白名单外仍可读"


def _ingest_fg(tmp_path, port, cwd, **extra):
    tp = _make_transcript(tmp_path, "合成提问", "合成回答")
    raw = json.dumps({"session_id": "s1", "transcript_path": str(tp), "cwd": str(cwd)})
    env = _hook_env(port, tmp_path, AIDUMEI_CC_HOOK_BG="1", AIDUMEI_HOOK_INPUT=raw, **extra)
    return subprocess.run(["bash", str(_CC_HOOKS / "aidumem-cc-ingest.sh")], input=raw.encode(),
                          capture_output=True, env={**_clean_env(), **env}, timeout=30)


def test_p0_ingest_respects_allowlist(tmp_path):
    work = tmp_path / "work"
    (work / "sub").mkdir(parents=True)
    with _capture_server(lambda p: {"status": "accepted"}) as (port, calls):
        _ingest_fg(tmp_path, port, tmp_path / "elsewhere", AIDUMEI_CC_ALLOW_DIRS=str(work))
        assert not any(p.startswith("/add") for p, _ in calls)
        _ingest_fg(tmp_path, port, work / "sub", AIDUMEI_CC_ALLOW_DIRS=f"/nonexistent:{work}")
        assert any(p.startswith("/add") for p, _ in calls)


def test_p0_ingest_allowlist_prefix_is_path_aware(tmp_path):
    """/x/work 不罩 /x/work2（按路径段比，不按字符串前缀）。"""
    work = tmp_path / "work"
    work.mkdir()
    (tmp_path / "work2").mkdir()
    with _capture_server(lambda p: {"status": "accepted"}) as (port, calls):
        _ingest_fg(tmp_path, port, tmp_path / "work2", AIDUMEI_CC_ALLOW_DIRS=str(work))
        assert not any(p.startswith("/add") for p, _ in calls)


def test_p0_ingest_test_url_routes_to_isolated_instance_without_prod_token(tmp_path):
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "test-cc-hook.key").write_text("amk_prod_key\n")
    with _capture_server(lambda p: {"status": "accepted"}) as (prod_port, prod_calls), \
            _capture_server(lambda p: {"status": "accepted"}) as (test_port, test_calls):
        r = _ingest_fg(tmp_path, prod_port, tmp_path, AIDUMEI_TEST_URL=f"http://127.0.0.1:{test_port}",
                       AIDUMEI_KEYS_DIR=str(keys), AIDUMEI_CC_ALLOW_DIRS="/nonexistent")
        assert r.returncode == 0
        assert not prod_calls, "测试开关开着，生产实例一个请求都不该收到"
        assert any(p.startswith("/add") for p, _ in test_calls)


def test_p0_hooks_off_switch_silences_all_cc_lines(tmp_path):
    with _capture_server(lambda p: {"status": "ok", "results": []}) as (port, calls):
        env = _hook_env(port, tmp_path, AIDUMEI_HOOKS="off", AIDUMEI_DISTILL_FOREGROUND="1")
        tp = _make_transcript(tmp_path, "a", "b")
        for script, pl in (("aidumem-cc-inject.sh", {"prompt": "hi", "session_id": "s"}),
                           ("aidumem-cc-ingest.sh", {"session_id": "s", "transcript_path": str(tp)}),
                           ("aidumem-cc-distill.sh", {"session_id": "s", "hook_event_name": "SessionEnd"})):
            assert _run_hook(_CC_HOOKS / script, pl, env).returncode == 0
        time.sleep(0.5)
        assert calls == []


def test_p0_distill_respects_allowlist(tmp_path):
    work = tmp_path / "work"
    work.mkdir()

    def route(p):
        return {"status": "skipped", "reason": "too_short"}
    with _capture_server(route) as (port, calls):
        env = _hook_env(port, tmp_path, AIDUMEI_DISTILL_FOREGROUND="1", AIDUMEI_CC_ALLOW_DIRS=str(work))
        _run_hook(_CC_HOOKS / "aidumem-cc-distill.sh",
                  {"session_id": "d1", "hook_event_name": "SessionEnd", "cwd": str(tmp_path)}, env, timeout=40)
        assert not any(p.startswith("/session/distill") for p, _ in calls)
        _run_hook(_CC_HOOKS / "aidumem-cc-distill.sh",
                  {"session_id": "d2", "hook_event_name": "SessionEnd", "cwd": str(work)}, env, timeout=40)
        assert _wait_for(calls, lambda p: p.startswith("/session/distill"))


def test_p0_sweep_skips_projects_outside_allowlist(cc, tmp_path, monkeypatch):  # noqa: F811
    work = tmp_path / "work"
    work.mkdir()
    ok_mem = cc["home"] / ".claude" / "projects" / B.project_slug(str(work / "app")) / "memory"
    ok_mem.mkdir(parents=True)
    _topic(ok_mem, "a.md", "笔记甲", title="合成甲")
    _topic(cc["mem"], "b.md", "笔记乙", title="合成乙")              # -work-demo：不在白名单
    monkeypatch.setenv("AIDUMEI_CC_ALLOW_DIRS", str(work))
    client = FakeClient()
    assert S.main(["--apply", "--json"], client=client) == 0
    titles = [a["metadata"]["cc_title"] for a in client.adds]
    assert titles == ["合成甲"], titles


# ═══════════════ 安装器：身份写死进用户级配置 ═══════════════

def test_installer_pins_user_id_and_allow_dirs(tmp_path):
    cmd = C.hook_command("claude-code", "aidumem-cc-inject.sh", C.DEFAULT_URL, tmp_path / ".env",
                         user_id="agent-a", allow_dirs="/w/a:/w/b")
    assert "AIDUMEM_USER_ID=agent-a" in cmd and "AIDUMEI_CC_ALLOW_DIRS=/w/a:/w/b" in cmd
    cx = C.hook_command("codex", "aidumem-codex-inject.sh", C.DEFAULT_URL, tmp_path / ".env",
                        user_id="agent-a", allow_dirs="/w/a")
    assert "AIDUMEM_USER_ID=agent-a" in cx and "AIDUMEI_CC_ALLOW_DIRS" not in cx


@pytest.fixture()
def chome(tmp_path, monkeypatch):
    home = tmp_path / "h"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    for k in ("CLAUDE_CONFIG_DIR", "AIDUMEM_USER_ID", "AIDUMEM_DEFAULT_USER_ID", *_SWITCHES):
        monkeypatch.delenv(k, raising=False)
    return home


def test_installer_writes_identity_from_env_file_and_keeps_it_on_rerun(chome, monkeypatch, capsys):
    envf = chome / "svc.env"
    envf.write_text("AIDUMEM_DEFAULT_USER_ID=agent-a\n")
    cfgp = chome / ".claude" / "settings.json"
    assert C.main(["--host", "claude-code", "--url", "http://127.0.0.1:9", "--env-file", str(envf),
                   "--dry-run", "--json"]) == 0
    capsys.readouterr()
    C.main(["--host", "claude-code", "--url", "http://127.0.0.1:9", "--env-file", str(envf), "--no-selftest",
            "--allow-dir", str(chome)])
    capsys.readouterr()
    cmds = [h["command"] for gs in json.loads(cfgp.read_text())["hooks"].values() for g in gs for h in g["hooks"]]
    assert cmds and all("AIDUMEM_USER_ID=agent-a" in c for c in cmds)
    assert all(f"AIDUMEI_CC_ALLOW_DIRS={chome.resolve()}" in c for c in cmds)
    # .env 里身份改了、重跑不给参数：沿用已装的身份与白名单，不悄悄换
    envf.write_text("AIDUMEM_DEFAULT_USER_ID=someone-else\n")
    C.main(["--host", "claude-code", "--url", "http://127.0.0.1:9", "--env-file", str(envf), "--no-selftest"])
    capsys.readouterr()
    cmds2 = [h["command"] for gs in json.loads(cfgp.read_text())["hooks"].values() for g in gs for h in g["hooks"]]
    assert cmds2 == cmds
    # 显式 --user-id 才换
    C.main(["--host", "claude-code", "--url", "http://127.0.0.1:9", "--env-file", str(envf), "--no-selftest",
            "--user-id", "agent-b"])
    capsys.readouterr()
    cmds3 = [h["command"] for gs in json.loads(cfgp.read_text())["hooks"].values() for g in gs for h in g["hooks"]]
    assert all("AIDUMEM_USER_ID=agent-b" in c for c in cmds3)


def test_installer_rejects_unsafe_user_id(chome, capsys):
    assert C.main(["--host", "claude-code", "--user-id", "../x", "--dry-run", "--no-selftest"]) == 2


def test_check_flags_unpinned_identity_and_test_switch_without_touching_config(chome, capsys):
    """老安装（命令里没写身份）+ .env 里误留测试开关：--check 只报告、不改配置、不因此判红。"""
    envf = chome / "svc.env"
    envf.write_text("AIDUMEI_TEST_URL=http://127.0.0.1:1\n")
    old, _ = C.merge_hooks({}, "claude-code", "http://127.0.0.1:9", envf)
    cfgp = chome / ".claude" / "settings.json"
    cfgp.write_text(json.dumps(old))
    C.main(["--host", "claude-code", "--url", "http://127.0.0.1:9", "--env-file", str(envf), "--check",
            "--no-selftest", "--json"])
    rep = json.loads(capsys.readouterr().out)
    assert rep["hosts"][0]["identity_pinned"] is False
    assert rep["hosts"][0]["lines"] and all(rep["hosts"][0]["lines"].values())
    assert any("AIDUMEI_TEST_URL" in w for w in rep["switches"]["warnings"])
    assert any("白名单" in w for w in rep["switches"]["warnings"])
    assert "http://127.0.0.1:1" not in json.dumps(rep)            # 只报设没设，不打印值
    assert json.loads(cfgp.read_text()) == old


def test_existing_assign_ignores_hand_written_case_commands():
    cfg = {"hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command":
           'case "$PWD" in */x/*) export AIDUMEM_USER_ID=x;; esac; AIDUMEM_ENV_FILE=/e /h/aidumem-cc-ingest.sh'}]}]}}
    assert C._existing_assign(cfg, "claude-code", "AIDUMEM_USER_ID") == ""
    cfg2 = {"hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command":
            "AIDUMEI_INSTALL_DIR=/r AIDUMEM_USER_ID=agent-a bash /r/aidumem-cc-ingest.sh"}]}]}}
    assert C._existing_assign(cfg2, "claude-code", "AIDUMEM_USER_ID") == "agent-a"


# ═══════════════ Stop 钩子立即返回 ═══════════════

def test_stop_hook_returns_before_reading_transcript(tmp_path):
    """transcript 是个还没人写的 FIFO：旧版前台解析会卡死在 open；现在钩子必须立刻返回，
    后台进程等到内容后照常写入。"""
    fifo = tmp_path / "transcript.jsonl"
    os.mkfifo(fifo)
    with _capture_server(lambda p: {"status": "accepted"}) as (port, calls):
        env = _hook_env(port, tmp_path)
        t0 = time.monotonic()
        r = _run_hook(_CC_HOOKS / "aidumem-cc-ingest.sh",
                      {"session_id": "fast", "transcript_path": str(fifo), "hook_event_name": "Stop"}, env,
                      timeout=10)
        took = time.monotonic() - t0
        assert r.returncode == 0 and took < 3, took
        assert not calls
        lines = [json.dumps({"type": "user", "message": {"role": "user", "content": "后台合成提问"}},
                            ensure_ascii=False)]
        with open(fifo, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        assert _wait_for(calls, lambda p: p.startswith("/add"), tries=100)
    body = json.loads(next(b for p, b in calls if p.startswith("/add")).decode())
    assert body["messages"][0]["content"] == "后台合成提问"
    assert body["metadata"]["_origin_session_id"] == "cc-fast"
