"""f0.8.0：integrations/ 下所有钩子 .sh 在 git 索引里必须是 100755。

f0.8 之前 integrations/claude-code/*.sh 是 100644：connect_host 用 ``bash 脚本`` 调不受影响，
但照文档直接把脚本路径写进宿主配置（或 git clone 后手工执行）会 Permission denied。
读的是 git 索引，不是工作区文件权限——clone 下来是什么权限只由索引决定。
（无 .git 的 archive 解包里退回读文件权限位，archive 的 tar 权限就来自索引。）
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _index_modes() -> dict[str, str]:
    """git 工作树：读索引；git archive 解包（无 .git，如实验机全量）：读文件权限位——
    archive 按索引写 tar 权限，100755 解出来就是可执行，两者同一个真相。"""
    if shutil.which("git") and (REPO / ".git").exists():
        out = subprocess.run(["git", "ls-files", "-s", "--", "integrations"], cwd=REPO,
                             capture_output=True, text=True, check=True).stdout
        modes = {}
        for ln in out.splitlines():
            meta, path = ln.split("\t", 1)
            if path.endswith(".sh"):
                modes[path] = meta.split()[0]
        return modes
    return {str(p.relative_to(REPO)): ("100755" if os.stat(p).st_mode & 0o111 == 0o111 else "100644")
            for p in (REPO / "integrations").rglob("*.sh")}


def test_all_integration_shell_hooks_are_executable_in_git_index():
    modes = _index_modes()
    assert modes, "integrations/ 下一个 .sh 都没找到——路径变了？"
    bad = sorted(p for p, m in modes.items() if m != "100755")
    assert not bad, f"这些钩子在 git 索引里不可执行（需 git update-index --chmod=+x）：{bad}"


def test_claude_code_hooks_are_covered():
    modes = _index_modes()
    cc = [p for p in modes if p.startswith("integrations/claude-code/")]
    assert len(cc) >= 4, cc
