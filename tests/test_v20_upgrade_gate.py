"""
tests/test_v20_upgrade_gate.py — 升级入口闸门：备份那一步必须真的能过
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

`scripts/pre-upgrade-check.sh` 是文档规定的升级入口：它先备份、再用
`backup_gate.sh require` 硬门禁挡住「没有已验证备份就开始升级」。

但自 v19.4.0 引入以来，它的「代码仓备份」这一步写的是：

    cp -a --exclude='venv' … "${REPO_ROOT}" "${CODE_BACKUP_DIR}"

**cp 没有 --exclude 这个选项** —— GNU coreutils 与 BSD 双双不认。实测：

    cp: unrecognized option '--exclude=venv'    退出码 1，目标目录一个不生成

于是这一步每跑必 `bad`，FAIL 恒 ≥ 1，整脚本恒退 1。**一个永远发红的闸门
等于没有闸门**：真要升级的人只能绕过它，备份纪律就此名存实亡——而绕过的
那一刻，你以为拦着你的那道门其实早就不在了。

这类缺陷能活这么久，原因很朴素：全仓没有任何一个测试引用过这个脚本
（v20.0 之前 `grep -rl pre-upgrade-check tests/` 为空）。它需要活的 API
才能整脚本跑通，所以本文件**不整脚本执行**（那会让测试去打生产端点），
只钉住让它永久发红的那颗钉子：

  ① 全仓 shell 脚本不许再给 cp 传 --exclude（该缺陷的直接形态）
  ② 升级入口的代码仓备份必须用真的支持排除的工具，且排除项没丢
  ③ 能力探针：当前平台的 tar 真的认 --exclude —— 换句话说，②依赖的机制
     不是又一个「查手册以为有」的选项。这条是①②的地基：光把 cp 换成别的
     名字，不证明新名字真的работает。

第 ③ 条故意做成端到端的建包+解包：只断言「命令退出 0」是不够的，
被排除的目录如果照样躺在解出来的树里，备份就还是那个胖包。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_PRE_UPGRADE = os.path.join(_REPO_ROOT, "scripts", "pre-upgrade-check.sh")

# cp 调用里带 --exclude 的形态（允许中间夹别的短选项，如 cp -a --exclude=…）
_CP_EXCLUDE = re.compile(r"(?:^|[|;&(]|\s)cp\s+(?:-[A-Za-z]+\s+)*--exclude")


def _shell_scripts() -> list[str]:
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(_REPO_ROOT):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in {".git", "venv", ".venv", "__pycache__", "node_modules"}
        ]
        for name in filenames:
            if name.endswith(".sh"):
                found.append(os.path.join(dirpath, name))
    return sorted(found)


def test_no_shell_script_passes_exclude_to_cp():
    """① cp 不认 --exclude；谁这么写，那一步就是永久失败。"""
    scripts = _shell_scripts()
    assert scripts, "一个 .sh 都没扫到，说明遍历口径写坏了（空集不算通过）"

    offenders: list[str] = []
    for path in scripts:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for lineno, line in enumerate(fh, 1):
                code = line.split("#", 1)[0]
                if _CP_EXCLUDE.search(code):
                    rel = os.path.relpath(path, _REPO_ROOT)
                    offenders.append(f"{rel}:{lineno}: {line.strip()}")

    assert not offenders, "cp 没有 --exclude 选项，这些调用每次都会失败：\n" + "\n".join(
        offenders
    )


def test_pre_upgrade_code_backup_keeps_its_exclusions():
    """② 换掉 cp 之后，排除项不许在搬家过程中掉队。"""
    with open(_PRE_UPGRADE, encoding="utf-8") as fh:
        text = fh.read()

    # 定位代码仓备份那一段（从注释锚点到该步骤的 bad 分支结束）
    start = text.find("代码仓轻量备份")
    assert start != -1, "找不到代码仓备份段落，锚点变了就得同步改这个测试"
    end = text.find("步骤 2/5", start)
    assert end != -1, "找不到步骤 2 的分界，段落切分口径失效"
    block = text[start:end]

    assert "tar -cf" in block, "代码仓备份应当用 tar（cp 不支持排除）"
    for pattern in ("--exclude='venv'", "--exclude='__pycache__'", "--exclude='*.bak-*'"):
        assert pattern in block, f"备份排除项缺了 {pattern}，包会胖回去"
    assert not _CP_EXCLUDE.search(block), "这一段又回到 cp --exclude 了"


def test_tar_on_this_platform_really_honors_exclude(tmp_path):
    """③ 能力探针：②依赖的 tar --exclude 在本平台真的生效，不是纸面选项。"""
    src = tmp_path / "repo"
    (src / "venv" / "lib").mkdir(parents=True)
    (src / "venv" / "lib" / "huge.so").write_bytes(b"\0" * 1024)
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"\0" * 16)
    (src / "ducky").mkdir()
    (src / "ducky" / "keep.py").write_text("KEEP\n", encoding="utf-8")
    (src / "version.py.bak-pre-v19-20260101_000000").write_text("old\n", encoding="utf-8")

    tar_path = tmp_path / "bundle.tar"
    out = tmp_path / "restored"
    out.mkdir()

    create = subprocess.run(
        [
            "tar",
            "-cf",
            str(tar_path),
            "--exclude=venv",
            "--exclude=.venv",
            "--exclude=__pycache__",
            "--exclude=*.tar.gz",
            "--exclude=*.bak-*",
            "-C",
            str(src),
            ".",
        ],
        capture_output=True,
        text=True,
    )
    if create.returncode != 0:
        pytest.fail(
            "本平台的 tar 不认这组 --exclude（这正是 cp 当年翻车的形态）："
            f"rc={create.returncode} stderr={create.stderr.strip()[:300]}"
        )

    extract = subprocess.run(
        ["tar", "-xf", str(tar_path), "-C", str(out)], capture_output=True, text=True
    )
    assert extract.returncode == 0, f"解包失败：{extract.stderr.strip()[:300]}"

    restored = {
        os.path.relpath(os.path.join(dirpath, name), out)
        for dirpath, _dirnames, filenames in os.walk(out)
        for name in filenames
    }

    assert "ducky/keep.py" in restored, f"该留的文件没留下：{sorted(restored)}"
    assert not [p for p in restored if p.startswith("venv/")], f"venv 没被排除：{sorted(restored)}"
    assert not [p for p in restored if "__pycache__" in p], f"缓存没被排除：{sorted(restored)}"
    assert not [p for p in restored if ".bak-" in p], f"旧 bak 没被排除：{sorted(restored)}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
