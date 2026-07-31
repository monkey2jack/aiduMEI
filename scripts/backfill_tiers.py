#!/usr/bin/env python3
"""
scripts/backfill_tiers.py — v13.0 Pantheon 历史事实分层回填
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

schema 迁移把历史事实一律归为 semantic（安全默认）。
本脚本按 category/key/value 重新推断层级，让铁律回到 procedural（零衰减）、
事件流水回到 episodic。

用法
    python3 scripts/backfill_tiers.py --dry-run   # 只看会怎么改（默认）
    python3 scripts/backfill_tiers.py --apply     # 真正写入

安全承诺
    · 只 UPDATE memory_tier / decay_at / recorded_at，不动正文
    · --dry-run 是默认行为，必须显式 --apply 才写
    · 写前自动备份 facts.db 到 backups/
"""
from __future__ import annotations

import argparse
import shutil
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ducky.federation import tier as tier_mod  # noqa: E402
from ducky.federation.schema import ensure_federation_schema  # noqa: E402
from ducky.utils import FACTS_DB, get_facts_conn  # noqa: E402


def backup_db() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_dir = Path(FACTS_DB).parent.parent / "backups" / f"backfill-tiers-{stamp}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "facts.db.bak"
    shutil.copy2(FACTS_DB, dest)
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(description="v13.0 分层回填")
    parser.add_argument("--apply", action="store_true", help="真正写入（缺省为 dry-run）")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 条（0=全部）")
    args = parser.parse_args()

    ensure_federation_schema()
    conn = get_facts_conn()
    sql = """SELECT id, category, fact_key, fact_value,
                    COALESCE(memory_tier,'semantic') AS memory_tier,
                    COALESCE(recorded_at, created_at) AS recorded_at
             FROM facts WHERE archived=0"""
    if args.limit:
        sql += f" LIMIT {int(args.limit)}"
    rows = conn.execute(sql).fetchall()

    changes: list[tuple[str, str | None, str, int]] = []
    before = Counter()
    after = Counter()

    for row in rows:
        current = tier_mod.normalize_tier(row["memory_tier"])
        inferred = tier_mod.infer_tier(
            row["category"] or "", row["fact_key"] or "", (row["fact_value"] or "")[:400]
        )
        before[current] += 1
        after[inferred] += 1
        if inferred == current:
            continue
        recorded = row["recorded_at"] or datetime.now(timezone.utc).isoformat()
        try:
            base = datetime.fromisoformat(str(recorded).replace("Z", "+00:00").replace(" ", "T"))
        except ValueError:
            base = datetime.now(timezone.utc)
        changes.append((inferred, tier_mod.decay_deadline(inferred, base), recorded, row["id"]))

    print(f"扫描 {len(rows)} 条事实")
    print(f"当前分布: {dict(before)}")
    print(f"推断分布: {dict(after)}")
    print(f"需变更: {len(changes)} 条")

    tier_shift = Counter(c[0] for c in changes)
    print(f"变更去向: {dict(tier_shift)}")

    if not args.apply:
        print("\n[dry-run] 未写入。加 --apply 真正执行。")
        for item in changes[:10]:
            print(f"  fact#{item[3]} → {item[0]} (decay_at={item[1]})")
        conn.close()
        return 0

    if not changes:
        print("无需变更。")
        conn.close()
        return 0

    dest = backup_db()
    print(f"已备份 → {dest}")

    conn.executemany(
        "UPDATE facts SET memory_tier=?, decay_at=?, recorded_at=? WHERE id=?", changes
    )
    conn.commit()

    final = conn.execute(
        """SELECT COALESCE(memory_tier,'semantic') AS t, COUNT(*) AS c
           FROM facts WHERE archived=0 GROUP BY t"""
    ).fetchall()
    conn.close()
    print(f"✅ 写入完成。最终分布: {dict((r['t'], r['c']) for r in final)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
