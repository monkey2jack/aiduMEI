#!/usr/bin/env python3
"""
scripts/backfill_epistemic.py — v21.0 收口 · epistemic 单源精确回填
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
生产用户审计 🟡-3 整改：存量行中 **source 明确命中推断清单** 的，安全标为
reasoned；外加 🔴-2 误标纠正（pattern_extract 被错打 referenced 的行）。

铁律边界（写死在本文件，不许扩）：
  - 只动「来源无歧义」的行（_REASONED_SOURCES 白名单命中）；
  - 绝不碰具名来源行（dudu / 生产域具名直述来源 / dudu……）——历史无法考证，
    如实保留 fuzzy，这仍是「宁缺毋滥」；
  - dry-run 是默认；--apply 才落库；落库动作记 fact_events 一笔。
  - 可逆：被改行的集合由 source 精确决定，需要回滚时按同一谓词
    把 epistemic_mode 改回 'fuzzy' 即可（report 里打印该谓词）。

用法：
  python3 scripts/backfill_epistemic.py            # dry-run：只报告
  python3 scripts/backfill_epistemic.py --apply    # 落库
  AIDUMEM_DATA_DIR=/path python3 scripts/backfill_epistemic.py --apply
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 推断来源白名单（与 ducky/epistemic.py 的 _REASONED_SOURCES 对齐，但回填
# 只用「整词/前缀明确」的子集，避免子串误伤——回填比在线判定更保守）
BACKFILL_REASONED_SOURCES = (
    "pattern_extract",
    "experience_distiller",
    "reflect",
    "autodream",
    "self_edit",
    "refine_memory",
    "cron_lesson",
    "assistant",
    "llm",
    "instinct_graduation",
    "skill_growth",
    "ai-self",
)


def _facts_db_path() -> str:
    data_dir = os.environ.get("AIDUMEM_DATA_DIR", "").strip() or os.path.join(
        os.path.expanduser("~"), ".aidumem")
    return os.path.join(data_dir, "facts.db")


def main() -> int:
    apply = "--apply" in sys.argv
    db_path = _facts_db_path()
    if not os.path.exists(db_path):
        print(f"❌ facts.db 不存在: {db_path}")
        return 1

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(facts)").fetchall()}
        if "epistemic_mode" not in cols:
            print("❌ facts 表无 epistemic_mode 列——请先运行 v21 迁移（启动服务即可）")
            return 1

        # 目标 1：fuzzy 存量中白名单来源 → reasoned
        marks = ",".join("?" for _ in BACKFILL_REASONED_SOURCES)
        rows_t1 = conn.execute(
            f"SELECT source, COUNT(*) c FROM facts "
            f"WHERE epistemic_mode='fuzzy' AND source IN ({marks}) "
            f"GROUP BY source ORDER BY c DESC",
            BACKFILL_REASONED_SOURCES).fetchall()

        # 目标 2：🔴-2 误标纠正（pattern_extract 被错打 referenced）
        rows_t2 = conn.execute(
            "SELECT COUNT(*) c FROM facts "
            "WHERE epistemic_mode='referenced' AND source='pattern_extract'"
        ).fetchone()[0]

        total_t1 = sum(r[1] for r in rows_t1)
        print("═══ epistemic 单源精确回填（dry-run 默认）═══")
        print(f"库: {db_path}")
        print(f"目标 1（fuzzy→reasoned，白名单来源）: {total_t1} 行")
        for src, c in rows_t1:
            print(f"  · {src}: {c}")
        print(f"目标 2（🔴-2 误标纠正 referenced→reasoned，source=pattern_extract）: {rows_t2} 行")
        print(f"回滚谓词: source IN {BACKFILL_REASONED_SOURCES} 且本次改动行")

        if not apply:
            print("\n（dry-run，未落库；确认数字后加 --apply 执行）")
            return 0

        now = datetime.now(timezone.utc).isoformat()
        cur1 = conn.execute(
            f"UPDATE facts SET epistemic_mode='reasoned' "
            f"WHERE epistemic_mode='fuzzy' AND source IN ({marks})",
            BACKFILL_REASONED_SOURCES)
        cur2 = conn.execute(
            "UPDATE facts SET epistemic_mode='reasoned' "
            "WHERE epistemic_mode='referenced' AND source='pattern_extract'")
        # Themis 纪律：回填动作本身进事件账本
        try:
            conn.execute(
                "INSERT INTO fact_events (event_type, category, fact_key, new_value,"
                " affected_ids, created_at, user_id, bank_id) "
                "VALUES ('epistemic.backfill', 'maintenance', 'v21.0', ?, '[]', ?, 'system', 'default')",
                (f"fuzzy→reasoned {cur1.rowcount} 行；误标纠正 {cur2.rowcount} 行；"
                 f"白名单 {len(BACKFILL_REASONED_SOURCES)} 源", now))
        except Exception as e:
            print(f"⚠️ fact_events 记账跳过（表结构差异）: {e}")
        conn.commit()
        print(f"\n✅ 已落库：{cur1.rowcount} 行转 reasoned，{cur2.rowcount} 行误标纠正（{now}）")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
