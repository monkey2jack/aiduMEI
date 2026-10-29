#!/usr/bin/env python3
"""f0.6+++ 回滚补写:native 期间(--since 之后)在 memory.db 里的写/改/删 → 旧 mem0 栈(Qdrant + history.db + text_fts)。

切回 mem0(去掉 AIDUMEI_MEMORY_KERNEL=native)前,服务停着时跑。memory.db 只读;默认 dry-run,--apply 才写。
幂等:Qdrant 里同 id 且 hash/updated_at 一致的点跳过,history/text_fts 用 INSERT OR IGNORE,第二次应 0 变更。

  python scripts/native_to_mem0_resync.py --data-dir data --since 2026-10-28T10:00:00+08:00 \\
      --qdrant-url http://127.0.0.1:6333 --collection <mem0 主 collection> [--apply] [--json report.json]

  - 记忆:created_at/updated_at 晚于 --since 的行 → Qdrant upsert(复用 memory.db 里的向量,mem0 payload 形状);
    没有向量 / 维度不符 / id 不是 UUID(如迁移来的 verbatim:N)→ 只记数不写 Qdrant(FTS 仍能搜到)。
  - 删除:memory_history 里 --since 之后的 DELETE → Qdrant 删点 + text_fts 删行(否则已删内容会在 mem0 侧复活)。
  - history:memory_history 中 --since 之后的行 → history.db 的 history 表(同形,直拷)。

不回滚项(f0.7 P2-3,外审 haiku/ds/muse/sesame 点名,如实列出):
  - facts:f0.7 起 native 与 mem0 共用 facts.db 单一真源,无需回写;
  - 墓碑:HTTP 删除的可恢复快照(/tombstones 列表与恢复接口读的那份)写在 facts.db,不用回写;
    native 内核删除时另在 memory.db 留一份内核层墓碑(主记忆、派生事实、连带清掉的 VERBATIM 副本),
    恢复接口不读它、也不回写(复核 haiku N6 更正;两份都受 TTL 约束);
  - 图边 graph_edges:mem0 栈不读,不回写。
退出码:0 = 完成;2 = 有带正文的行因缺向量/维度不符没能写进 Qdrant(FTS 侧已补,语义召回缺这几条);
--since 必须带时区,否则拒绝(避免按 UTC 误判差 8 小时)。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import struct
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_HIST_COLS = ("id", "memory_id", "old_memory", "new_memory", "event", "created_at", "updated_at",
              "is_deleted", "actor_id", "role")


def _ts(v) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _after(v, since: datetime) -> bool:
    d = _ts(v)
    return d is not None and d > since


def _is_uuid(s: str) -> bool:
    try:
        uuid.UUID(str(s))
        return True
    except ValueError:
        return False


def _payload(r) -> dict:
    p = json.loads(r["metadata"] or "{}")
    p.update(data=r["content"], hash=r["hash"], user_id=r["user_id"], bank_id=r["bank_id"],
             created_at=r["created_at"], updated_at=r["updated_at"])
    for k in ("agent_id", "run_id", "category"):
        if r[k]:
            p[k] = r[k]
    try:
        from ducky.tombstone import _bm25_text    # mem0 bm25 用的词形化字段,与墓碑恢复同口径
        p["text_lemmatized"] = _bm25_text(r["content"])
    except ImportError:
        p["text_lemmatized"] = r["content"]
    return p


def _client(url: str, path: str):
    from qdrant_client import QdrantClient
    return QdrantClient(url=url) if url else QdrantClient(path=path)


def resync(data_dir: str, since: str, *, memory_db: str = "", qdrant_url: str = "", qdrant_path: str = "",
           collection: str = "", apply: bool = False) -> dict:
    try:
        t0 = datetime.fromisoformat(str(since).replace("Z", "+00:00"))
    except ValueError:
        raise SystemExit(f"--since 不是 ISO 时间: {since!r}") from None
    if t0.tzinfo is None:
        raise SystemExit(f"--since 必须带时区(如 +08:00): {since!r}")
    src = sqlite3.connect(f"file:{memory_db or os.path.join(data_dir, 'memory.db')}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    rows = [r for r in src.execute("SELECT m.*, v.dim, v.embedding FROM memories m LEFT JOIN memory_vectors v"
                                   " ON v.mid=m.mid") if _after(r["created_at"], t0) or _after(r["updated_at"], t0)]
    hist = [r for r in src.execute("SELECT * FROM memory_history ORDER BY created_at") if _after(r["created_at"], t0)]
    live = {(r[0], r[1], r[2]) for r in src.execute("SELECT id, user_id, bank_id FROM memories")}
    # f0.7 P2-3:删除按 (id, 租户) 判定与执行;旧迁移历史行无租户列时退回默认租户
    deleted_keys = sorted({(h["memory_id"], h["user_id"] or "default", h["bank_id"] or "default") for h in hist
                           if h["event"] == "DELETE"} - live)
    deleted = sorted({k[0] for k in deleted_keys})
    rep = {"since": since, "apply": apply, "changed_rows": len(rows), "history_rows": len(hist),
           "deleted_ids": len(deleted), "qdrant": {"upsert": 0, "unchanged": 0, "deleted": 0, "skipped_non_uuid": 0,
                                                    "skipped_no_vector": 0, "skipped_dim_mismatch": 0},
           "history_inserted": 0, "text_fts": {"inserted": 0, "deleted": 0}}

    client = _client(qdrant_url, qdrant_path) if (qdrant_url or qdrant_path) and collection else None
    if client is not None:
        from qdrant_client import models as qm
        info = client.get_collection(collection)
        vcfg = info.config.params.vectors
        dim = getattr(vcfg, "size", None)
        cand = [r for r in rows if _is_uuid(r["id"])]
        rep["qdrant"]["skipped_non_uuid"] = len(rows) - len(cand)
        have = {str(p.id): p.payload or {} for p in client.retrieve(collection, ids=[r["id"] for r in cand],
                                                                    with_payload=True)} if cand else {}
        pts = []
        for r in cand:
            old = have.get(str(uuid.UUID(r["id"])))
            if old and old.get("hash") == r["hash"] and old.get("updated_at") == r["updated_at"]:
                rep["qdrant"]["unchanged"] += 1
            elif r["embedding"] is None:
                rep["qdrant"]["skipped_no_vector"] += 1
            elif dim and r["dim"] != dim:
                rep["qdrant"]["skipped_dim_mismatch"] += 1
            else:
                vec = list(struct.unpack(f"<{r['dim']}f", r["embedding"]))
                pts.append(qm.PointStruct(id=r["id"], vector=vec, payload=_payload(r)))
        rep["qdrant"]["upsert"] = len(pts)
        gone = [i for i in deleted if _is_uuid(i)]
        if gone:
            present = client.retrieve(collection, ids=gone, with_payload=False)
            rep["qdrant"]["deleted"] = len(present)
            gone = [str(p.id) for p in present]
        if apply:
            if pts:
                client.upsert(collection, points=pts, wait=True)
            if gone:
                client.delete(collection, points_selector=qm.PointIdsList(points=gone), wait=True)
        client.close()

    hpath = os.path.join(data_dir, "history.db")
    if hist and os.path.exists(hpath):
        h = sqlite3.connect(hpath)
        cols = [c for c in _HIST_COLS if c in {x[1] for x in h.execute("PRAGMA table_info(history)")}]
        known = {x[0] for x in h.execute(f"SELECT id FROM history WHERE id IN ({','.join('?' * len(hist))})",
                                         [r["id"] for r in hist])}
        new = [[r[c] for c in cols] for r in hist if r["id"] not in known]
        rep["history_inserted"] = len(new)
        if apply and new:
            with h:
                h.executemany(f"INSERT OR IGNORE INTO history ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", new)
        h.close()

    fpath = os.path.join(data_dir, "text_fts.db")
    if os.path.exists(fpath):
        f = sqlite3.connect(fpath)
        fcols = {x[1] for x in f.execute("PRAGMA table_info(memories)")}
        want = [c for c in ("id", "content", "user_id", "bank_id", "category", "memory_type", "created_at") if c in fcols]
        for r in rows:
            if not f.execute("SELECT 1 FROM memories WHERE id=? AND user_id=? AND bank_id=?",
                             (r["id"], r["user_id"], r["bank_id"])).fetchone():
                rep["text_fts"]["inserted"] += 1
                if apply:
                    f.execute(f"INSERT INTO memories ({','.join(want)}) VALUES ({','.join('?' * len(want))})",
                              [r[c] for c in want])
            elif apply:   # 内容被 native 改过 → 同步正文(触发器维护 FTS)
                f.execute("UPDATE memories SET content=? WHERE id=? AND user_id=? AND bank_id=? AND content<>?",
                          (r["content"], r["id"], r["user_id"], r["bank_id"], r["content"]))
        for i, uid, bank in deleted_keys:
            n = f.execute("SELECT COUNT(*) FROM memories WHERE id=? AND user_id=? AND bank_id=?",
                          (i, uid, bank)).fetchone()[0]
            rep["text_fts"]["deleted"] += n
            if apply and n:
                f.execute("DELETE FROM memories WHERE id=? AND user_id=? AND bank_id=?", (i, uid, bank))
        f.commit()
        f.close()
    src.close()
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--since", required=True, help="切到 native 的时刻(ISO,带时区)")
    ap.add_argument("--memory-db", default="")
    ap.add_argument("--qdrant-url", default="")
    ap.add_argument("--qdrant-path", default="")
    ap.add_argument("--collection", default="")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", default="")
    a = ap.parse_args(argv)
    rep = resync(a.data_dir, a.since, memory_db=a.memory_db, qdrant_url=a.qdrant_url, qdrant_path=a.qdrant_path,
                 collection=a.collection, apply=a.apply)
    out = json.dumps(rep, ensure_ascii=False, indent=2)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            fh.write(out)
    print(out)
    q = rep["qdrant"]
    return 2 if (q["skipped_no_vector"] or q["skipped_dim_mismatch"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
