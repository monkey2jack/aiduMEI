"""f0.7 P3：connect_host 一条命令接宿主 + 当场验收（网友反馈「装完不挂载」）。"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from scripts import connect_host as C


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for k in ("AIDUMEM_API_TOKEN", "AIDUMEM_USER_ID", "AIDUMEM_DEFAULT_USER_ID", "AIDUMEI_BANK_ID", "AIDUMEM_URL", "AIDUMEI_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AIDUMEM_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


class _Mem(BaseHTTPRequestHandler):
    """最小服务桩：/add 存原话，/search 子串命中，/delete 真删。"""
    store: list[dict] = []
    calls: list[str] = []

    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self.calls.append("GET " + self.path.split("?")[0])
        if self.path.startswith("/health"):
            return self._send({"status": "ok", "health_status": "ok", "version": "stub"})
        return self._send({"blocks": []})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        self.calls.append("POST " + self.path)
        if self.path == "/add":
            for m in body.get("messages") or []:
                if m.get("role") == "user":
                    self.store.append({"id": f"m{len(self.store)}", "memory": m["content"]})
            return self._send({"status": "queued"})
        if self.path == "/search":
            q = body.get("query", "")
            hits = [m for m in self.store if any(t in m["memory"] for t in (q, "暗号")) and ("暗号" in q or q in m["memory"])]
            return self._send({"results": hits, "recall_verdict": "found" if hits else "not_found"})
        if self.path == "/delete":
            self.store[:] = [m for m in self.store if m["id"] != body.get("memory_id")]
            return self._send({"status": "deleted"})
        return self._send({}, 404)


@pytest.fixture()
def stub():
    _Mem.store, _Mem.calls = [], []
    srv = HTTPServer(("127.0.0.1", 0), _Mem)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _cc(home: Path) -> Path:
    return home / ".claude" / "settings.json"


def test_merge_is_idempotent_and_keeps_foreign_hooks(home):
    foreign = {"type": "command", "command": "/opt/other/gate.sh", "timeout": 5}
    cfg = {"env": {"X": "1"}, "hooks": {"UserPromptSubmit": [{"matcher": "", "hooks": [foreign]}]}}
    env = home / ".env"
    new, ch = C.merge_hooks(cfg, "claude-code", C.DEFAULT_URL, env)
    assert len(ch) == 3 and all(C.wired_lines(new, "claude-code").values())
    assert foreign in new["hooks"]["UserPromptSubmit"][0]["hooks"] and new["env"] == {"X": "1"}
    again, ch2 = C.merge_hooks(new, "claude-code", C.DEFAULT_URL, env)
    assert ch2 == [] and again == new                                   # 幂等
    moved, ch3 = C.merge_hooks(new, "claude-code", "http://10.1.2.3:9000", env)
    assert ch3 and "AIDUMEI_URL=http://10.1.2.3:9000" in json.dumps(moved)   # 换地址只更新本仓条目
    off, _ = C.merge_hooks(moved, "claude-code", C.DEFAULT_URL, env, remove=True)
    assert off == {"env": {"X": "1"}, "hooks": {"UserPromptSubmit": [{"matcher": "", "hooks": [foreign]}]}}


def test_commands_point_at_repo_sources_not_copies(home):
    cmd = C.hook_command("claude-code", "aidumem-cc-ingest.sh", C.DEFAULT_URL, home / ".env")
    assert str(C.REPO / "integrations/claude-code/aidumem-cc-ingest.sh") in cmd
    assert "AIDUMEI_INSTALL_DIR=" in cmd and "_URL=" not in cmd     # 默认地址不写死


def test_dry_run_touches_nothing(home, stub):
    (home / ".claude").mkdir()
    rc = C.main(["--host", "claude-code", "--dry-run", "--url", stub])
    assert rc == 0 and not _cc(home).exists()


def test_broken_host_config_is_never_overwritten(home, stub, capsys):
    (home / ".claude").mkdir()
    _cc(home).write_text("{ not json", encoding="utf-8")
    rc = C.main(["--host", "claude-code", "--url", stub, "--no-selftest"])
    assert rc == 1 and _cc(home).read_text(encoding="utf-8") == "{ not json"
    assert capsys.readouterr().out.splitlines()[0] == "服务 ✅ · 宿主接入 ❌"


def test_connect_backs_up_then_selftest_recalls_and_cleans(home, stub, capsys):
    (home / ".claude").mkdir()
    _cc(home).write_text(json.dumps({"env": {"K": "v"}}), encoding="utf-8")
    rc = C.main(["--host", "claude-code", "--url", stub, "--wait", "15", "--env-file", str(home / ".env")])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert out.splitlines()[0] == "服务 ✅ · 宿主接入 ✅"
    cfg = json.loads(_cc(home).read_text(encoding="utf-8"))
    assert cfg["env"] == {"K": "v"} and all(C.wired_lines(cfg, "claude-code").values())
    assert list((home / ".claude").glob("settings.json.bak-aidumei-*"))   # 先备份
    assert "POST /add" in _Mem.calls and "POST /delete" in _Mem.calls
    assert not any("AIDU-" in m["memory"] for m in _Mem.store)          # 测试记忆已清理


def test_not_wired_is_red_with_nonzero_exit(home, stub, capsys):
    (home / ".claude").mkdir()
    rc = C.main(["--host", "claude-code", "--url", stub, "--check"])
    assert rc == 1 and capsys.readouterr().out.splitlines()[0] == "服务 ✅ · 宿主接入 ❌"


def test_service_down_is_red(home, capsys):
    (home / ".claude").mkdir()
    rc = C.main(["--host", "claude-code", "--url", "http://127.0.0.1:9", "--no-selftest"])
    assert rc == 1 and capsys.readouterr().out.splitlines()[0].startswith("服务 ❌")


def test_no_host_detected_is_red(home, stub, monkeypatch, capsys):
    monkeypatch.setattr(C, "detect_hosts", lambda: [])
    rc = C.main(["--url", stub])
    assert rc == 1 and "宿主接入 ❌" in capsys.readouterr().out.splitlines()[0]


def test_uninstall_removes_only_ours(home, stub):
    (home / ".claude").mkdir()
    C.main(["--host", "claude-code", "--url", stub, "--no-selftest"])
    assert C.main(["--host", "claude-code", "--uninstall", "--url", stub]) == 0
    assert "hooks" not in json.loads(_cc(home).read_text(encoding="utf-8"))


def test_hermes_plugin_is_symlinked_not_copied(home, monkeypatch):
    monkeypatch.setattr(C.shutil, "which", lambda name: None)
    r = C.connect_hermes(dry=False, remove=False)
    link = home / ".hermes" / "plugins" / "aidumem"
    assert link.is_symlink() and link.resolve() == (C.REPO / C.HERMES_PLUGIN).resolve()
    assert r["ok"] is False and "hermes" in r["error"]                  # 没 hermes 命令 = 不算接上


def test_host_wiring_probe(tmp_path, monkeypatch):
    from ducky import host_wiring as H
    monkeypatch.setattr(H, "_path", lambda: str(tmp_path / "hw.json"))
    monkeypatch.setattr(H, "_state", {"last_at": None, "agents": {}, "flushed_at": 0.0})
    probe, warn = H.status(started_at=0, now=10)
    assert probe["last_host_write_at"] is None and warn is None           # 刚起服务不报
    probe, warn = H.status(started_at=0, now=H.STALE_AFTER_S + 1)
    assert warn and "connect_host" in warn                                 # 30 分钟没写入 → 告警
    from ducky.origin_context import reset_origin, set_origin_from_metadata
    reset_origin(set_origin_from_metadata({"_origin_agent": "aidumem-cc-ingest"}))
    probe, warn = H.status(started_at=0, now=H.STALE_AFTER_S + 1)
    assert probe["agents"] == ["aidumem-cc-ingest"] and warn is None
    assert json.loads((tmp_path / "hw.json").read_text())["agents"]       # 落盘，重启可读


@pytest.mark.parametrize("kernel", ["native", "default"])
def test_native_local_mode_write_is_recallable(tmp_path, kernel):
    """实验机从零安装实测:native + AIDUMEI_ENGINE_MODE=local 时 /add 落不进任何可召回的库。"""
    import os
    import subprocess
    import sys
    env = {**os.environ, "TMPDIR": str(tmp_path), "AIDUMEM_LOG_DIR": str(tmp_path)}
    p = subprocess.run([sys.executable, str(C.REPO / "tests" / "_f07_p3_local_probe.py"), kernel], capture_output=True,
                       text=True, env=env, timeout=110, cwd=str(tmp_path))
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert out == {"add": "direct", "mode": "local", "hit": True}, p.stderr[-800:]


def test_local_tier_defaults_to_native_kernel(monkeypatch):
    from ducky.memory_kernel import configured_kernel
    monkeypatch.delenv("AIDUMEI_MEMORY_KERNEL", raising=False)
    import ducky.memory_kernel as mk
    monkeypatch.setattr(mk, "_legacy_mem0_unmigrated", lambda: False)   # 全新部署（无 mem0 旧数据）
    # f0.7 WP3b：默认内核改 native（之前 auto/cloud/未设 → mem0）
    for mode, want in (("local", "native"), ("auto", "native"), ("cloud", "native"), ("", "native")):
        monkeypatch.setenv("AIDUMEI_ENGINE_MODE", mode)
        assert configured_kernel() == want, mode
    monkeypatch.setenv("AIDUMEI_ENGINE_MODE", "local")
    monkeypatch.setenv("AIDUMEI_MEMORY_KERNEL", "mem0")
    assert configured_kernel() == "mem0"                                  # 显式选择永远优先


def test_wp3b_unmigrated_mem0_deployment_stays_on_mem0(monkeypatch, tmp_path):
    """f0.7 WP3b：老 mem0 部署升级上来（mem0 历史库有数据、没有 memory.db）不能被默认 native 换掉内核
    —— 否则升级后记忆「凭空消失」；有了 memory.db（已迁移）或全新部署 → native。"""
    import sqlite3
    import ducky.memory_kernel as mk
    import ducky.mem0_runtime as mr
    import ducky.utils as u
    monkeypatch.delenv("AIDUMEI_MEMORY_KERNEL", raising=False)
    monkeypatch.setenv("AIDUMEI_ENGINE_MODE", "auto")
    monkeypatch.setattr(u, "DATA_DIR", str(tmp_path))
    hist = tmp_path / "history.db"
    monkeypatch.setattr(mr, "get_history_db_path", lambda: str(hist))
    assert mk.configured_kernel() == "native"                 # 全新：无历史库
    c = sqlite3.connect(hist)
    c.execute("CREATE TABLE history (id TEXT)")
    c.commit()
    assert mk.configured_kernel() == "native"                 # 历史库空
    c.execute("INSERT INTO history VALUES ('x')")
    c.commit()
    c.close()
    assert mk.configured_kernel() == "mem0"                   # 有旧数据、未迁移 → 留在 mem0
    (tmp_path / "memory.db").write_bytes(b"")
    assert mk.configured_kernel() == "native"                 # 已迁移
    monkeypatch.setenv("AIDUMEI_ENGINE_MODE", "local")
    (tmp_path / "memory.db").unlink()
    assert mk.configured_kernel() == "native"                 # 本地档永远 native


def test_wp3b_local_tier_restore_brings_back_native_memory(tmp_path):
    """f0.7 WP3b（实验机无 mem0 从零实测）：本地档删了再从墓碑恢复，native 记忆本体必须回来。"""
    import os
    import subprocess
    import sys
    env = {**os.environ, "TMPDIR": str(tmp_path), "AIDUMEM_LOG_DIR": str(tmp_path)}
    p = subprocess.run([sys.executable, str(C.REPO / "tests" / "_f07_p3_local_probe.py"), "default", "restore"],
                       capture_output=True, text=True, env=env, timeout=110, cwd=str(tmp_path))
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert out == {"add": "direct", "mode": "local", "hit": True, "deleted": "committed",
                   "restored": "ok", "native_row": True}, p.stderr[-800:]
