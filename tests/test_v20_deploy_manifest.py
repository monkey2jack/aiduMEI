"""tests/test_v20_deploy_manifest.py — v20 P0-9：部署链路必须有清单

用户视角审计有一条👎：「守卫测试文件 `test_v20_legacy_alias_guard.py` 引用了但不在
生产仓」。逐个核对之后发现**双方都对** —— 那个文件在仓里确实存在，生产机上确实没有，
因为生产**靠文件拷贝部署、不靠 git**。

所以这不是「文档承诺未兑现」，是**部署链路本身没有清单**。没有清单就没有判据：
「生产和仓库一致吗」此前只能靠人一个个文件去看，而人看不完三百个文件。

判据口径是铁律 11：**按主键集合做差集，不比计数。**
「两边都是 309 个文件」不能证明是同一批 309 个 —— 少一个 A、多一个 B，计数完全相同。

跑法：cd <仓库根> && .venv/bin/pytest tests/test_v20_deploy_manifest.py -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
_TOOL = _REPO_ROOT / "scripts" / "deploy_manifest.py"

sys.path.insert(0, str(_REPO_ROOT / "scripts"))
import deploy_manifest as DM  # noqa: E402


def _run(*args):
    return subprocess.run([sys.executable, str(_TOOL), *args],
                          capture_output=True, text=True)


# ═══════════════ ① 清单本身 ═══════════════

def test_manifest_excludes_secrets_data_and_build_artifacts(tmp_path):
    """★ 清单射程：密钥、数据、编译与构建产物一个都不许进。

    它们本就该两边不同（生产有真实数据、仓库没有），混进来会让差集**永远非空** ——
    而一个永远非空的判据，和一个永远为空的判据一样没用。

    ⚠️ 变异轮抓到的：第一版直接扫本仓，然后断言「清单里没有 .env / .llm_key /
    data/*.db」。它是绿的，但**绿得空转** —— 本仓压根没有这些文件，所以把整张
    豁免表删空，那条断言照样绿。判据必须踩在「这些文件真的存在」的前提上。
    所以这里现造一棵含全部敏感物的合成树。
    """
    root = tmp_path / "tree"
    # 该被清点的
    (root / "ducky").mkdir(parents=True)
    (root / "ducky" / "core.py").write_text("x = 1\n", encoding="utf-8")
    (root / "README.md").write_text("# hi\n", encoding="utf-8")
    # 一个都不许被清点的
    (root / ".env").write_text("AIDUMEM_API_TOKEN=realtokenvalue\n", encoding="utf-8")
    (root / ".llm_key").write_text("sk-real\n", encoding="utf-8")
    (root / "mem0_config_local.json").write_text("{}", encoding="utf-8")
    (root / "data").mkdir()
    (root / "data" / "facts.db").write_bytes(b"SQLite")
    (root / "data" / "note.json").write_text("{}", encoding="utf-8")
    (root / "logs").mkdir()
    (root / "logs" / "api.log").write_text("line\n", encoding="utf-8")
    (root / "ducky" / "__pycache__").mkdir()
    (root / "ducky" / "__pycache__" / "core.cpython-312.pyc").write_bytes(b"\x00")
    (root / "aidumei.egg-info").mkdir()
    (root / "aidumei.egg-info" / "PKG-INFO").write_text("Name: x\n", encoding="utf-8")
    (root / ".venv").mkdir()
    (root / ".venv" / "pyvenv.cfg").write_text("home = /x\n", encoding="utf-8")
    (root / ".coverage").write_text("cov", encoding="utf-8")

    m = DM.emit(root)
    got = set(m["files"])
    assert got == {"ducky/core.py", "README.md"}, (
        f"清单射程不对。应只清点 2 个代码文件，实际 {len(got)} 个：{sorted(got)}"
    )

    # 正向对照：这棵树里确实摆了那些敏感物（否则上面那条又是空转）
    for must_exist in (".env", ".llm_key", "mem0_config_local.json",
                       "data/facts.db", "logs/api.log",
                       "ducky/__pycache__/core.cpython-312.pyc",
                       "aidumei.egg-info/PKG-INFO", ".venv/pyvenv.cfg", ".coverage"):
        assert (root / must_exist).exists(), f"合成树没摆上 {must_exist}，本用例空转"


def test_manifest_hashes_content_not_just_names():
    """清单必须带内容哈希 —— 只比文件名列表连「改了内容」都发现不了。"""
    m = DM.emit(_REPO_ROOT)
    sample = next(iter(m["files"].values()))
    assert len(sample) == 64 and all(c in "0123456789abcdef" for c in sample), (
        f"哈希形状不对：{sample!r}（期望 sha256 十六进制 64 位）"
    )


def test_emit_refuses_to_produce_an_empty_manifest(tmp_path):
    """★ 空清单必须拒绝运行，不许换来一个退出码 0。

    照 `release_scan.py` 的同一条口径：扫了 0 个文件的「一致」与「真的一致」
    无法区分。这条是结构性兜底 —— 拼错的路径、空目录、权限不足，症状都是同一个 0。
    """
    empty = tmp_path / "nothing"
    empty.mkdir()
    r = _run("emit", "--root", str(empty))
    assert r.returncode == 2, f"空目录换来了退出码 {r.returncode}"
    assert "拒绝运行" in r.stderr


def test_emit_refuses_a_nonexistent_root(tmp_path):
    r = _run("emit", "--root", str(tmp_path / "does-not-exist"))
    assert r.returncode == 2 and "拒绝运行" in r.stderr


# ═══════════════ ② 判据是集合差集，不是计数 ═══════════════

def _write(tmp_path, name, files):
    p = tmp_path / name
    p.write_text(json.dumps({"root_name": name, "count": len(files), "files": files}),
                 encoding="utf-8")
    return str(p)


def test_identical_manifests_diff_clean(tmp_path):
    f = {"a.py": "1" * 64, "b/c.py": "2" * 64}
    left = _write(tmp_path, "l.json", f)
    right = _write(tmp_path, "r.json", dict(f))
    r = _run("diff", left, right)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "差异合计 = 0" in r.stdout


def test_same_count_but_different_sets_is_caught(tmp_path):
    """★ 本文件的核心：**计数相同、集合不同**必须被抓住。

    这正是铁律 11 那句话的可执行形态。少一个 A、多一个 B —— 计数判据全绿，
    集合判据当场报出两个名字。
    """
    left = _write(tmp_path, "l.json", {"a.py": "1" * 64, "b.py": "2" * 64})
    right = _write(tmp_path, "r.json", {"a.py": "1" * 64, "ghost.py": "3" * 64})
    r = _run("diff", left, right)
    assert r.returncode == 1, f"计数相同的集合差异没被抓住：\n{r.stdout}"
    assert "- b.py" in r.stdout, f"没点名「只在左侧」的那个：\n{r.stdout}"
    assert "+ ghost.py" in r.stdout, f"没点名「只在右侧」的那个：\n{r.stdout}"


def test_same_names_but_changed_content_is_caught(tmp_path):
    """同名不同内容 —— 只比文件名的清单对此完全失明。"""
    left = _write(tmp_path, "l.json", {"a.py": "1" * 64})
    right = _write(tmp_path, "r.json", {"a.py": "9" * 64})
    r = _run("diff", left, right)
    assert r.returncode == 1
    assert "~ a.py" in r.stdout, f"内容变化没被点名：\n{r.stdout}"
    assert "内容不同 : 1" in r.stdout


def test_diff_refuses_when_either_side_is_empty(tmp_path):
    """一侧为空时拒绝运行 —— 空清单的「无差异」没有意义。"""
    left = _write(tmp_path, "l.json", {"a.py": "1" * 64})
    right = _write(tmp_path, "r.json", {})
    r = _run("diff", left, right)
    assert r.returncode == 2, f"空清单换来了退出码 {r.returncode}"
    assert "拒绝运行" in r.stderr


def test_diff_refuses_missing_manifest_files(tmp_path):
    left = _write(tmp_path, "l.json", {"a.py": "1" * 64})
    r = _run("diff", left, str(tmp_path / "nope.json"))
    assert r.returncode == 2 and "拒绝运行" in r.stderr


def test_report_never_reduces_the_verdict_to_a_count(tmp_path):
    """★ 报告里必须逐条点名，不许只给一个数字。

    「差异 3 处」对运维没有可行动性；「少了 b.py、多了 ghost.py、a.py 内容变了」才有。
    """
    left = _write(tmp_path, "l.json", {"a.py": "1" * 64, "b.py": "2" * 64})
    right = _write(tmp_path, "r.json", {"a.py": "9" * 64, "ghost.py": "3" * 64})
    r = _run("diff", left, right)
    for token in ("- b.py", "+ ghost.py", "~ a.py"):
        assert token in r.stdout, f"报告里缺 {token}：\n{r.stdout}"
