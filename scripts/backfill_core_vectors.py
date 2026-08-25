#!/usr/bin/env python3
"""scripts/backfill_core_vectors.py — 核心记忆存量向量回填（v20.1 WP-D1）

⚠️ 数据变更停点：**生产环境执行本脚本必须先获维护者单独批准**（v20.0.1 审计
登记原文：存量回填会改变「什么东西可被搜到」的边界，属数据决策不是代码
决策）。v20.1 部署本身只让**新写/更新**的块进向量池，存量不动。

默认 dry-run：只报会写哪些块，一个字节不动。`--apply` 才真写。

用法：
    python3 scripts/backfill_core_vectors.py                 # dry-run 全部作用域
    python3 scripts/backfill_core_vectors.py --user U --bank B   # dry-run 单作用域
    python3 scripts/backfill_core_vectors.py --apply         # 真写（先拿到点头！）
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default=None, help="只回填该 user_id（默认全部）")
    parser.add_argument("--bank", default=None, help="只回填该 bank_id（默认全部）")
    parser.add_argument("--apply", action="store_true",
                        help="真正写入向量库（默认 dry-run 只报不写）")
    args = parser.parse_args()

    from ducky.core_memory import backfill_core_vectors

    result = backfill_core_vectors(user_id=args.user, bank_id=args.bank,
                                   apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not args.apply:
        print("\n[dry-run] 未写入任何数据。确认清单无误、且已获批准后，"
              "加 --apply 执行。", file=sys.stderr)
    return 1 if result.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
