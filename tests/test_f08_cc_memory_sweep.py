"""f0.8 ①：CC 记忆兜底扫——dry-run 不动、apply 打码入库、幂等、裁剪存根与索引封顶。"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from scripts.cc_memory_sweep import B
from scripts import cc_memory_sweep as S
from tests.test_f08_cc_memory_bridge import SECRET, FakeClient, _topic


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for k in ("CLAUDE_CONFIG_DIR", "AIDUMEI_CC_MEMORY_DIR", "AIDUMEI_SECRET_GUARD", "AIDUMEI_CC_TRIM_DAYS",
              "AIDUMEI_CC_INDEX_CAP", "AIDUMEI_CC_BRIDGE_MAX_CHARS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AIDUMEI_CC_BRIDGE_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("AIDUMEI_CC_BRIDGE_TRASH", str(tmp_path / "trash"))
    mem = tmp_path / ".claude" / "projects" / "-work-demo" / "memory"
    mem.mkdir(parents=True)
    return {"home": tmp_path, "mem": mem}


def _age(p: Path, days: float):
    t = time.time() - days * 86400
    os.utime(p, (t, t))


def _snapshot(d: Path) -> dict:
    return {str(p.relative_to(d)): p.read_bytes() for p in sorted(d.rglob("*")) if p.is_file()}


def _run(args, fc, capsys):
    rc = S.main(args + ["--json"], client=fc)
    return rc, json.loads(capsys.readouterr().out)


def test_dry_run_is_default_and_changes_nothing(env, capsys):
    _topic(env["mem"], body="带密钥 " + SECRET)
    _topic(env["mem"], name="b.md", body="普通内容", title="b")
    before = _snapshot(env["mem"])
    fc = FakeClient()
    rc, rep = _run([], fc, capsys)
    assert rc == 0 and rep["mode"] == "dry-run"
    acts = {a["action"] for a in rep["actions"]}
    assert {"would_redact", "would_ingest"} <= acts
    assert _snapshot(env["mem"]) == before and fc.adds == []
    assert not Path(os.environ["AIDUMEI_CC_BRIDGE_STATE"]).exists()


def test_apply_redacts_and_ingests_then_is_idempotent(env, capsys):
    p = _topic(env["mem"], body="带密钥 " + SECRET)
    _topic(env["mem"], name="b.md", body="普通内容", title="b")
    (env["mem"] / "MEMORY.md").write_text("- [a](user_prefs.md)\n- [b](b.md)\n")
    fc = FakeClient()
    rc, rep = _run(["--apply"], fc, capsys)
    assert rc == 0
    assert "sk-proj-" not in p.read_text() and len(fc.adds) == 2  # 索引不入库
    assert "sk-proj-" not in json.dumps(fc.adds, ensure_ascii=False)
    rc, rep2 = _run(["--apply"], fc, capsys)
    assert rep2["actions"] == [] and len(fc.adds) == 2


def test_dry_and_apply_override(env, capsys):
    _topic(env["mem"])
    fc = FakeClient()
    _, rep = _run(["--apply", "--dry-run"], fc, capsys)
    assert rep["mode"] == "dry-run" and fc.adds == []


def test_symlinks_skipped_in_sweep(env, capsys):
    outside = env["home"] / "victim.md"
    outside.write_text(SECRET)
    (env["mem"] / "evil.md").symlink_to(outside)
    fc = FakeClient()
    _run(["--apply"], fc, capsys)
    assert outside.read_text() == SECRET and fc.adds == []


def test_explicit_dir_and_all_projects(env, capsys):
    other = env["home"] / ".claude" / "projects" / "-work-other" / "memory"
    other.mkdir(parents=True)
    _topic(other, body="另一个项目")
    _topic(env["mem"])
    fc = FakeClient()
    _, rep = _run([], fc, capsys)
    assert rep["dirs"] == 2
    _, rep = _run(["--dir", str(other)], fc, capsys)
    assert rep["dirs"] == 1 and {a["project"] for a in rep["actions"]} == {"-work-other"}


def test_trim_needs_apply_and_flag(env, capsys):
    p = _topic(env["mem"])
    fc = FakeClient()
    _run(["--apply"], fc, capsys)
    _age(p, 30)
    before = p.read_text()
    _, rep = _run(["--apply"], fc, capsys)  # 没 --trim：不动
    assert p.read_text() == before and rep["actions"] == []
    _, rep = _run(["--trim"], fc, capsys)  # 没 --apply：只报告
    assert p.read_text() == before and [a["action"] for a in rep["actions"]] == ["would_stub"]
    _, rep = _run(["--apply", "--trim"], fc, capsys)
    txt = p.read_text()
    assert B.is_stub(txt) and txt.startswith("---\nname: user prefs") and B.STUB_PREFIX + "user prefs" in txt
    act = [a for a in rep["actions"] if a["action"] == "stub"][0]
    assert Path(act["backup"]).read_text() == before
    assert oct(Path(act["backup"]).stat().st_mode & 0o777) == oct(0o600)
    # 存根不入库、不触发取代：服务端那条导入还在
    _, rep = _run(["--apply", "--trim"], fc, capsys)
    assert rep["actions"] == [] and len(fc.store) == 1


def test_trim_spares_recent_unimported_and_changed(env, capsys):
    fresh = _topic(env["mem"], name="fresh.md", title="fresh")
    old = _topic(env["mem"], name="old.md", title="old")
    fc = FakeClient()
    _run(["--apply"], fc, capsys)
    _age(old, 30)
    unimported = _topic(env["mem"], name="new.md", title="new")
    _age(unimported, 30)
    fc.add_code = 503  # new.md 导入失败 → 不许存根
    _, rep = _run(["--apply", "--trim", "--days", "14"], fc, capsys)
    stubbed = {a["file"] for a in rep["actions"] if a["action"] == "stub"}
    assert stubbed == {"old.md"}
    assert not B.is_stub(fresh.read_text()) and not B.is_stub(unimported.read_text())


def test_index_cap(env, capsys):
    files = []
    for i in range(12):
        files.append(_topic(env["mem"], name=f"t{i:02d}.md", body=f"内容 {i}", title=f"t{i}"))
    idx = env["mem"] / "MEMORY.md"
    lines = ["# Memory index"] + [f"- [t{i}](t{i:02d}.md) — 第 {i} 条" for i in range(12)]
    idx.write_text("\n".join(lines) + "\n")
    fc = FakeClient()
    _run(["--apply"], fc, capsys)
    unimp = env["mem"] / "zz.md"
    idx.write_text(idx.read_text() + "- [zz](zz.md) — 未导入\n")
    fc.add_code = 503
    unimp.write_text("未导入的内容")
    _, rep = _run(["--trim", "--index-cap", "6"], fc, capsys)
    act = [a for a in rep["actions"] if a.get("file") == "MEMORY.md"][0]
    assert act["action"] == "would_cap_index" and act["drop"] == 8 and len(idx.read_text().splitlines()) == 14
    _, rep = _run(["--apply", "--trim", "--index-cap", "6"], fc, capsys)
    kept = idx.read_text().splitlines()
    assert len(kept) == 6 and kept[0] == "# Memory index"
    assert "- [zz](zz.md) — 未导入" in kept  # 未导入的条目绝不摘
    assert kept[1].startswith("- [t8]")  # 从最早的条目摘起
    act = [a for a in rep["actions"] if a.get("file") == "MEMORY.md"][0]
    assert Path(act["backup"]).read_text().count("\n") == 14
    _, rep = _run(["--apply", "--trim", "--index-cap", "6"], fc, capsys)
    assert not [a for a in rep["actions"] if a.get("file") == "MEMORY.md"]  # 幂等


def test_index_cap_prefers_stubbed_entries(env, capsys):
    for i in range(6):
        _topic(env["mem"], name=f"t{i}.md", body=f"内容 {i}", title=f"t{i}")
    idx = env["mem"] / "MEMORY.md"
    idx.write_text("\n".join(f"- [t{i}](t{i}.md)" for i in range(6)) + "\n")
    fc = FakeClient()
    _run(["--apply"], fc, capsys)
    _age(env["mem"] / "t5.md", 30)
    _run(["--apply", "--trim", "--index-cap", "5"], fc, capsys)
    kept = idx.read_text().splitlines()
    assert "- [t5](t5.md)" not in kept and "- [t0](t0.md)" in kept


def test_index_over_cap_with_nothing_removable_only_reports(env, capsys):
    idx = env["mem"] / "MEMORY.md"
    idx.write_text("\n".join(f"- [x{i}](x{i}.md)" for i in range(10)) + "\n")
    before = idx.read_text()
    _, rep = _run(["--apply", "--trim", "--index-cap", "5"], FakeClient(), capsys)
    act = [a for a in rep["actions"] if a.get("file") == "MEMORY.md"][0]
    assert act["action"] == "index_over_cap" and act["over_cap_unimported"] == 5
    assert idx.read_text() == before


def test_errors_give_exit_code_1(env, capsys):
    _topic(env["mem"])
    rc, rep = _run(["--apply"], FakeClient(add_code=503), capsys)
    assert rc == 1 and rep["errors"] == 1


def test_user_auto_memory_directory_is_swept(env, capsys):
    custom = env["home"] / "my-mem"
    custom.mkdir()
    (env["home"] / ".claude" / "settings.json").write_text(json.dumps({"autoMemoryDirectory": "~/my-mem"}))
    _topic(custom)
    _, rep = _run([], FakeClient(), capsys)
    assert rep["dirs"] == 2 and any(a.get("project") == env["home"].name for a in rep["actions"])
