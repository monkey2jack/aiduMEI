#!/usr/bin/env python3
"""scripts/deploy_manifest.py — 部署清单：文件集合 + 内容哈希（v20 · P0-9）

为什么需要它
────────────
用户视角审计提过一条👎：「守卫测试文件 `test_v20_legacy_alias_guard.py` 引用了但不在
生产仓」。逐个核对之后发现**双方都对**：那个文件在仓里确实存在，而生产机上确实没有
—— 因为生产**靠文件拷贝部署，不靠 git**。

所以这不是「文档承诺未兑现」，是**部署链路本身没有清单**。没有清单就没有判据：
「生产和仓库一致吗」这个问题，此前只能靠人一个个文件去看，而人看不完 300 个文件。

判据口径（铁律 11）
──────────────────
**按主键集合做差集，不比计数。** 「两边都是 288 个文件」不能证明是同一批 288 个 ——
少一个 A、多一个 B，计数完全相同。所以输出三个集合：只在左、只在右、两边都有但
哈希不同。计数只用来给人看一眼规模，从不作为判据。

用法
────
    # 在仓库根生成清单
    python3 scripts/deploy_manifest.py emit > /tmp/repo.json

    # 在部署树生成清单
    python3 scripts/deploy_manifest.py emit --root /root/dudu-mem0 > /tmp/prod.json

    # 比对（差集非空 → 退出码 1 并逐条点名）
    python3 scripts/deploy_manifest.py diff /tmp/repo.json /tmp/prod.json

退出码：0 = 完全一致；1 = 有差异（已逐条点名）；2 = 拒绝运行（用法错/路径不存在/
扫到 0 个文件 —— 后者与「真的一致」无法区分，照 release_scan 的同一条口径作废本轮）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

#: 只清点**代码资产**。数据、密钥、缓存、虚拟环境不在部署清单的射程内 ——
#: 它们本就该两边不同（生产有真实数据，仓库没有），混进来会让差集永远非空，
#: 而一个永远非空的判据等于没有判据。
SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules", "data", "logs",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build",
    ".claude", "htmlcov", "backups",
}
#: 构建产物目录：`pip install -e .` 生成的 egg-info 两边必然不同（路径与时间戳），
#: 混进清单会让差集永远非空 —— 而永远非空的判据等于没有判据。
#: 用后缀匹配而不是写死名字：包名一改，写死的名字就静默失效。
SKIP_DIR_SUFFIXES = (".egg-info", ".dist-info")
SKIP_SUFFIXES = {".pyc", ".pyo", ".log", ".db", ".db-wal", ".db-shm", ".sqlite"}
#: 按文件名精确豁免（不许目录级豁免 —— 铁律 12）
SKIP_NAMES = {
    ".env", ".llm_key", ".sf_key", ".sensenova_key",
    ".ui_password_hash", ".ui_initial_password",
    "mem0_config_local.json",
    # 测试与工具残留：两边必然不同，且与「代码是否一致」无关
    ".coverage", ".DS_Store",
}


def _iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in SKIP_DIRS and not d.endswith(SKIP_DIR_SUFFIXES)
        ]
        for fn in filenames:
            if fn in SKIP_NAMES or Path(fn).suffix in SKIP_SUFFIXES:
                continue
            p = Path(dirpath) / fn
            if p.is_symlink() or not p.is_file():
                continue
            yield p


def emit(root: Path) -> dict:
    entries: dict[str, str] = {}
    for p in _iter_files(root):
        try:
            h = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            continue
        entries[p.relative_to(root).as_posix()] = h
    return {"root_name": root.name, "count": len(entries), "files": entries}


def diff(left: dict, right: dict, left_name: str, right_name: str) -> int:
    lf, rf = left["files"], right["files"]
    lk, rk = set(lf), set(rf)

    only_left = sorted(lk - rk)
    only_right = sorted(rk - lk)
    changed = sorted(k for k in (lk & rk) if lf[k] != rf[k])

    # 结构性兜底：扫到 0 个文件的清单，其「无差异」与「真的一致」无法区分
    for name, m in ((left_name, left), (right_name, right)):
        if not m["files"]:
            print(f"[拒绝运行] {name} 清单里一个文件都没有；"
                  f"扫了 0 个文件的「一致」与真的一致无法区分，本轮结论作废",
                  file=sys.stderr)
            return 2

    print(f"左 {left_name}: {left['count']} 个文件")
    print(f"右 {right_name}: {right['count']} 个文件")
    print(f"—— 判据是集合差集，不是计数（铁律 11）——")
    print(f"只在左侧 : {len(only_left)}")
    for k in only_left[:40]:
        print(f"    - {k}")
    if len(only_left) > 40:
        print(f"    …… 另 {len(only_left) - 40} 个")
    print(f"只在右侧 : {len(only_right)}")
    for k in only_right[:40]:
        print(f"    + {k}")
    if len(only_right) > 40:
        print(f"    …… 另 {len(only_right) - 40} 个")
    print(f"内容不同 : {len(changed)}")
    for k in changed[:40]:
        print(f"    ~ {k}")
    if len(changed) > 40:
        print(f"    …… 另 {len(changed) - 40} 个")

    total = len(only_left) + len(only_right) + len(changed)
    print(f"\n差异合计 = {total}")
    return 1 if total else 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(add_help=True, description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("emit", help="生成清单（JSON 到 stdout）")
    e.add_argument("--root", default=".")
    d = sub.add_parser("diff", help="比对两份清单")
    d.add_argument("left")
    d.add_argument("right")
    args = ap.parse_args(argv)

    if args.cmd == "emit":
        root = Path(args.root).resolve()
        if not root.is_dir():
            print(f"[拒绝运行] 根目录不存在：{root}", file=sys.stderr)
            return 2
        m = emit(root)
        if not m["files"]:
            print(f"[拒绝运行] 在 {root} 下扫到 0 个文件；"
                  f"空清单的「一致」没有意义", file=sys.stderr)
            return 2
        json.dump(m, sys.stdout, ensure_ascii=False, indent=1, sort_keys=True)
        print()
        return 0

    for path in (args.left, args.right):
        if not Path(path).exists():
            print(f"[拒绝运行] 清单文件不存在：{path}", file=sys.stderr)
            return 2
    with open(args.left, encoding="utf-8") as f:
        left = json.load(f)
    with open(args.right, encoding="utf-8") as f:
        right = json.load(f)
    return diff(left, right, args.left, args.right)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
