#!/usr/bin/env python3
"""scripts/backfill_local_vectors.py — 存量本地向量回填（v20.2 WP-F）

把云 collection 里已有的记忆逐条补进本地 collection（同源 id）——
没有这一步，降挡时只有断供期新写入可被语义召回，存量记忆全部失明。

纪律（与 backfill_core_vectors 同款）：
  - dry-run 默认，--apply 才写；生产执行是数据变更停点，须维护者点头。
  - 幂等：同 id upsert，跑一百遍点数不涨。
  - 只在服务停止或同进程内运行（嵌入式 qdrant 单进程——R-17 铁律）。
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写（默认 dry-run）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理点数（0=全量）")
    args = ap.parse_args()

    from ducky.dual_index import LOCAL_COLLECTION, ensure_local_collection, upsert_local
    from ducky.mem0_runtime import get_memory

    client = get_memory().vector_store.client
    report = {"apply": args.apply, "scanned": 0, "would_index": 0,
              "indexed": 0, "skipped_no_text": 0, "failed": 0}
    if args.apply:
        ensure_local_collection(client)
    have_local = set()
    if LOCAL_COLLECTION in {c.name for c in client.get_collections().collections}:
        offset = None
        while True:
            pts, offset = client.scroll(LOCAL_COLLECTION, limit=256, offset=offset,
                                        with_payload=False, with_vectors=False)
            have_local.update(str(p.id) for p in pts)
            if offset is None:
                break

    offset = None
    while True:
        pts, offset = client.scroll("mem0", limit=128, offset=offset,
                                    with_payload=True, with_vectors=False)
        for p in pts:
            report["scanned"] += 1
            pid = str(p.id)
            if pid in have_local:
                continue
            pl = dict(p.payload or {})
            text = pl.get("data") or pl.get("memory") or ""
            if not text:
                report["skipped_no_text"] += 1
                continue
            report["would_index"] += 1
            if args.apply:
                if upsert_local(pid, str(text)[:2000], pl, client=client):
                    report["indexed"] += 1
                else:
                    report["failed"] += 1
            if args.limit and report["would_index"] >= args.limit:
                offset = None
                break
        if offset is None:
            break

    print(json.dumps(report, ensure_ascii=False, indent=1))
    if not args.apply:
        print("[dry-run] 未写入。确认清单无误、获维护者批准后加 --apply。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
