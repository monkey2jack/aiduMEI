"""f0.8 ①：Claude Code 自带记忆桥——路径判定、原地打码、入库载荷、去重与取代。"""
from __future__ import annotations

import io
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from scripts.cc_memory_sweep import B

SECRET = "api_key = sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"


@pytest.fixture()
def cc(tmp_path, monkeypatch):
    """假 HOME：~/.claude/projects/<项目>/memory/ + 状态文件 + 干净环境。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    for k in ("CLAUDE_CONFIG_DIR", "AIDUMEI_CC_MEMORY_DIR", "AIDUMEM_API_TOKEN", "AIDUMEM_USER_ID",
              "AIDUMEM_DEFAULT_USER_ID", "AIDUMEI_BANK_ID", "AIDUMEM_URL", "AIDUMEI_URL", "AIDUMEM_ENV_FILE",
              "AIDUMEI_CC_BRIDGE_INGEST", "AIDUMEI_SECRET_GUARD", "AIDUMEI_CC_BRIDGE_MAX_CHARS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AIDUMEI_KEYS_DIR", str(tmp_path / "nokeys"))
    monkeypatch.setenv("AIDUMEM_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("AIDUMEI_CC_BRIDGE_STATE", str(tmp_path / "state.json"))
    proj = tmp_path / ".claude" / "projects" / "-work-demo"
    mem = proj / "memory"
    mem.mkdir(parents=True)
    return {"home": tmp_path, "proj": proj, "mem": mem,
            "payload": lambda fp, tool="Write": {
                "session_id": "s1", "transcript_path": str(proj / "s1.jsonl"), "cwd": "/work/demo",
                "hook_event_name": "PostToolUse", "tool_name": tool, "tool_input": {"file_path": str(fp)}}}


class FakeClient:
    """内存服务桩：/add 存一条（带 metadata），/search 全量返回，/delete 真删。"""

    def __init__(self, add_code=200):
        self.ident = {"user_id": "agent-a", "bank_id": "default", "url": "x", "token": ""}
        self.store: list[dict] = []
        self.adds: list[dict] = []
        self.add_code = add_code

    def add(self, content, metadata, idem):
        self.adds.append({"content": content, "metadata": metadata, "idem": idem})
        if self.add_code != 200:
            return self.add_code, {"detail": {"reason": "oversized_raw_copy"}}
        self.store.append({"id": f"m{len(self.adds)}", "memory": content, "metadata": dict(metadata)})
        return 200, {"status": "ok"}

    def search(self, query, limit=20):
        return list(self.store)[:limit]

    def delete(self, mid):
        before = len(self.store)
        self.store[:] = [m for m in self.store if m["id"] != mid]
        return len(self.store) < before


def _topic(mem: Path, name="user_prefs.md", body="用户喜欢简洁的回答。", title="user prefs"):
    p = mem / name
    p.write_text(f"---\nname: {title}\ndescription: 偏好\ntype: user\n---\n\n{body}\n", encoding="utf-8")
    return p


# ── 路径判定 ────────────────────────────────────────────────────────────
def test_target_inside_memory_dir(cc):
    p = _topic(cc["mem"])
    assert B.hook_target(cc["payload"](p)) == (p.resolve(), cc["mem"].resolve())


@pytest.mark.parametrize("tool", ["Read", "Bash", "NotebookEdit", None])
def test_non_write_tools_ignored(cc, tool):
    p = _topic(cc["mem"])
    assert B.hook_target(cc["payload"](p, tool=tool)) is None


def test_files_outside_memory_dir_ignored(cc):
    other = cc["proj"] / "notes.md"
    other.write_text("x")
    elsewhere = cc["home"] / "memory" / "a.md"
    elsewhere.parent.mkdir()
    elsewhere.write_text("x")
    for fp in (other, elsewhere, cc["proj"] / "s1.jsonl"):
        assert B.hook_target(cc["payload"](fp)) is None


def test_other_projects_memory_dir_ignored(cc):
    other = cc["home"] / ".claude" / "projects" / "-work-other" / "memory"
    other.mkdir(parents=True)
    p = _topic(other)
    assert B.hook_target(cc["payload"](p)) is None


def test_path_traversal_rejected(cc):
    outside = cc["home"] / "secret.md"
    outside.write_text("x")
    trav = str(cc["mem"]) + "/../../../../secret.md"
    assert B.resolve_inside(trav, cc["mem"]) is None
    assert B.hook_target(cc["payload"](trav)) is None
    assert B.resolve_inside("memory/a.md", cc["mem"]) is None  # 相对路径


def test_symlink_file_escape_rejected(cc):
    outside = cc["home"] / "victim.md"
    outside.write_text("keep me " + SECRET)
    link = cc["mem"] / "evil.md"
    link.symlink_to(outside)
    assert B.hook_target(cc["payload"](link)) is None
    B.run_hook(cc["payload"](link), client=FakeClient())
    assert SECRET in outside.read_text()  # 外部文件原样


def test_symlink_dir_escape_rejected(cc):
    outdir = cc["home"] / "elsewhere"
    outdir.mkdir()
    (outdir / "x.md").write_text(SECRET)
    (cc["mem"] / "sub").symlink_to(outdir)
    assert B.resolve_inside(cc["mem"] / "sub" / "x.md", cc["mem"]) is None


def test_symlinked_memory_root_rejected(cc, tmp_path):
    real = tmp_path / "realmem"
    real.mkdir()
    (real / "a.md").write_text("x")
    link_root = cc["proj"] / "memory2"
    link_root.symlink_to(real)
    assert B.resolve_inside(link_root / "a.md", link_root) is None


def test_non_md_and_dirs_rejected(cc):
    (cc["mem"] / "a.txt").write_text("x")
    (cc["mem"] / "d.md").mkdir()
    assert B.resolve_inside(cc["mem"] / "a.txt", cc["mem"]) is None
    assert B.resolve_inside(cc["mem"] / "d.md", cc["mem"]) is None


def test_memory_dir_from_cwd_when_no_transcript(cc):
    pl = cc["payload"](cc["mem"] / "a.md")
    pl.pop("transcript_path")
    assert B.memory_dir_for(pl) == cc["mem"]


def test_transcript_outside_projects_falls_back_to_cwd(cc):
    pl = cc["payload"](cc["mem"] / "a.md")
    pl["transcript_path"] = "/tmp/x/y.jsonl"
    assert B.memory_dir_for(pl) == cc["mem"]


def test_memory_dir_env_override_and_claude_config_dir(cc, tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    pl = cc["payload"](cc["mem"] / "a.md")
    pl.pop("transcript_path")
    assert B.memory_dir_for(pl) == tmp_path / "cfg" / "projects" / "-work-demo" / "memory"
    monkeypatch.setenv("AIDUMEI_CC_MEMORY_DIR", str(cc["mem"]))
    assert B.memory_dir_for(pl) == cc["mem"]


# ── 原地打码 ────────────────────────────────────────────────────────────
def test_redaction_rewrites_in_place_and_keeps_mode(cc):
    p = _topic(cc["mem"], body="数据库连接：" + SECRET + "\n其他内容保留。")
    os.chmod(p, 0o640)
    ino_before = p.stat().st_ino
    res = B.run_hook(cc["payload"](p), client=FakeClient())
    txt = p.read_text(encoding="utf-8")
    assert "sk-proj-" not in txt and "[redacted:" in txt
    assert "其他内容保留。" in txt and txt.startswith("---\nname: user prefs")
    assert oct(p.stat().st_mode & 0o777) == oct(0o640)
    assert p.stat().st_ino != ino_before  # 原子替换（新 inode）
    assert res["redacted"]
    assert not [f for f in os.listdir(cc["mem"]) if f.endswith(".tmp")]


def test_clean_file_untouched(cc):
    p = _topic(cc["mem"])
    ino, mtime = p.stat().st_ino, p.stat().st_mtime_ns
    B.run_hook(cc["payload"](p), client=FakeClient())
    assert (p.stat().st_ino, p.stat().st_mtime_ns) == (ino, mtime)


def test_index_is_redacted_but_not_ingested(cc):
    idx = cc["mem"] / "MEMORY.md"
    idx.write_text("- [prefs](user_prefs.md) — " + SECRET + "\n", encoding="utf-8")
    fc = FakeClient()
    res = B.run_hook(cc["payload"](idx, tool="Edit"), client=fc)
    assert "sk-proj-" not in idx.read_text()
    assert res["reason"] == "index" and fc.adds == []


def test_redact_only_switch(cc, monkeypatch):
    monkeypatch.setenv("AIDUMEI_CC_BRIDGE_INGEST", "0")
    p = _topic(cc["mem"], body=SECRET)
    fc = FakeClient()
    res = B.run_hook(cc["payload"](p), client=fc)
    assert res["action"] == "redact_only" and fc.adds == [] and "sk-proj-" not in p.read_text()


def test_hook_stdout_tells_model_about_redaction(cc, monkeypatch, capsys):
    p = _topic(cc["mem"], body=SECRET)
    monkeypatch.setenv("AIDUMEI_CC_BRIDGE_INGEST", "0")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(cc["payload"](p))))
    assert B.main(["hook"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert "打码" in out["hookSpecificOutput"]["additionalContext"]
    assert "sk-proj-" not in json.dumps(out)


def test_hook_never_fails_on_garbage(cc, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert B.main(["hook"]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": "Write", "tool_input": "x"})))
    assert B.main(["hook"]) == 0
    assert capsys.readouterr().out == ""


# ── 入库载荷 ────────────────────────────────────────────────────────────
def test_ingest_payload_shape(cc):
    p = _topic(cc["mem"], body="喜欢中文回复，不要长篇。")
    fc = FakeClient()
    res = B.run_hook(cc["payload"](p), client=fc)
    assert res["action"] == "ingested"
    a = fc.adds[0]
    md = a["metadata"]
    assert md["source"] == "cc_auto_memory" and md["cc_file"] == "user_prefs.md"
    assert md["cc_title"] == "user prefs" and md["cc_type"] == "user"
    assert md["cc_project"] == "-work-demo" and len(md["cc_sha"]) == 32
    assert md["_origin_session_id"] == "cc-s1" and md["_origin_agent"] == B.AGENT
    assert a["content"].startswith("[Claude Code 记忆 · user prefs]\n偏好")
    assert "喜欢中文回复" in a["content"] and "---" not in a["content"]
    assert a["idem"].startswith("ccmem-")


def test_ingested_content_is_the_redacted_text(cc):
    p = _topic(cc["mem"], body="key " + SECRET)
    fc = FakeClient()
    B.run_hook(cc["payload"](p), client=fc)
    assert "sk-proj-" not in json.dumps(fc.adds, ensure_ascii=False)


def test_long_file_truncated_with_flag(cc, monkeypatch):
    monkeypatch.setenv("AIDUMEI_CC_BRIDGE_MAX_CHARS", "600")
    p = _topic(cc["mem"], body="长" * 2000)
    fc = FakeClient()
    B.run_hook(cc["payload"](p), client=fc)
    assert fc.adds[0]["metadata"]["cc_truncated"] == 1 and len(fc.adds[0]["content"]) < 700


def test_title_fallbacks(tmp_path):
    p = tmp_path / "some_topic.md"
    assert B.file_title(p, "# 标题一\n正文") == "标题一"
    assert B.file_title(p, "正文") == "some_topic"


def test_wire_request_has_caller_and_bearer(cc, monkeypatch):
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, body, self.headers.get("Authorization")))
            b = json.dumps({"status": "ok", "results": []}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("AIDUMEM_URL", f"http://127.0.0.1:{srv.server_port}")
        monkeypatch.setenv("AIDUMEM_USER_ID", "agent-a")
        monkeypatch.setenv("AIDUMEM_API_TOKEN", "tok-test")
        p = _topic(cc["mem"])
        res = B.run_hook(cc["payload"](p))
    finally:
        srv.shutdown()
    assert res["action"] == "ingested"
    adds = [b for path, b, _ in seen if path == "/add"]
    assert adds and adds[0]["user_id"] == "agent-a" and adds[0]["caller_user_id"] == "agent-a"
    assert adds[0]["idempotency_key"].startswith("ccmem-")
    assert all(auth == "Bearer tok-test" for _, _, auth in seen)
    # f0.8.0 上线实测：生产 /search 4–6 秒，写死 5 秒会让导入整体超时；缺省 12 秒、可配、非法值回退
    monkeypatch.delenv("AIDUMEI_CC_BRIDGE_TIMEOUT", raising=False)
    assert B.Client({}).timeout == 12.0
    monkeypatch.setenv("AIDUMEI_CC_BRIDGE_TIMEOUT", "20")
    assert B.Client({}).timeout == 20.0
    monkeypatch.setenv("AIDUMEI_CC_BRIDGE_TIMEOUT", "abc")
    assert B.Client({}).timeout == 12.0
    assert B.Client({}, timeout=3).timeout == 3.0


def test_agent_key_file_overrides_token(cc, monkeypatch):
    kd = cc["home"] / "keys"
    kd.mkdir()
    (kd / "agent-a.key").write_text("per-agent-key\n")
    monkeypatch.setenv("AIDUMEI_KEYS_DIR", str(kd))
    monkeypatch.setenv("AIDUMEM_USER_ID", "agent-a")
    monkeypatch.setenv("AIDUMEM_API_TOKEN", "shared")
    assert B.identity()["token"] == "per-agent-key"
    monkeypatch.setenv("AIDUMEM_USER_ID", "../agent-a")
    assert B.identity()["token"] == "shared"


# ── 去重与取代 ──────────────────────────────────────────────────────────
def test_resave_same_content_is_skipped(cc):
    p = _topic(cc["mem"])
    fc = FakeClient()
    B.run_hook(cc["payload"](p), client=fc)
    p.write_text(p.read_text() + "\n\n")  # 只多了空白：哈希按规范化正文算
    res = B.run_hook(cc["payload"](p, tool="Edit"), client=fc)
    assert res["reason"] == "unchanged" and len(fc.adds) == 1


def test_duplicate_on_server_skipped_without_state(cc):
    p = _topic(cc["mem"])
    fc = FakeClient()
    B.run_hook(cc["payload"](p), client=fc)
    os.unlink(os.environ["AIDUMEI_CC_BRIDGE_STATE"])  # 状态丢了：靠服务端同哈希去重
    res = B.run_hook(cc["payload"](p), client=fc)
    assert res["reason"] == "duplicate" and len(fc.adds) == 1
    assert json.loads(Path(os.environ["AIDUMEI_CC_BRIDGE_STATE"]).read_text())  # 补记了状态


def test_near_duplicate_same_text_skipped(cc):
    p = _topic(cc["mem"])
    fc = FakeClient()
    pl = B.build_payload(p.resolve(), cc["mem"].resolve(), p.read_text())
    fc.store.append({"id": "x1", "memory": pl["content"] + "  ", "metadata": {}})  # 别处写过同正文
    res = B.run_hook(cc["payload"](p), client=fc)
    assert res["reason"] == "duplicate" and fc.adds == []


def test_updated_file_supersedes_earlier_import(cc):
    p = _topic(cc["mem"], body="旧版本：喜欢长回答。")
    fc = FakeClient()
    other = _topic(cc["mem"], name="other.md", body="另一个文件。", title="other")
    B.run_hook(cc["payload"](other), client=fc)
    B.run_hook(cc["payload"](p), client=fc)
    fc.store.append({"id": "foreign", "memory": "别的来源", "metadata": {"source": "cc-ingest"}})
    p.write_text(p.read_text().replace("旧版本：喜欢长回答。", "新版本：喜欢短回答。"))
    res = B.run_hook(cc["payload"](p, tool="Edit"), client=fc)
    assert res["action"] == "ingested" and res["superseded"] == 1
    mems = [m["memory"] for m in fc.store]
    assert any("新版本" in m for m in mems) and not any("旧版本" in m for m in mems)
    assert any("另一个文件" in m for m in mems) and "别的来源" in mems  # 别的文件/来源不动


def test_rejected_by_gate_is_not_retried(cc):
    p = _topic(cc["mem"])
    fc = FakeClient(add_code=422)
    res = B.run_hook(cc["payload"](p), client=fc)
    assert res["action"] == "rejected"
    res2 = B.run_hook(cc["payload"](p), client=fc)
    assert res2["reason"] == "unchanged" and len(fc.adds) == 1


def test_server_error_leaves_state_for_retry(cc):
    p = _topic(cc["mem"])
    fc = FakeClient(add_code=503)
    assert B.run_hook(cc["payload"](p), client=fc)["action"] == "error"
    fc.add_code = 200
    assert B.run_hook(cc["payload"](p), client=fc)["action"] == "ingested"


def test_stub_files_not_ingested_and_do_not_supersede(cc):
    p = _topic(cc["mem"])
    fc = FakeClient()
    B.run_hook(cc["payload"](p), client=fc)
    p.write_text("---\nname: user prefs\n---\n" + B.STUB_PREFIX + "user prefs\n")
    res = B.run_hook(cc["payload"](p, tool="Edit"), client=fc)
    assert res["reason"] == "stub" and len(fc.store) == 1


def test_state_file_is_private(cc):
    p = _topic(cc["mem"])
    B.run_hook(cc["payload"](p), client=FakeClient())
    assert oct(Path(os.environ["AIDUMEI_CC_BRIDGE_STATE"]).stat().st_mode & 0o777) == oct(0o600)


def test_shell_wrapper_runs_hook(cc, tmp_path):
    import subprocess
    p = _topic(cc["mem"], body=SECRET)
    sh = B.REPO / "integrations" / "claude-code" / "aidumem-cc-memory-bridge.sh"
    env = {**os.environ, "AIDUMEI_CC_BRIDGE_INGEST": "0"}
    r = subprocess.run(["bash", str(sh)], input=json.dumps(cc["payload"](p)), env=env,
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "sk-proj-" not in p.read_text()
    assert "additionalContext" in r.stdout


# ── CC 的记忆目录口径：按 git 仓库根，子目录 / worktree 共用 ───────────────────
def _git_repo(base: Path) -> Path:
    repo = base / "src" / "proj"
    (repo / ".git" / "worktrees" / "wt1").mkdir(parents=True)
    (repo / "sub" / "deep").mkdir(parents=True)
    return repo


def test_memory_dir_follows_git_root_for_subdirs(cc):
    repo = _git_repo(cc["home"])
    mem = cc["home"] / ".claude" / "projects" / B.project_slug(str(repo)) / "memory"
    mem.mkdir(parents=True)
    p = _topic(mem)
    pl = cc["payload"](p)
    pl["cwd"] = str(repo / "sub" / "deep")
    pl["transcript_path"] = str(cc["home"] / ".claude" / "projects" / B.project_slug(pl["cwd"]) / "s.jsonl")
    assert B.hook_target(pl) == (p.resolve(), mem.resolve())


def test_memory_dir_follows_main_repo_for_worktrees(cc):
    repo = _git_repo(cc["home"])
    wt = cc["home"] / "wt1"
    wt.mkdir()
    gd = repo / ".git" / "worktrees" / "wt1"
    (gd / "commondir").write_text("../..\n")
    (wt / ".git").write_text(f"gitdir: {gd}\n")
    assert B.git_root(str(wt)) == repo.resolve()
    mem = cc["home"] / ".claude" / "projects" / B.project_slug(str(repo.resolve())) / "memory"
    mem.mkdir(parents=True)
    p = _topic(mem)
    pl = cc["payload"](p)
    pl["cwd"], pl["transcript_path"] = str(wt), ""
    assert B.hook_target(pl) is not None


def test_project_dir_name_and_auto_memory_directory(cc, monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_PROJECT_DIR_NAME", "shared")
    shared = cc["home"] / ".claude" / "projects" / "shared" / "memory"
    shared.mkdir(parents=True)
    pl = cc["payload"](_topic(shared))
    assert B.hook_target(pl) is not None
    monkeypatch.setenv("CLAUDE_CODE_PROJECT_DIR_NAME", "../x")
    assert all(".." not in d.parts for d in B.memory_dirs_for(pl))
    custom = cc["home"] / "my-mem"
    custom.mkdir()
    (cc["home"] / ".claude" / "settings.json").write_text(json.dumps({"autoMemoryDirectory": "~/my-mem"}))
    pl = cc["payload"](_topic(custom))
    assert B.memory_dirs_for(pl)[0] == custom and B.hook_target(pl) is not None


def test_modified_stamp_alone_is_not_a_new_version(cc):
    p = _topic(cc["mem"])
    fc = FakeClient()
    B.run_hook(cc["payload"](p), client=fc)
    p.write_text(p.read_text().replace("type: user\n", "type: user\nmodified: 2026-11-09T10:00:00Z\n"))
    assert B.run_hook(cc["payload"](p, tool="Edit"), client=fc)["reason"] == "unchanged"
    p.write_text(p.read_text().replace("description: 偏好", "description: 新偏好"))
    assert B.run_hook(cc["payload"](p, tool="Edit"), client=fc)["action"] == "ingested"


def test_real_server_ingest_dedup_supersede(tmp_path):
    """走真路由（本地档 + native，零外呼）：打码落盘、metadata 可见、无泄漏、重存跳过、更新取代旧条。"""
    import subprocess
    import sys
    env = {**os.environ, "TMPDIR": str(tmp_path), "AIDUMEM_LOG_DIR": str(tmp_path)}
    p = subprocess.run([sys.executable, str(B.REPO / "tests" / "_f08_ccb_real_probe.py")], capture_output=True,
                       text=True, env=env, timeout=110, cwd=str(tmp_path))
    out = json.loads(p.stdout.strip().splitlines()[-1])
    assert out == {"r1": "ingested", "redacted": ["api_key"], "on_disk": True, "metadata": True, "leak": False,
                   "r2": "unchanged", "r3": "ingested", "superseded": 1, "new_present": True,
                   "old_mem_rows": 0, "old_verbatim_rows": 0, "n1": 1}, p.stderr[-800:]


# ── f0.8.0：CC 2.1.319 把 type 放进嵌套 metadata: 块 ─────────────────────────
_NESTED = ("---\nname: 合成偏好\ndescription: 合成用例\nmetadata:\n  type: feedback\n  modified: 2026-01-01\n---\n"
           "回答保持简短。\n")


def test_nested_metadata_type_is_read():
    """★ 反向用例：type 只在 metadata: 下 → cc_type 仍要有（旧解析器只认顶层，丢掉）。"""
    fm, _, body = B.split_frontmatter(_NESTED)
    assert B.fm_type(fm) == "feedback"
    root = Path("/synthetic/proj/memory")
    pl = B.build_payload(root / "feedback_short.md", root, _NESTED)
    assert pl["metadata"]["cc_type"] == "feedback"
    assert body.strip() == "回答保持简短。"


def test_top_level_type_still_wins_and_hash_unchanged():
    top = "---\nname: 合成偏好\ntype: user\nmetadata:\n  type: feedback\n---\n正文\n"
    fm, _, _ = B.split_frontmatter(top)
    assert B.fm_type(fm) == "user"
    # 内容哈希只看顶层键：嵌套块里的 modified 时间戳变了不算新版本
    a = _NESTED
    b = _NESTED.replace("2026-01-01", "2026-02-02")
    assert B.content_sha(a) == B.content_sha(b)
