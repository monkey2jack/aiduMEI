"""f0.7 P0 数据安全回归(七路外审复现用例,先红后绿)。

编号对应 f0.7 任务书 §3 P0;每条用例的来源审计写在 docstring 里。
"""
import sqlite3
import threading
import time

import pytest

from ducky.memory_kernel.impl_native import NativeMemory


class _Emb:
    def __init__(self, fail=False, delay=0.0):
        self.fail, self.delay = fail, delay

    def embed(self, text, action=None):
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("embedding down")
        v = [0.0] * 8
        for ch in str(text):
            v[ord(ch) % 8] += 1.0
        return v


@pytest.fixture
def mem(tmp_path):
    return NativeMemory(path=str(tmp_path / "memory.db"), embedder=_Emb())


def _ids(m, uid="u"):
    return {r["id"] for r in m.get_all(filters={"user_id": uid}, limit=500)["results"]}


# ── P0-1 同文连带删(haiku P1-1 / qwen F-1 / ds A-03 / muse P1-04)──────────────
def test_p0_1_independent_same_text_memories_survive_delete(mem):
    ids = [mem.add("好的", user_id="u", metadata={"session_id": f"s{i}"})["results"][0]["id"] for i in range(5)]
    res = mem.delete(ids[0])
    assert res["collateral_deleted"] == []
    assert _ids(mem) == set(ids[1:])


def test_p0_1_independent_same_text_memories_survive_update(mem):
    a = mem.add("我喜欢喝咖啡", user_id="u")["results"][0]["id"]
    b = mem.add("我喜欢喝咖啡", user_id="u")["results"][0]["id"]
    mem.update(a, "我改喝茶了")
    assert mem.get(b)["memory"] == "我喜欢喝咖啡"


def test_p0_1_user_and_assistant_turn_with_same_text_are_independent(mem):
    r = mem.add([{"role": "user", "content": "同一句话"}, {"role": "assistant", "content": "同一句话"}],
                user_id="u")["results"]
    mem.delete(r[0]["id"])
    assert mem.get(r[1]["id"]) is not None


def test_p0_1_verbatim_copy_still_cleared_and_reported(mem):
    a = mem.add("我的测试城市是上海", user_id="u")["results"][0]["id"]
    mem.insert("verbatim:7", "我的测试城市是上海", user_id="u", metadata={"memory_type": "VERBATIM"})
    res = mem.update(a, "我的测试城市是北京")
    assert res["collateral_deleted"] == ["verbatim:7"] and mem.get("verbatim:7") is None


def test_p0_1_delete_all_with_duplicates_is_atomic_and_complete(mem):
    for _ in range(3):
        mem.add("好的", user_id="u")
    mem.insert("verbatim:1", "好的", user_id="u", metadata={"memory_type": "VERBATIM"})
    mem.add("别的", user_id="u")
    mem.add("bob 的", user_id="bob")
    assert mem.delete_all(user_id="u")["deleted"] >= 4
    assert _ids(mem) == set() and len(_ids(mem, "bob")) == 1


# ── P0-2 内核租户定位(六路共识;sesame F-01 / ds A-02 / muse P1-03,P1-05 / ds A-07)────────
def _two_tenants_same_id(m):
    m.insert("core::persona", "alice 的人设:医生", user_id="alice", vector=[])
    m.insert("core::persona", "bob 的人设:律师", user_id="bob", vector=[])


def test_p0_2_unscoped_access_to_ambiguous_id_is_refused(mem):
    _two_tenants_same_id(mem)
    for call in (lambda: mem.get("core::persona"), lambda: mem.delete("core::persona"),
                 lambda: mem.update("core::persona", "篡改")):
        with pytest.raises(ValueError, match="more than one tenant"):
            call()
    assert len(_ids(mem, "alice")) == 1 and len(_ids(mem, "bob")) == 1


def test_p0_2_scoped_calls_only_touch_own_tenant(mem):
    _two_tenants_same_id(mem)
    assert mem.get("core::persona", user_id="bob")["memory"] == "bob 的人设:律师"
    mem.update("core::persona", "bob 的人设:律师,搬到北京", user_id="bob")
    mem.delete("core::persona", user_id="bob")
    assert mem.get("core::persona", user_id="bob") is None
    assert mem.get("core::persona", user_id="alice")["memory"] == "alice 的人设:医生"
    assert [h["event"] for h in mem.history("core::persona", user_id="alice")] == ["ADD"]   # bob 的改删没碰她的历史


def test_p0_2_vector_store_insert_requires_tenant_and_scopes_upsert(mem):
    with pytest.raises(ValueError):
        mem.vector_store.insert(vectors=[[1.0] * 8], payloads=[{"data": "无主记忆"}], ids=["orphan-1"])
    mem.vector_store.insert(vectors=[[1.0] * 8], payloads=[{"data": "alice 块", "user_id": "alice"}], ids=["k1"])
    mem.vector_store.insert(vectors=[[1.0] * 8], payloads=[{"data": "bob 块", "user_id": "bob"}], ids=["k1"])
    assert mem.get("k1", user_id="alice")["memory"] == "alice 块"      # bob 的写没覆盖 alice
    assert mem.get("k1", user_id="bob")["memory"] == "bob 块"


def test_p0_2_vector_store_writes_keep_kernel_invariants(mem):
    mem.vector_store.insert(vectors=[[1.0] * 8], payloads=[{"data": "旧正文", "user_id": "u"}], ids=["v1"])
    mem.vector_store.update("v1", vector=None, payload={"data": "新正文", "user_id": "u"})
    assert [h["event"] for h in mem.history("v1")] == ["UPDATE"]                  # 旁路改也留历史
    assert mem.conn.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0] == 1   # 旧向量保留待补
    with pytest.raises(ValueError):
        mem.vector_store.delete("v1")                       # 复核 haiku N8:无租户旁路删一律拒绝
    mem.vector_store.delete("v1", user_id="u")
    assert mem.get("v1") is None
    assert mem.conn.execute("SELECT COUNT(*) FROM tombstones WHERE target_id='v1'").fetchone()[0] == 1


# ── P0-3 删除即擦除(sesame F-03)──────────────────────────────────────────
def test_p0_3_delete_erases_history_text_and_tombstone_has_ttl(mem):
    a = mem.add("我的银行卡密码提示是紫色长颈鹿", user_id="u")["results"][0]["id"]
    mem.update(a, "我的银行卡密码提示是紫色长颈鹿二号")
    mem.delete(a)
    c = mem.conn
    assert c.execute("SELECT COUNT(*) FROM memory_history WHERE old_memory LIKE '%长颈鹿%'"
                     " OR new_memory LIKE '%长颈鹿%'").fetchone()[0] == 0
    exp = c.execute("SELECT snapshot_expires_at FROM tombstones WHERE target_id=?", (a,)).fetchone()[0]
    assert exp and exp > time.time()


def test_p0_3_production_kernel_does_not_double_write_facts():
    """native 生产档事实单一真源 = facts.db(haiku P1-2 / ds A-05)。
    复核 haiku N7:原来的源码字符串断言改成行为验证,见 test_f07_reaudit.py::test_facts_single_source_by_behaviour;
    这里留内核层的直接证据:derive_facts=False 时 add 不往 memory.db 写事实。"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        m = NativeMemory(path=td + "/m.db", embedder=_Emb(), derive_facts=False)
        m.add("我的测试城市是苏州", user_id="u")
        assert m.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0


# ── P0-4 旧库重复事实键不再让内核打不开(ds A-01)─────────────────────────────
def test_p0_4_legacy_duplicate_fact_keys_do_not_brick_the_kernel(tmp_path):
    p = str(tmp_path / "old.db")
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, agent_id TEXT DEFAULT 'default', category TEXT,"
              " fact_key TEXT, fact_value TEXT, source TEXT, created_at TEXT, updated_at TEXT)")
    c.execute("INSERT INTO facts(category,fact_key,fact_value,source,updated_at) VALUES"
              " ('a','k','旧','pattern_extract','2026-01-01'),('a','k','新','manual','2026-02-01')")
    c.commit()
    c.close()
    m = NativeMemory(path=p, embedder=None)
    assert [r[0] for r in m.conn.execute("SELECT fact_value FROM facts")] == ["新"]
    assert m.conn.execute("SELECT COUNT(*) FROM tombstones WHERE target_type='fact'").fetchone()[0] == 1


# ── P0-5 嵌入与锁(ds A-04 / haiku P2-7 / ds A-13)──────────────────────────
def test_p0_5_slow_embedding_does_not_freeze_reads(tmp_path):
    m = NativeMemory(path=str(tmp_path / "m.db"), embedder=_Emb())
    m.add("一条记忆", user_id="u")
    m.embedding_model = _Emb(delay=1.5)
    t = threading.Thread(target=lambda: m.search("记忆", filters={"user_id": "u"}))
    t.start()
    time.sleep(0.2)
    t0 = time.time()
    m.get_all(filters={"user_id": "u"})
    assert time.time() - t0 < 0.5
    t.join()


def test_p0_5_failed_reembed_on_update_keeps_old_vector(tmp_path):
    m = NativeMemory(path=str(tmp_path / "m.db"), embedder=_Emb())
    a = m.add("项目代号 Nova", user_id="u")["results"][0]["id"]
    m.embedding_model = _Emb(fail=True)
    m.update(a, "项目代号 Nova 二期")
    assert m.conn.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0] == 1
    assert m.get(a)["metadata"].get("_embed_pending") is True


def test_p0_5_missing_numpy_degrades_to_fts(tmp_path, monkeypatch):
    import ducky.memory_kernel.impl_native as nat
    m = NativeMemory(path=str(tmp_path / "m.db"), embedder=_Emb())
    m._sqlite_vec = False
    m.add("aiduMEI 端口 8767", user_id="u")

    def _boom(*a, **k):
        raise ImportError("numpy unavailable")
    monkeypatch.setattr(nat, "_cosines", _boom)
    hits = m.search("aiduMEI 端口", filters={"user_id": "u"})["results"]
    assert hits and hits[0]["memory"] == "aiduMEI 端口 8767"


# ── 迁移:同文不同 id 不塌缩(qwen F-1);补嵌入逐条容错(haiku P2-8)────────────────
def _old_text_fts(src, rows):
    import os
    os.makedirs(src, exist_ok=True)
    c = sqlite3.connect(os.path.join(src, "text_fts.db"))
    c.execute("CREATE TABLE memories (id TEXT, content TEXT, user_id TEXT, bank_id TEXT, category TEXT,"
              " memory_type TEXT, created_at TEXT, UNIQUE(id,user_id,bank_id))")
    c.executemany("INSERT INTO memories VALUES (?,?,?,?,?,?,?)", rows)
    c.commit()
    c.close()


def test_p0_1_migration_keeps_same_text_rows_with_distinct_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDUMEM_DATA_DIR", str(tmp_path / "live"))
    (tmp_path / "live").mkdir()
    src = str(tmp_path / "old")
    _old_text_fts(src, [(i, "同一句话", "u", "default", None, None, "2026-10-01") for i in ("id-a", "id-b", "id-c")])
    from scripts.migrate_to_native import migrate
    rep = migrate(src, str(tmp_path / "memory.db"))
    assert rep["memories"]["inserted_from_fts_only"] == 3 and rep["memories"]["skipped_same_content"] == 0
    assert rep["reconcile"]["missing_in_native"] == 0 and rep["reconcile"]["native_total"] == 3


def test_p0_5_embed_missing_survives_one_failure(tmp_path):
    from scripts.migrate_to_native import embed_missing
    m = NativeMemory(path=str(tmp_path / "m.db"), embedder=None)
    for t in ("第一条", "第二条", "第三条"):
        m.add(t, user_id="u")

    class _Flaky:
        n = 0

        def embed(self, text, action=None):
            _Flaky.n += 1
            if _Flaky.n == 2:
                raise TimeoutError("slow upstream")
            return [1.0] * 8
    assert embed_missing(m, _Flaky()) == 2 and m.last_embed_failures == 1
    assert m.conn.execute("SELECT COUNT(*) FROM memory_vectors").fetchone()[0] == 2
