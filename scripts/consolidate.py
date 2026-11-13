#!/usr/bin/env python3
"""Nightly consolidation by hand (f0.8+).

  python scripts/consolidate.py run [--mode shadow|on] [--user U --bank B] [--full] [--max-new N] [--no-llm]
  python scripts/consolidate.py report [--user U --bank B]
  python scripts/consolidate.py restore MEMORY_ID --user U [--bank B]

--data-dir points at another data directory (a copy) instead of the configured one.
Output is counts and ids only, never memory text.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["run", "report", "restore"])
    ap.add_argument("memory_id", nargs="?")
    ap.add_argument("--data-dir", default="")
    ap.add_argument("--mode", default="shadow", choices=["shadow", "on"])
    ap.add_argument("--user", default="")
    ap.add_argument("--bank", default="default")
    ap.add_argument("--full", action="store_true", help="ignore the watermark (first backfill)")
    ap.add_argument("--max-new", type=int, default=0)
    ap.add_argument("--no-llm", action="store_true", help="exact duplicates only")
    a = ap.parse_args(argv)
    if a.data_dir:
        os.environ["AIDUMEM_DATA_DIR"] = os.path.abspath(a.data_dir)
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from ducky import consolidate as C
    from ducky.utils import DATA_DIR, get_facts_conn

    fconn = get_facts_conn()
    C.ensure_schema(fconn)
    if a.cmd == "restore":
        if not (a.memory_id and a.user):
            ap.error("restore needs MEMORY_ID and --user")
        print(json.dumps({"restored": C.restore(fconn, a.user, a.bank, a.memory_id)}))
        return 0
    if a.cmd == "report":
        where, args = ("WHERE user_id=? AND bank_id=?", (a.user, a.bank)) if a.user else ("", ())
        rows = fconn.execute(f"SELECT state, kind, judge, COUNT(*) FROM consolidation_links {where} "
                             "GROUP BY 1,2,3", args).fetchall()
        runs = fconn.execute(f"SELECT run_id, mode, started_at, new_rows, llm_calls, links_duplicate, "
                             f"links_changed, error FROM consolidation_runs {where} ORDER BY started_at DESC "
                             "LIMIT 5", args).fetchall()
        print(json.dumps({"links": rows, "recent_runs": runs}, ensure_ascii=False, indent=1))
        return 0
    mconn = sqlite3.connect(f"file:{os.path.join(DATA_DIR, 'memory.db')}?mode=ro", uri=True)
    scopes = [(a.user, a.bank)] if a.user else mconn.execute(
        "SELECT DISTINCT user_id, bank_id FROM memories").fetchall()
    for u, b in scopes:
        rep = C.run_scope(mconn, fconn, u, b, mode=a.mode, full=a.full, max_new=a.max_new or None,
                          use_llm=not a.no_llm)
        print(json.dumps(rep, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
