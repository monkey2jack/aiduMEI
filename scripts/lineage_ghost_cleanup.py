#!/usr/bin/env python3
"""lineage_ghost_cleanup — v20.5.0a → v20.5.0 升级遗留幽灵链清理（用户审计 🔴-1）。

背景：v20.5.0a 的写入路径在 upsert 冲突命中时误用 lastrowid 当谱系 memory_id，
可能留下两类病态链：
  ① 幽灵链：fact:<id> 指向 facts 表中不存在的行（且链尾不是 DELETE/FORGET 终链）；
  ② 形态链：fact:<fact_key>（非数字）——缺陷路径的产物，与正规 fact:<id> 链分叉。

本脚本默认**只报告不改动**（dry-run）。`--apply` 时才删除病态链记录，且：
  · 删除前自动把 facts.db 备份到同目录（.pre_ghost_cleanup_<时间戳>）；
  · 只动 memory_lineage 表，绝不触碰 facts 行；
  · 删除后跑 verify_lineage_integrity 对账并报告结果。

用法：
    python scripts/lineage_ghost_cleanup.py --json            # 只报告
    python scripts/lineage_ghost_cleanup.py --apply           # 备份 + 清理 + 对账
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ducky.utils import get_facts_conn  # noqa: E402
from ducky.memory_lineage import ensure_lineage_schema, verify_lineage_integrity  # noqa: E402


def find_sick_chains(conn) -> dict:
    """返回 {'ghost': [...], 'malformed': [...]} 两类病态 memory_id 及行数。"""
    from ducky.schema_bootstrap import ensure_core_schema
    ensure_core_schema()  # 幂等；全新库没有 facts 表时先建，避免 no such table
    ensure_lineage_schema(conn)
    facts_ids = {r[0] for r in conn.execute("SELECT id FROM facts").fetchall()}
    rows = conn.execute(
        "SELECT memory_id, MAX(version), COUNT(*) FROM memory_lineage "
        "WHERE memory_id LIKE 'fact:%' GROUP BY memory_id"
    ).fetchall()
    ghost, malformed = [], []
    for mid, _maxv, cnt in rows:
        fid_str = mid.split(":", 1)[1]
        if not fid_str.isdigit():
            malformed.append({"memory_id": mid, "records": cnt})
            continue
        if int(fid_str) not in facts_ids:
            tail = conn.execute(
                "SELECT action FROM memory_lineage WHERE memory_id=? ORDER BY version DESC LIMIT 1",
                (mid,)).fetchone()
            # DELETE/FORGET 终链是合法闭链（行本就该不存在），不算幽灵
            if not tail or (tail[0] or "").upper() not in ("DELETE", "FORGET"):
                ghost.append({"memory_id": mid, "records": cnt})
    return {"ghost": ghost, "malformed": malformed}


def main() -> int:
    ap = argparse.ArgumentParser(description="v20.5.0a 幽灵谱系链清理（默认 dry-run）")
    ap.add_argument("--apply", action="store_true", help="实际删除（先自动备份 facts.db）")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    args = ap.parse_args()

    conn = get_facts_conn()
    try:
        sick = find_sick_chains(conn)
        total = len(sick["ghost"]) + len(sick["malformed"])
        report = {"status": "ok", "sick_chains": sick, "sick_count": total,
                  "applied": False, "backup": None, "verify_after": None}

        if args.apply and total:
            from ducky.utils import FACTS_DB
            backup = f"{FACTS_DB}.pre_ghost_cleanup_{time.strftime('%Y%m%d_%H%M%S')}"
            shutil.copy2(FACTS_DB, backup)
            report["backup"] = backup
            ids = [c["memory_id"] for c in sick["ghost"]] + [c["memory_id"] for c in sick["malformed"]]
            for mid in ids:
                conn.execute("DELETE FROM memory_lineage WHERE memory_id=?", (mid,))
            conn.commit()
            report["applied"] = True
            report["verify_after"] = verify_lineage_integrity()

        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(f"幽灵链（fact:<id> 指向不存在行）: {len(sick['ghost'])} 条")
            for c in sick["ghost"]:
                print(f"  · {c['memory_id']} ({c['records']} 条记录)")
            print(f"形态链（fact:<fact_key> 非法形态）: {len(sick['malformed'])} 条")
            for c in sick["malformed"]:
                print(f"  · {c['memory_id']} ({c['records']} 条记录)")
            if not total:
                print("✅ 无病态链")
            elif not args.apply:
                print("\n（dry-run）加 --apply 才会清理；清理前自动备份 facts.db")
            else:
                print(f"\n已清理 {total} 条病态链（备份: {report['backup']}）")
                v = report["verify_after"]
                print(f"清理后对账: status={v['status']} broken={v['broken_count']}")
        return 0 if (total == 0 or report["applied"]) else 2
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
