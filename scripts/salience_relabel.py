#!/usr/bin/env python3
"""salience_relabel — 把 salience 表里戳成 default 的行改回记忆真正的主人（f0.7.4）。

背景：f0.7.3 以前 /search 给结果提权时，没有 salience 行就补插一行，但不带作用域，
列默认值把它记成 user_id='default'（原文句柄 verbatim:N 和部分代理身份的记忆）。
f0.7.4 起补插带戳；本脚本修存量。只改 salience 的 user_id/bank_id 两列（元数据），
不碰记忆本身。

归属怎么认：verbatim:N → facts.db verbatim_turns 第 N 行；其余 id → memory.db（native）
与 text_fts.db 的 memories 表。只有恰好一个主人才改；找不到主人的（记忆已不在）
叫孤儿，只报告、从不删除；多个主人的叫歧义，只报告。

用法：
    python scripts/salience_relabel.py            # 只报告（默认 dry-run，不改任何东西）
    python scripts/salience_relabel.py --apply    # 先备份 salience.db，再改
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ducky.utils as U  # noqa: E402   # 调用时取路径（测试可改）


def _ro(path: str):
    if not os.path.exists(path):
        return None
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)


def _owners(mid: str, facts, mem, fts) -> set:
    found: set = set()
    if mid.startswith("verbatim:"):
        n = mid.split(":", 1)[1]
        if facts is not None and n.isdigit():
            found |= set(facts.execute(
                "SELECT user_id, bank_id FROM verbatim_turns WHERE id=?", (int(n),)).fetchall())
        return {(u, b or "default") for u, b in found if u}
    for conn in (mem, fts):
        if conn is None:
            continue
        try:
            found |= set(conn.execute(
                "SELECT user_id, COALESCE(bank_id,'default') FROM memories WHERE id=?", (mid,)).fetchall())
        except sqlite3.OperationalError:   # 该库没有 memories 表（如 mem0 内核没有 memory.db）
            continue
    return {(u, b or "default") for u, b in found if u}


def plan() -> dict:
    sal = _ro(U.SALIENCE_DB)
    if sal is None:
        return {"rows_default": 0, "relabel": [], "orphans": [], "ambiguous": [], "owner_is_default": 0}
    facts, mem, fts = _ro(U.FACTS_DB), _ro(os.path.join(U.DATA_DIR, "memory.db")), _ro(U.TEXT_FTS_DB)
    rows = [r[0] for r in sal.execute("SELECT memory_id FROM salience WHERE user_id='default'")]
    out = {"rows_default": len(rows), "relabel": [], "orphans": [], "ambiguous": [], "owner_is_default": 0}
    for mid in rows:
        owners = _owners(str(mid), facts, mem, fts)
        if not owners:
            out["orphans"].append(mid)
        elif len(owners) > 1:
            out["ambiguous"].append(mid)
        else:
            user, bank = next(iter(owners))
            if user == "default":
                out["owner_is_default"] += 1
            else:
                out["relabel"].append((mid, user, bank))
    for c in (sal, facts, mem, fts):
        if c is not None:
            c.close()
    return out


def summary(p: dict) -> dict:
    by_user: dict = {}
    for _mid, user, _bank in p["relabel"]:
        by_user[user] = by_user.get(user, 0) + 1
    return {"rows_default": p["rows_default"], "relabel_by_user": by_user,
            "orphans": len(p["orphans"]), "ambiguous": len(p["ambiguous"]),
            "owner_is_default": p["owner_is_default"]}


def apply(p: dict) -> dict:
    salience_db = U.SALIENCE_DB
    bak = f"{salience_db}.pre_relabel_{time.strftime('%Y%m%d_%H%M%S')}"
    src = sqlite3.connect(salience_db, timeout=30)
    dst = sqlite3.connect(bak)
    src.backup(dst)
    dst.close()
    n = 0
    with src:
        for mid, user, bank in p["relabel"]:
            n += src.execute("UPDATE salience SET user_id=?, bank_id=? WHERE memory_id=? AND user_id='default'",
                             (user, bank, mid)).rowcount or 0
    src.close()
    return {"backup": bak, "updated": n}


def main() -> int:
    ap = argparse.ArgumentParser(description="salience 行 default 戳改回真实主人（默认 dry-run）")
    ap.add_argument("--apply", action="store_true", help="先备份 salience.db 再改；孤儿永不删除")
    ap.add_argument("--ids", action="store_true", help="同时列出孤儿与歧义的 memory_id（不含正文）")
    a = ap.parse_args()
    p = plan()
    rep = summary(p)
    if a.ids:
        rep["orphan_ids"], rep["ambiguous_ids"] = p["orphans"], p["ambiguous"]
    if a.apply and p["relabel"]:
        rep.update(apply(p))
        rep["rows_default_after"] = plan()["rows_default"]
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
