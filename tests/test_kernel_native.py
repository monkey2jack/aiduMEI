"""f0.6+++ WP3a · native 单库内核:写读改删、租户隔离、图边同事务、影子不改主路。"""
from __future__ import annotations

import hashlib
import math

import pytest

from ducky.memory_kernel.impl_native import NativeMemory, ShadowMemory


class _Emb:
    def embed(self, text, action=None):
        v = [0.0] * 64
        s = str(text).lower()
        for g in list(s) + [s[i:i + 2] for i in range(len(s) - 1)]:
            v[int(hashlib.md5(g.encode()).hexdigest()[:6], 16) % 64] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]


@pytest.fixture()
def mem(tmp_path):
    return NativeMemory(path=str(tmp_path / "memory.db"), embedder=_Emb())


def _texts(res):
    return [r["memory"] for r in res["results"]]


def test_add_search_update_delete_roundtrip(mem):
    a = mem.add("aiduMEI 的生产端口是 8767", user_id="u", metadata={"_origin_session_id": "s1"})["results"][0]["id"]
    mem.add("用户喜欢在杭州喝咖啡", user_id="u", metadata={"_origin_session_id": "s1"})
    hit = mem.search("aiduMEI 端口", filters={"user_id": "u"}, top_k=2)["results"][0]
    assert hit["id"] == a and 0 < hit["score"] <= 1 and hit["score_details"]["bm25_rank"] == 1
    mem.update(a, "aiduMEI 的生产端口改为 8777")
    got = _texts(mem.search("aiduMEI 生产端口", filters={"user_id": "u"}, top_k=5))
    assert "aiduMEI 的生产端口改为 8777" in got and "aiduMEI 的生产端口是 8767" not in got
    mem.delete(a)
    assert mem.get(a) is None
    assert not any("8777" in t for t in _texts(mem.search("8777 端口", filters={"user_id": "u"})))
    c = mem.conn
    assert c.execute("SELECT COUNT(*) FROM tombstones WHERE target_id=?", (a,)).fetchone()[0] == 1
    assert c.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0] == 1          # CASCADE 跟删
    assert c.execute("SELECT COUNT(*) FROM graph_edges WHERE src_id=? OR dst_id=?", (a, a)).fetchone()[0] == 0
    # f0.7 P0-3:删除即擦除——只剩一行不含正文的 DELETE 标记,原文只在(受 TTL 约束的)墓碑里
    assert [(r["event"], r["old_memory"], r["new_memory"]) for r in mem.history(a)] == [("DELETE", None, None)]
    with pytest.raises(ValueError):
        mem.delete(a)


def test_tenant_isolation_and_bank(mem):
    mem.add("alice 的密码提示是蓝色鲸鱼", user_id="alice")
    mem.add("bob 的项目在 lab 机器", user_id="bob", metadata={"bank_id": "work"})
    assert "alice 的密码提示是蓝色鲸鱼" not in _texts(mem.search("蓝色鲸鱼", filters={"user_id": "bob"}))
    assert _texts(mem.search("lab 机器", filters={"user_id": "bob", "bank_id": "default"})) == []
    assert _texts(mem.search("lab 机器", filters={"user_id": "bob", "bank_id": "work"}))
    with pytest.raises(ValueError):
        mem.search("x", filters={})


def test_graph_edges_written_in_same_transaction(mem):
    ids = [mem.add(t, user_id="u", metadata={"_origin_session_id": "s9"})["results"][0]["id"]
           for t in ("aiduMEI 部署在 node-a", "aiduMEI 今天升级了")]
    mem.update(ids[0], "aiduMEI 迁到 lab")
    edges = {r[0] for r in mem.conn.execute("SELECT edge_type FROM graph_edges")}
    assert edges == {"same_session", "co_occurrence", "update_chain"}


def test_update_delete_cascade_to_derived_facts_and_copies(tmp_path):
    """/add 派生的 pattern 事实(测试城市=上海)必须随 update/delete 走;同文副本(verbatim:N)随之清掉;人工事实不被覆盖。"""
    m = NativeMemory(path=str(tmp_path / "memory.db"), embedder=_Emb(), derive_facts=True)
    facts = lambda: {(r[0], r[1]) for r in m.conn.execute("SELECT fact_key, fact_value FROM facts")}  # noqa: E731
    a = m.add("我的测试城市是上海", user_id="u")["results"][0]["id"]
    m.insert("verbatim:7", "我的测试城市是上海", user_id="u", metadata={"memory_type": "VERBATIM"})
    assert ("我的测试城市", "上海") in facts()
    m.update(a, "我的测试城市是北京")
    assert facts() == {("我的测试城市", "北京")} and m.get("verbatim:7") is None
    m.conn.execute("INSERT INTO facts (category,fact_key,fact_value,source,user_id,bank_id) "
                   "VALUES ('pattern_kv','我的口头禅','加油','manual','u','default')")
    b = m.add("我的口头禅是冲鸭", user_id="u")["results"][0]["id"]
    assert ("我的口头禅", "加油") in facts()                       # 人工来源同键不被 pattern 覆盖
    m.delete(a)
    m.delete(b)
    assert facts() == {("我的口头禅", "加油")}
    assert m.conn.execute("SELECT COUNT(*) FROM tombstones WHERE target_type='fact'").fetchone()[0] == 2
    off = NativeMemory(path=str(tmp_path / "shadow.db"), embedder=_Emb())          # 影子档不派生
    off.add("我的测试城市是上海", user_id="u")
    assert off.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0


def test_shadow_returns_primary_and_mirrors_writes(mem, tmp_path):
    class _Primary:
        embedding_model = _Emb()
        marker = "mem0"

        def __init__(self):
            self.rows = {}

        def add(self, text, **kw):
            self.rows["p1"] = text
            return {"results": [{"id": "p1", "memory": text, "event": "ADD"}]}

        def search(self, q, **kw):
            return {"results": [{"id": k, "memory": v, "score": 0.9} for k, v in self.rows.items()]}

        def delete(self, mid):
            self.rows.pop(mid)

    sh = ShadowMemory(_Primary(), mem, log_dir=str(tmp_path / "shadow"))
    sh.add("影子写入一条", user_id="u")
    assert mem.get("p1")["memory"] == "影子写入一条"
    assert sh.search("影子", filters={"user_id": "u"})["results"][0]["score"] == 0.9    # 读的是主内核
    assert sh.marker == "mem0"                                                        # 其余属性透传
    sh.delete("p1")
    assert mem.get("p1") is None
    assert list((tmp_path / "shadow").glob("shadow-*.jsonl"))


def test_migration_idempotent_and_read_only(tmp_path):
    """旧库(text_fts/history/facts)→ memory.db:两次导入第二次 0 新增;源库字节不变;删过的原文不复活。"""
    import sqlite3
    from scripts.migrate_to_native import migrate
    src = tmp_path / "old"
    src.mkdir()
    with sqlite3.connect(src / "text_fts.db") as c:
        c.execute("CREATE TABLE memories (id TEXT, content TEXT, user_id TEXT, bank_id TEXT, category TEXT,"
                  " memory_type TEXT, created_at TEXT)")
        c.execute("INSERT INTO memories VALUES ('m1','aiduMEI 跑在 node-a','u','default',NULL,'FACTS','2026-10-01')")
    with sqlite3.connect(src / "history.db") as c:
        c.execute("CREATE TABLE history (id TEXT PRIMARY KEY, memory_id TEXT, old_memory TEXT, new_memory TEXT,"
                  " event TEXT, created_at TEXT, updated_at TEXT, is_deleted INTEGER, actor_id TEXT, role TEXT)")
        c.execute("INSERT INTO history VALUES ('h1','m1','aiduMEI 跑在 node-b','aiduMEI 跑在 node-a','UPDATE',"
                  "'2026-10-02',NULL,0,NULL,NULL)")
    with sqlite3.connect(src / "facts.db") as c:
        c.execute("CREATE TABLE verbatim_turns (id INTEGER PRIMARY KEY, user_id TEXT, bank_id TEXT, session_id TEXT,"
                  " role TEXT, content TEXT, created_at TEXT)")
        c.executemany("INSERT INTO verbatim_turns VALUES (?,?,?,?,?,?,?)",
                      [(1, "u", "default", "s1", "user", "原话一句 aiduMEI", None),
                       (2, "u", "default", "s1", "user", "已删除的原话", None)])
        c.execute("CREATE TABLE tombstones (tombstone_id INTEGER PRIMARY KEY, target_id TEXT, target_type TEXT,"
                  " user_id TEXT, bank_id TEXT, content_snapshot TEXT, reason TEXT, actor TEXT, tombstoned_at TEXT,"
                  " restored_at TEXT)")
        c.execute("INSERT INTO tombstones VALUES (1,'x','memory','u','default','已删除的原话','','u','t',NULL)")
        c.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, category TEXT, fact_key TEXT, fact_value TEXT,"
                  " source TEXT, user_id TEXT, bank_id TEXT)")
        c.execute("INSERT INTO facts VALUES (5,'pattern_kv','aiduMEI 跑在','node-a','pattern_extract','u','default')")
        c.execute("CREATE TABLE candidate_facts (candidate_id INTEGER PRIMARY KEY, fact_id INTEGER, fact_key TEXT,"
                  " category TEXT, fact_value TEXT, user_id TEXT, scope_user_id TEXT, bank_id TEXT, status TEXT)")
        c.execute("INSERT INTO candidate_facts VALUES (1,5,'aiduMEI 跑在','pattern_kv','node-a','pattern_extract',"
                  "'u','default','pending')")
    before = {p.name: p.read_bytes() for p in src.iterdir() if p.suffix == ".db"}
    out = str(tmp_path / "memory.db")
    r1, r2 = migrate(str(src), out, embedder=_Emb()), migrate(str(src), out, embedder=_Emb())
    assert r1["memories"]["inserted_from_fts_only"] == 1 and r1["verbatim"]["inserted"] == 1
    assert r1["verbatim"]["skipped_tombstoned"] == 1 and r1["reconcile"]["missing_in_native"] == 0
    assert r2["memories"]["inserted_from_fts_only"] == 0 and r2["verbatim"]["inserted"] == 0
    assert r2["tables"]["tombstones"]["inserted"] == 0 and r1["graph_edges"].get("update_chain") == 1
    assert before == {p.name: p.read_bytes() for p in src.iterdir() if p.suffix == ".db"}
    assert r1["source_check"] and all(v["same"] for v in r1["source_check"].values())
    assert r1["candidate_facts"] == {"rows": 1, "normalized": 1} and r2["candidate_facts"]["normalized"] == 0
    assert r2["tables"]["candidate_facts"]["missing_in_target"] == 0
    nm = NativeMemory(path=out, embedder=_Emb())
    assert tuple(nm.conn.execute("SELECT user_id, scope_user_id, source FROM candidate_facts").fetchone()) == \
        ("u", "u", "pattern_extract")
    hits = NativeMemory(path=out, embedder=_Emb()).search("aiduMEI", filters={"user_id": "u"})["results"]
    assert {h["memory"] for h in hits} == {"aiduMEI 跑在 node-a", "原话一句 aiduMEI"}


def test_source_check_compares_content_not_file_bytes(tmp_path):
    """WAL 被合进主文件 → 文件字节变了但逻辑内容没变,source_fingerprint 必须判相同;真改了内容判不同。"""
    import sqlite3
    from scripts.migrate_to_native import source_fingerprint
    db = tmp_path / "salience.db"
    keep = sqlite3.connect(db)
    keep.execute("PRAGMA journal_mode=WAL")
    keep.execute("PRAGMA wal_autocheckpoint=0")
    keep.execute("CREATE TABLE s (id TEXT, v INTEGER)")
    keep.execute("INSERT INTO s VALUES ('a', 1)")
    keep.commit()
    raw_before, fp_before = db.read_bytes(), source_fingerprint(str(tmp_path))
    keep.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    keep.close()
    assert db.read_bytes() != raw_before                      # 文件哈希会误报
    assert source_fingerprint(str(tmp_path)) == fp_before     # 内容指纹不误报
    with sqlite3.connect(db) as c:
        c.execute("UPDATE s SET v=2")
    assert source_fingerprint(str(tmp_path))["salience.db"] != fp_before["salience.db"]


def test_vector_store_adapter_mem0_shape(mem):
    """native 下旧模块摸 mem.vector_store.*(墓碑快照/恢复、CoreMemory 块、六型同步)走 memory.db。"""
    vs = mem.vector_store
    assert vs.client is None and vs.is_native
    vs.insert(vectors=[_Emb().embed("核心块 v1")], payloads=[{"data": "核心块 v1", "user_id": "u", "bank_id": "default",
                                                            "category": "core_memory"}], ids=["c-1"])
    vs.insert(vectors=[_Emb().embed("核心块 v2")], payloads=[{"data": "核心块 v2", "user_id": "u"}], ids=["c-1"])
    p = vs.get("c-1").payload
    assert (p["data"], p["user_id"], p["bank_id"]) == ("核心块 v2", "u", "default")           # 同 id = upsert
    assert mem.conn.execute("SELECT COUNT(*) FROM memories WHERE id='c-1'").fetchone()[0] == 1
    vs.set_payload("c-1", {"memory_type": "PREFERENCES"})
    assert vs.get("c-1").payload["memory_type"] == "PREFERENCES" and vs.get("c-1").payload["data"] == "核心块 v2"
    assert [o.id for o in vs.list(filters={"user_id": "u"})[0]] == ["c-1"]
    vs.delete("c-1", user_id="u")                  # f0.7 复核 haiku N8:删除必须带租户
    assert vs.get("c-1") is None and mem.conn.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0] == 0


def test_numpy_cosine_matches_definition_and_add_is_atomic(mem, monkeypatch):
    from ducky.memory_kernel.impl_native import _cosines, _pack
    a, b = [1.0, 0.0, 1.0], [1.0, 1.0, 0.0]
    got = _cosines([_pack(a), _pack([0.0, 0.0, 0.0])], b)
    assert abs(got[0] - 0.5) < 1e-6 and got[1] is None                     # 零向量不参与排序
    import sqlite3
    calls = {"n": 0}
    real = mem._write

    def boom(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise sqlite3.OperationalError("disk I/O")
        return real(*a, **kw)
    monkeypatch.setattr(mem, "_write", boom)
    with pytest.raises(sqlite3.OperationalError):
        mem.add([{"role": "user", "content": "第一句"}, {"role": "assistant", "content": "第二句"}], user_id="u")
    assert mem.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0   # 整笔回滚,无半写


def test_native_journal_failure_does_not_lock_scope(tmp_path, monkeypatch):
    """native 写失败 = 事务回滚 = 确定没写:journal 记 not_applied,作用域不进 repair_required(旧 409 锁死)。"""
    monkeypatch.setenv("AIDUMEI_MEMORY_KERNEL", "native")
    from ducky import mutation_journal as mj
    monkeypatch.setattr(mj, "journal_path", lambda: tmp_path / "mj.sqlite3")
    m = NativeMemory(path=str(tmp_path / "memory.db"), embedder=_Emb())
    mj.install_memory_journal(m)
    m.add("第一条", user_id="u")

    def broken(*a, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(m, "_write", broken)
    with pytest.raises(RuntimeError):
        m.add("第二条", user_id="u")
    monkeypatch.undo()
    monkeypatch.setenv("AIDUMEI_MEMORY_KERNEL", "native")
    monkeypatch.setattr(mj, "journal_path", lambda: tmp_path / "mj.sqlite3")
    with pytest.raises(ValueError):
        mj.execute_request("u", "default", {"x": 1}, lambda: (_ for _ in ()).throw(ValueError("later layer")))
    m.add("第三条", user_id="u")                                                       # 不被锁
    with mj._db() as c:
        states = {(r["kind"], r["state"]) for r in c.execute("SELECT kind, state FROM mutations")}
    assert ("mem0_add", "not_applied") in states and ("request", "not_applied") in states
    assert not any(s == "repair_required" for _k, s in states)


def test_native_to_mem0_resync_roundtrip(tmp_path):
    """回滚补写:native 期间新写/改/删 → Qdrant + history + text_fts;dry-run 不写,二次 apply 0 变更。"""
    import sqlite3
    import time
    import uuid
    from datetime import datetime, timezone
    import qdrant_client as qc
    from qdrant_client import models as qm
    from scripts.native_to_mem0_resync import resync
    d = tmp_path / "data"
    d.mkdir()
    old, a = "2026-10-20T08:00:00+00:00", str(uuid.uuid4())
    q = qc.QdrantClient(path=str(tmp_path / "q"))
    q.create_collection("c", vectors_config=qm.VectorParams(size=64, distance=qm.Distance.COSINE))
    q.upsert("c", points=[qm.PointStruct(id=a, vector=_Emb().embed("旧值"), payload={"data": "旧值", "user_id": "u"})])
    q.close()
    with sqlite3.connect(d / "history.db") as c:
        c.execute("CREATE TABLE history (id TEXT PRIMARY KEY, memory_id TEXT, old_memory TEXT, new_memory TEXT,"
                  " event TEXT, created_at TEXT, updated_at TEXT, is_deleted INTEGER, actor_id TEXT, role TEXT)")
    m = NativeMemory(path=str(d / "memory.db"), embedder=_Emb())
    m.insert(a, "旧值", user_id="u", created_at=old, history=False)
    time.sleep(0.01)
    since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
    time.sleep(0.01)
    b = m.add("native 期间新写", user_id="u")["results"][0]["id"]
    m.delete(a)
    m.conn.close()
    kw = dict(qdrant_path=str(tmp_path / "q"), collection="c")
    dry = resync(str(d), since, **kw)
    assert dry["qdrant"]["upsert"] == 1 and dry["qdrant"]["deleted"] == 1
    resync(str(d), since, apply=True, **kw)
    q = qc.QdrantClient(path=str(tmp_path / "q"))
    ids = {str(p.id) for p in q.scroll("c", limit=10)[0]}
    q.close()
    assert ids == {b}
    again = resync(str(d), since, apply=True, **kw)
    assert again["qdrant"]["upsert"] == 0 and again["qdrant"]["deleted"] == 0 and again["history_inserted"] == 0
