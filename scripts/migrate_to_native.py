#!/usr/bin/env python3
"""f0.6+++ WP3a · 旧存储 → 单库 memory.db 的幂等导入 + 图边回填 + 对账。

源全部只读:facts.db / text_fts.db / history.db 用 ?mode=ro 打开;Qdrant 只做 scroll
(path 模式先整目录复制到临时目录再读,不碰源目录的锁)。可重复执行,第二次应 0 新增。

  python scripts/migrate_to_native.py --data-dir <旧 DATA_DIR> --out <memory.db>
        [--qdrant-url URL | --qdrant-path DIR] [--collection NAME] [--json report.json]

记忆真源取 Qdrant 点(带向量,mem0 同一 embedder 产出,直接复用);text_fts 里有、Qdrant
里没有的记录照导正文、不带向量(报告 need_embed,切换前补嵌入)。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# facts.db 里随单库迁走的兼容表(CoreMemory / facts / 原文 / 墓碑 / 谱系)。按源库 DDL 原样建表。
FACTS_TABLES = ("facts", "entities", "fact_entities", "fact_events", "core_memory", "verbatim_turns",
                "memory_lineage", "memory_types", "memory_epistemic", "memory_banks", "tombstones", "graph_edges",
                "candidate_facts")
_NORMALIZED = {"candidate_facts": {"user_id", "scope_user_id", "bank_id", "source"}}   # 对账时不比这些列
_CORE = {"data", "hash", "created_at", "updated_at", "user_id", "agent_id", "run_id", "text_lemmatized"}


def _ro(path: str) -> sqlite3.Connection | None:
    if not os.path.exists(path):
        return None
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _tables(c) -> set[str]:
    return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def source_fingerprint(data_dir: str) -> dict:
    """源库内容指纹:每个 SQLite 库按 iterdump(逻辑内容)算 sha256,不看文件字节。

    文件哈希不可靠:最后一个连接关闭时 SQLite 会把 -wal 合进主文件(2026-10-27 切换实测
    facts.db / salience.db 文件哈希变了、逻辑内容一字未变)。只读打开,不触发 checkpoint。"""
    out = {}
    for name in sorted(os.listdir(data_dir)):
        path = os.path.join(data_dir, name)
        if not name.endswith((".db", ".sqlite", ".sqlite3")) or not os.path.isfile(path):
            continue
        try:
            c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            h = hashlib.sha256()
            for line in c.iterdump():
                h.update(line.encode("utf-8", "surrogatepass"))
            c.close()
            out[name] = h.hexdigest()[:16]
        except sqlite3.Error as exc:
            out[name] = f"error:{type(exc).__name__}"
    return out


def _digest(rows) -> str:
    h = hashlib.sha256()
    for r in sorted(json.dumps(list(r), ensure_ascii=False, default=str) for r in rows):
        h.update(r.encode())
    return h.hexdigest()[:16]


def copy_table(src, dst, name: str) -> dict:
    """按源 DDL 建表(已存在则补列),INSERT OR IGNORE 全量;tombstones 按自然键去重。"""
    ddl = src.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()[0]
    if name not in _tables(dst):
        dst.execute(ddl)
    have = {r[1] for r in dst.execute(f"PRAGMA table_info({name})")}
    cols = [r[1] for r in src.execute(f"PRAGMA table_info({name})")]
    for col in cols:
        if col not in have:
            dst.execute(f'ALTER TABLE {name} ADD COLUMN "{col}"')
    before = dst.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
    rows = src.execute(f"SELECT {','.join(chr(34) + c + chr(34) for c in cols)} FROM {name}").fetchall()
    gone = set()
    if name == "facts" and "tombstones" in _tables(dst):   # 已在 memory.db 里裁掉的孤儿事实不再导回
        gone = {r[0] for r in dst.execute("SELECT target_id FROM tombstones WHERE target_type='fact'"
                                          " AND actor='migrate'")}
        rows = [r for r in rows if str(r[cols.index("id")]) not in gone]
    if name == "tombstones":     # 自增 id 会与影子期新写的撞,按自然键去重、不带 id
        cols_n = [c for c in cols if c != "tombstone_id"]
        for r in rows:
            d = dict(zip(cols, r))
            if not dst.execute("SELECT 1 FROM tombstones WHERE target_id=? AND user_id=? AND "
                               "COALESCE(tombstoned_at,'')=COALESCE(?, '')",
                               (d["target_id"], d["user_id"], d.get("tombstoned_at"))).fetchone():
                dst.execute(f"INSERT INTO tombstones ({','.join(cols_n)}) VALUES ({','.join('?' * len(cols_n))})",
                            [d[c] for c in cols_n])
    else:
        q = ",".join(chr(34) + c + chr(34) for c in cols)
        dst.executemany(f"INSERT OR IGNORE INTO {name} ({q}) VALUES ({','.join('?' * len(cols))})", rows)
    for (idx,) in src.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
                              (name,)).fetchall():   # 唯一约束/查询索引一并带上(兼容写入语义)
        dst.execute(idx.replace("CREATE UNIQUE INDEX ", "CREATE UNIQUE INDEX IF NOT EXISTS ", 1)
                    .replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1))
    after = dst.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
    skip = {"tombstone_id"} | _NORMALIZED.get(name, set())
    sel = ",".join(chr(34) + c + chr(34) for c in cols if c not in skip)
    id_pos = [c for c in cols if c not in skip].index("id") if name == "facts" and gone else None
    src_rows = {json.dumps(list(r), ensure_ascii=False, default=str) for r in src.execute(f"SELECT {sel} FROM {name}")
                if id_pos is None or str(r[id_pos]) not in gone}
    dst_rows = {json.dumps(list(r), ensure_ascii=False, default=str) for r in dst.execute(f"SELECT {sel} FROM {name}")}
    return {"source": len(rows), "inserted": after - before, "target": after,
            "source_digest": _digest(json.loads(x) for x in src_rows), "missing_in_target": len(src_rows - dst_rows)}


def qdrant_points(url: str, path: str, collection: str):
    from qdrant_client import QdrantClient
    tmp = None
    if path:                                   # 本地模式:复制后读,不抢源目录锁
        tmp = tempfile.mkdtemp(prefix="mig_qdrant_")
        shutil.copytree(path, os.path.join(tmp, "q"), ignore=shutil.ignore_patterns(".lock"))
        client = QdrantClient(path=os.path.join(tmp, "q"))
    else:
        client = QdrantClient(url=url)
    try:
        offset = None
        while True:
            pts, offset = client.scroll(collection, limit=256, offset=offset, with_payload=True, with_vectors=True)
            for p in pts:
                vec = p.vector.get("", None) if isinstance(p.vector, dict) else p.vector
                if isinstance(p.vector, dict) and vec is None:     # 命名向量:取第一个稠密向量
                    vec = next((v for v in p.vector.values() if isinstance(v, list)), None)
                yield str(p.id), dict(p.payload or {}), vec
            if offset is None:
                break
    finally:
        client.close()
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def import_memories(nm, points, fts_rows) -> dict:
    rep = {"qdrant_points": 0, "fts_rows": 0, "inserted_from_qdrant": 0, "inserted_from_fts_only": 0,
           "need_embed": 0, "skipped_existing": 0, "skipped_empty": 0, "skipped_same_content": 0}
    seen, qhashes = set(), set()
    for pid, pl, vec in points:
        rep["qdrant_points"] += 1
        text = pl.get("data") or ""
        if not text.strip():
            rep["skipped_empty"] += 1
            continue
        md = {k: v for k, v in pl.items() if k not in _CORE}
        ok = nm.insert(pid, text, user_id=str(pl.get("user_id") or "default"), metadata=md,
                       agent_id=pl.get("agent_id"), run_id=pl.get("run_id"), role=pl.get("role") or "user",
                       vector=vec or [], created_at=pl.get("created_at"), history=False)
        seen.add((pid, str(pl.get("user_id") or "default"), str(pl.get("bank_id") or "default")))
        qhashes.add((str(pl.get("user_id") or "default"), str(pl.get("bank_id") or "default"),
                     hashlib.md5(text.encode()).hexdigest()))
        rep["inserted_from_qdrant" if ok else "skipped_existing"] += 1
    for r in fts_rows:
        rep["fts_rows"] += 1
        key = (r["id"], r["user_id"] or "default", r["bank_id"] or "default")
        if key in seen or not (r["content"] or "").strip():
            continue
        h = hashlib.md5(r["content"].encode()).hexdigest()
        if (key[1], key[2], h) in qhashes:
            # 旧双写镜像(如 core::* 与 Qdrant uuid5 同文)只留一份。f0.7 P0-1(外审 qwen F-1):只与本次从 Qdrant
            # 导入的行比;text_fts 里彼此同文、id 不同的独立记忆不再塌成一条,且合并记录逐条入报告供对账。
            rep["skipped_same_content"] += 1
            rep.setdefault("merged_mirrors", []).append(r["id"])
            continue
        ok = nm.insert(r["id"], r["content"], user_id=key[1],
                       metadata={"bank_id": key[2], "category": r["category"], "memory_type": r["memory_type"]},
                       vector=[], created_at=r["created_at"], history=False)
        if ok:
            rep["inserted_from_fts_only"] += 1
            rep["need_embed"] += 1
        else:
            rep["skipped_existing"] += 1
    return rep


def import_verbatim(nm, facts) -> dict:
    """原文抽屉并入召回面:verbatim_turns → memories(id=verbatim:N,memory_type=VERBATIM,同文去重)。

    旧栈里有一批原话只活在 verbatim 层(mem0 层因去重/门控没收),不并入则单库召回会丢它们。
    """
    rep = {"verbatim_rows": 0, "inserted": 0, "skipped_same_content": 0, "skipped_existing": 0, "skipped_tombstoned": 0}
    if not facts or "verbatim_turns" not in _tables(facts):
        return rep
    for r in facts.execute("SELECT id,user_id,bank_id,session_id,role,content,created_at FROM verbatim_turns"):
        rep["verbatim_rows"] += 1
        uid, bank = r["user_id"] or "default", r["bank_id"] or "default"
        if nm.conn.execute("SELECT 1 FROM memories WHERE user_id=? AND bank_id=? AND hash=? AND id<>? LIMIT 1",
                           (uid, bank, hashlib.md5(r["content"].encode()).hexdigest(), f"verbatim:{r['id']}")).fetchone():
            rep["skipped_same_content"] += 1
            continue
        if nm.conn.execute("SELECT 1 FROM tombstones WHERE user_id=? AND content_snapshot=? AND restored_at IS NULL"
                           " LIMIT 1", (uid, r["content"])).fetchone():
            rep["skipped_tombstoned"] += 1           # 删过的话不许借原文抽屉复活
            continue
        ok = nm.insert(f"verbatim:{r['id']}", r["content"], user_id=uid, role=r["role"] or "user", vector=[],
                       metadata={"bank_id": bank, "memory_type": "VERBATIM", "session_id": r["session_id"] or None},
                       created_at=r["created_at"], history=False)
        rep["inserted" if ok else "skipped_existing"] += 1
    return rep


def embed_missing(nm, embedder) -> int:
    """给没有向量(或改写后向量待补 _embed_pending)的记忆补嵌入。幂等:只处理缺的。

    f0.7 P0-5(外审 haiku P2-8):逐条独立提交,网络调用不在写事务里;单条失败记数后继续,不再一条超时整批回滚。
    返回成功补上的条数;失败条数写进 nm.last_embed_failures 供报告。"""
    from ducky.memory_kernel.impl_native import _pack
    rows = nm.conn.execute(
        "SELECT m.mid, m.content, m.metadata FROM memories m LEFT JOIN memory_vectors v ON v.mid=m.mid"
        " WHERE v.mid IS NULL OR json_extract(m.metadata, '$._embed_pending') = 1").fetchall()
    done = failed = 0
    for mid, text, md in rows:
        try:
            vec = embedder.embed(text, "add")
        except Exception as exc:          # 网络/配额:跳过这一条,下次再补
            failed += 1
            print(f"embed_missing: mid={mid} skipped ({type(exc).__name__})", file=sys.stderr)
            continue
        if not vec:
            failed += 1
            continue
        meta = json.loads(md or "{}")
        meta.pop("_embed_pending", None)
        with nm.conn:
            nm.conn.execute("INSERT OR REPLACE INTO memory_vectors VALUES (?,?,?)", (mid, len(vec), _pack(vec)))
            nm.conn.execute("UPDATE memories SET metadata=? WHERE mid=?",
                            (json.dumps(meta, ensure_ascii=False, default=str), mid))
        done += 1
    nm.last_embed_failures = failed
    return done


def normalize_candidates(c) -> dict:
    """candidate_facts 归一:旧写入把「写入方」(如 pattern_extract)塞进 user_id、租户放在 scope_user_id。

    归一后 user_id = scope_user_id = 租户(优先取被治理 facts 行的 user_id/bank_id),写入方移到 source 列。
    幂等:已归一的行不再变化。
    """
    if "candidate_facts" not in {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
        return {"rows": 0, "normalized": 0}
    if "source" not in {r[1] for r in c.execute("PRAGMA table_info(candidate_facts)")}:
        c.execute("ALTER TABLE candidate_facts ADD COLUMN source TEXT DEFAULT ''")
    tenants = {r[0] for r in c.execute("SELECT DISTINCT user_id FROM facts")} | \
              {r[0] for r in c.execute("SELECT DISTINCT user_id FROM memories")}
    n = fixed = 0
    rows = c.execute("SELECT cf.candidate_id, cf.user_id, cf.scope_user_id, cf.bank_id, cf.source,"
                     " f.user_id fu, f.bank_id fb FROM candidate_facts cf LEFT JOIN facts f ON f.id=cf.fact_id").fetchall()
    with c:
        for r in rows:
            n += 1
            tenant = r["fu"] or (r["scope_user_id"] if r["scope_user_id"] not in ("", "default") or
                                 r["user_id"] not in tenants else r["user_id"])
            bank = r["fb"] or r["bank_id"] or "default"
            source = r["user_id"] if r["user_id"] != tenant else (r["source"] or "")
            if (r["user_id"], r["scope_user_id"], r["bank_id"], r["source"] or "") != (tenant, tenant, bank, source):
                c.execute("UPDATE candidate_facts SET user_id=?, scope_user_id=?, bank_id=?, source=? WHERE candidate_id=?",
                          (tenant, tenant, bank, source, r["candidate_id"]))
                fixed += 1
    return {"rows": n, "normalized": fixed}


def link_derived_facts(c) -> int:
    """给存量 pattern 事实补「来自哪条记忆」的链接(确定性重抽取,值完全一致才链),切 native 后改/删可级联。"""
    from ducky.pattern_extract import PATTERN_EXTRACT_SOURCE, extract_patterns
    before = c.execute("SELECT COUNT(*) FROM memory_facts").fetchone()[0]
    with c:
        for mid, text, uid, bank, created in c.execute(
                "SELECT mid, content, user_id, bank_id, created_at FROM memories").fetchall():
            for it in extract_patterns(text, recorded_at=created):
                c.execute("INSERT OR IGNORE INTO memory_facts SELECT ?, id FROM facts WHERE user_id=? AND bank_id=?"
                          " AND category=? AND fact_key=? AND fact_value=? AND source=?",
                          (mid, uid, bank, it["category"], it["fact_key"], it["fact_value"], PATTERN_EXTRACT_SOURCE))
    return c.execute("SELECT COUNT(*) FROM memory_facts").fetchone()[0] - before


def orphan_facts(c, prune: bool) -> dict:
    """没有任何记忆链接的 pattern 事实。能追溯到已删/已改旧文的,是旧栈 /update /delete 漏级联留下的旧值;
    prune=True 时在 memory.db 里墓碑后删除(源库不动)。"""
    from ducky.pattern_extract import PATTERN_EXTRACT_SOURCE, extract_patterns
    orph = {(r[1], r[2], r[3], r[4], r[5]): r[0] for r in c.execute(
        "SELECT id, user_id, bank_id, category, fact_key, fact_value FROM facts f WHERE source=? AND NOT EXISTS"
        " (SELECT 1 FROM memory_facts l WHERE l.fact_id=f.id)", (PATTERN_EXTRACT_SOURCE,))}
    stale = set()
    for uid, bank, text in c.execute(
            "SELECT user_id, bank_id, content_snapshot FROM tombstones WHERE content_snapshot IS NOT NULL UNION ALL"
            " SELECT user_id, bank_id, old_memory FROM memory_history WHERE event IN ('UPDATE','DELETE')"
            " AND old_memory IS NOT NULL").fetchall():
        for it in extract_patterns(text or ""):
            for key in ((uid, bank), (uid or "default", bank or "default")):
                k = (*key, it["category"], it["fact_key"], it["fact_value"])
                if k in orph:
                    stale.add(orph[k])
    if prune and stale:
        with c:
            for fid in stale:
                f = c.execute("SELECT user_id, bank_id, fact_key, fact_value FROM facts WHERE id=?", (fid,)).fetchone()
                c.execute("INSERT INTO tombstones (target_id,target_type,user_id,bank_id,content_snapshot,reason,actor,"
                          "tombstoned_at) VALUES (?,?,?,?,?,?,?,datetime('now'))",
                          (str(fid), "fact", f[0], f[1], f"{f[2]}={f[3]}", "orphan of deleted/updated memory", "migrate"))
                c.execute("DELETE FROM facts WHERE id=?", (fid,))
    return {"orphans": len(orph), "stale_from_deleted_or_updated": len(stale), "pruned": len(stale) if prune else 0}


def backfill_edges(c) -> dict:
    """历史数据补边(幂等,INSERT OR IGNORE):更新链 / 同会话相邻 / 原文同会话。共现由 insert 已写。"""
    c.execute("""INSERT OR IGNORE INTO graph_edges (src_type,src_id,dst_type,dst_id,edge_type,user_id,bank_id)
                 SELECT 'memory_version', h.id, 'memory', h.memory_id, 'update_chain', m.user_id, m.bank_id
                 FROM memory_history h JOIN memories m ON m.id=h.memory_id WHERE h.event='UPDATE'""")
    c.execute("""INSERT OR IGNORE INTO graph_edges (src_type,src_id,dst_type,dst_id,edge_type,user_id,bank_id)
                 SELECT 'memory', prev_id, 'memory', id, 'same_session', user_id, bank_id FROM (
                   SELECT id, user_id, bank_id, LAG(id) OVER (PARTITION BY user_id, bank_id, session_id
                                                             ORDER BY created_at, mid) prev_id
                   FROM memories WHERE session_id IS NOT NULL AND session_id<>'') WHERE prev_id IS NOT NULL""")
    if "verbatim_turns" in _tables(c):
        c.execute("""INSERT OR IGNORE INTO graph_edges (src_type,src_id,dst_type,dst_id,edge_type,user_id,bank_id)
                     SELECT 'verbatim', CAST(prev_id AS TEXT), 'verbatim', CAST(id AS TEXT), 'same_session',
                            user_id, bank_id FROM (
                       SELECT id, user_id, bank_id, LAG(id) OVER (PARTITION BY user_id, bank_id, session_id
                                                                 ORDER BY id) prev_id
                       FROM verbatim_turns WHERE session_id IS NOT NULL AND session_id<>'') WHERE prev_id IS NOT NULL""")
    c.commit()
    return {et: n for et, n in c.execute("SELECT edge_type, COUNT(*) FROM graph_edges GROUP BY 1")}


def migrate(data_dir: str, out: str, qdrant_url: str = "", qdrant_path: str = "", collection: str = "",
            embedder=None, prune_orphan_facts: bool = False) -> dict:
    from ducky.memory_kernel.impl_native import NativeMemory
    if os.path.realpath(out).startswith(os.path.realpath(data_dir) + os.sep) and os.environ.get("MIGRATE_ALLOW_IN_PLACE") != "1":
        raise SystemExit("拒绝把 memory.db 写进源 DATA_DIR(影子期另行部署;确需请设 MIGRATE_ALLOW_IN_PLACE=1)")
    src_before = source_fingerprint(data_dir)
    nm = NativeMemory(path=out, embedder=None)
    c = nm.conn
    report: dict = {"out": out, "tables": {}}
    fts = _ro(os.path.join(data_dir, "text_fts.db"))
    fts_rows = fts.execute("SELECT id,content,user_id,bank_id,category,memory_type,created_at FROM memories").fetchall() if fts and "memories" in _tables(fts) else []
    points = qdrant_points(qdrant_url, qdrant_path, collection) if (qdrant_url or qdrant_path) and collection else iter(())
    report["memories"] = import_memories(nm, points, fts_rows)
    hist = _ro(os.path.join(data_dir, "history.db"))
    if hist and "history" in _tables(hist):
        rows = hist.execute("SELECT id,memory_id,old_memory,new_memory,event,created_at,updated_at,is_deleted,"
                            "actor_id,role FROM history").fetchall()
        before = c.execute("SELECT COUNT(*) FROM memory_history").fetchone()[0]
        with c:
            c.executemany("INSERT OR IGNORE INTO memory_history (id,memory_id,old_memory,new_memory,event,created_at,"
                          "updated_at,is_deleted,actor_id,role) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
        report["tables"]["history->memory_history"] = {"source": len(rows), "inserted":
                                                       c.execute("SELECT COUNT(*) FROM memory_history").fetchone()[0] - before}
    facts = _ro(os.path.join(data_dir, "facts.db"))
    if facts:
        present = _tables(facts)
        with c:
            for t in FACTS_TABLES:
                if t in present:
                    report["tables"][t] = copy_table(facts, c, t)
    report["verbatim"] = import_verbatim(nm, facts)
    report["candidate_facts"] = normalize_candidates(c)
    report["derived_fact_links_added"] = link_derived_facts(c)
    report["orphan_pattern_facts"] = orphan_facts(c, prune_orphan_facts)
    report["embedded_missing"] = embed_missing(nm, embedder) if embedder is not None else 0
    report["embed_failures"] = getattr(nm, "last_embed_failures", 0)
    report["graph_edges"] = backfill_edges(c)
    # 对账:每条 Qdrant/text_fts 记忆在 memory.db 里内容一致
    src = {(r["id"], r["user_id"] or "default"): r["content"] for r in fts_rows}
    dst = {(r[0], r[1]): r[2] for r in c.execute("SELECT id,user_id,content FROM memories")}
    dst_hash = {(r[0], r[1]) for r in c.execute("SELECT user_id, hash FROM memories")}
    merged = set(report["memories"].get("merged_mirrors") or [])
    missing = [k for k, v in src.items() if k not in dst and not (k[0] in merged and
                                                                  (k[1], hashlib.md5(v.encode()).hexdigest()) in dst_hash)]
    report["reconcile"] = {"fts_rows": len(src), "missing_in_native": len(missing),
                           "content_mismatch": sum(1 for k, v in src.items() if k in dst and dst[k] != v),
                           "native_total": len(dst),
                           "vectors": c.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0]}
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    for conn in (fts, hist, facts):
        if conn is not None:
            conn.close()
    src_after = source_fingerprint(data_dir)
    report["source_check"] = {k: {"before": v, "after": src_after.get(k), "same": v == src_after.get(k)}
                              for k, v in src_before.items()}
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--qdrant-url", default="")
    ap.add_argument("--qdrant-path", default="")
    ap.add_argument("--collection", default="")
    ap.add_argument("--json", default="")
    ap.add_argument("--prune-orphan-facts", action="store_true",
                    help="在 memory.db 里墓碑并删除能追溯到已删/已改旧文的孤儿 pattern 事实(源库不动)")
    ap.add_argument("--embed-missing", action="store_true", help="用 mem0 同一份 embedder 配置给缺向量行补嵌入(会外呼)")
    a = ap.parse_args(argv)
    emb = None
    if a.embed_missing:
        from ducky.memory_kernel.impl_native import HttpEmbedder
        from ducky.mem0_runtime import _read_resolved_mem0_config
        emb = HttpEmbedder(_read_resolved_mem0_config().get("embedder") or {})
    rep = migrate(a.data_dir, a.out, a.qdrant_url, a.qdrant_path, a.collection, embedder=emb,
                  prune_orphan_facts=a.prune_orphan_facts)
    txt = json.dumps(rep, ensure_ascii=False, indent=1)
    if a.json:
        open(a.json, "w", encoding="utf-8").write(txt)
    print(txt)
    bad = rep["reconcile"]["missing_in_native"] + rep["reconcile"]["content_mismatch"]
    bad += sum(t.get("missing_in_target", 0) for t in rep["tables"].values())
    bad += sum(1 for v in rep.get("source_check", {}).values() if not v["same"])   # 源库逻辑内容被改 = 红
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
