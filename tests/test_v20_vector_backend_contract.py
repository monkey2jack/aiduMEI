"""v20 向量后端契约：体检必须真探过，且不得留下副作用。"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

import pytest

from ducky.vector_backend import (
    BackendError,
    BackendUnavailable,
    SQLiteVecBackend,
    backend_health,
)


_TMP = tempfile.mkdtemp(prefix="aidumem_v20_vec_")


def _tmp_path(name: str) -> str:
    return os.path.join(_TMP, f"{os.urandom(6).hex()}_{name}")


class _FakeInfo:
    points_count = 42


class _FakeClient:
    def __init__(self, exc: Exception | None = None):
        self.exc = exc
        self.calls: list[str] = []

    def get_collection(self, collection):
        self.calls.append(collection)
        if self.exc:
            raise self.exc
        return _FakeInfo()


class _FakeStore:
    def __init__(self, client, collection="mem0_facts"):
        self.client = client
        self.collection_name = collection


class _FakeMem:
    def __init__(self, store):
        self.vector_store = store


@pytest.fixture
def _no_singleton(monkeypatch):
    """确保测试之间不串用真实单例。"""
    monkeypatch.delenv("AIDUMEM_VECTOR_BACKEND", raising=False)
    monkeypatch.setattr(sys, "_aidumem_singleton", None, raising=False)
    yield


def _install_fake_singleton(monkeypatch, store):
    import ducky.mem0_runtime as mr

    monkeypatch.setattr(sys, "_aidumem_singleton", _FakeMem(store), raising=False)
    monkeypatch.setattr(mr, "is_mem_ready", lambda: True)


# ── V-1：默认（qdrant）分支的假绿灯 ──────────────────────────────

def test_qdrant_health_is_not_green_without_an_actual_probe(_no_singleton, monkeypatch):
    """单例没起时，绝不许报 ok=True。

    ⚠️ 原实现在这条分支上直接 ``return {"ok": True, ...}`` —— 生产默认走的
    正是这里。Qdrant 是死是活、集合在不在，``/health`` 一律报
    ``vector_backend_ok: true``。这项探针从来没探过任何东西，出事时体检
    还在报平安。
    """
    import ducky.mem0_runtime as mr

    monkeypatch.setattr(mr, "is_mem_ready", lambda: False)
    result = backend_health()

    assert result["backend"] == "qdrant"
    assert result["ok"] is False, "没探测就报 ok=True —— 假绿灯"
    assert result["probed"] is False, "必须显式声明「没探过」"
    assert result["degraded"] == [], "冷启动不是故障，不该记降级"


def test_qdrant_health_reports_a_real_probe_when_the_client_is_live(_no_singleton, monkeypatch):
    """单例活着时，ok=True 必须**有证据**：确实查到了集合。"""
    client = _FakeClient()
    _install_fake_singleton(monkeypatch, _FakeStore(client))

    result = backend_health()

    assert client.calls == ["mem0_facts"], "根本没去查集合，绿灯就是猜的"
    assert result["ok"] is True and result["probed"] is True
    assert result["points"] == 42, "证据字段缺失，运维无从判断这灯凭什么绿"


def test_qdrant_probe_failure_is_a_named_degradation(_no_singleton, monkeypatch):
    """客户端在、查询炸了 —— 这是真故障，必须红灯且记名降级。"""
    _install_fake_singleton(monkeypatch, _FakeStore(_FakeClient(RuntimeError("connection refused"))))

    result = backend_health()

    assert result["ok"] is False and result["probed"] is True
    assert result["degraded"] == ["vector_backend"]
    assert "connection refused" in result["error"]


def test_health_probe_never_constructs_the_mem0_singleton(_no_singleton, monkeypatch):
    """体检不许顺手把重量级单例建起来（会读配置、清 Qdrant 锁、连 LLM）。"""
    import ducky.mem0_runtime as mr

    called = []
    monkeypatch.setattr(mr, "is_mem_ready", lambda: False)
    monkeypatch.setattr(mr, "get_memory", lambda: called.append("boom"))

    backend_health()

    assert called == [], "体检把 mem0 单例建起来了 —— 体检不该有这种副作用"


# ── V-7：失败的体检不得在磁盘上留痕 ──────────────────────────────

def test_failed_extension_gate_leaves_no_files_on_disk(_no_singleton, monkeypatch):
    """扩展装不上 → 抛异常，且**一个字节都不许落盘**。

    ⚠️ 原 ``__post_init__`` 的顺序是「建目录 → connect → PRAGMA WAL →
    CREATE TABLE → 才去验扩展」。于是 ``backend_health()`` 这种只想问一句
    「这台机器行不行」的调用，哪怕结论是「不行」，也已经建好了
    vectors.sqlite 及 -wal/-shm，还漏了个没人 close 的连接。
    """
    monkeypatch.setenv("AIDUMEM_SQLITE_VEC_EXTENSION", "/nonexistent/sqlite_vec.so")
    path = _tmp_path("vectors.sqlite")

    with pytest.raises(BackendUnavailable):
        SQLiteVecBackend(path, require_extension=True)

    leftovers = [p for p in (path, path + "-wal", path + "-shm") if os.path.exists(p)]
    assert leftovers == [], f"失败的体检在磁盘上留了痕: {leftovers}"


def test_unconfigured_extension_also_leaves_no_files(_no_singleton, monkeypatch):
    """连路径都没配的情况同样不许落盘。"""
    monkeypatch.delenv("AIDUMEM_SQLITE_VEC_EXTENSION", raising=False)
    path = _tmp_path("vectors2.sqlite")

    with pytest.raises(BackendUnavailable):
        SQLiteVecBackend(path, require_extension=True)

    assert not os.path.exists(path), "未配置扩展时仍然建了库文件"


def test_backend_health_sqlite_branch_stays_clean_on_failure(_no_singleton, monkeypatch):
    """走 backend_health 的完整链路，同样不许留痕，且必须记降级。"""
    path = _tmp_path("vectors3.sqlite")
    monkeypatch.setenv("AIDUMEM_VECTOR_BACKEND", "sqlite-vec")
    monkeypatch.setenv("AIDUMEM_SQLITE_VEC_PATH", path)
    monkeypatch.setenv("AIDUMEM_SQLITE_VEC_EXTENSION", "/nonexistent/sqlite_vec.so")

    result = backend_health()

    assert result["ok"] is False
    assert result["degraded"] == ["vector_backend"]
    assert not os.path.exists(path), "体检失败却建出了 vectors.sqlite"


# ── V-2：不许把「扩展装上了」读成「检索加速了」 ──────────────────

def test_health_admits_search_is_a_python_fullscan(_no_singleton):
    """`health()` 必须如实交代打分路径。"""
    backend = SQLiteVecBackend(_tmp_path("poc.sqlite"))
    try:
        backend.upsert("a", [1.0, 0.0], {"bank_id": "work"})
        info = backend.health()
    finally:
        backend.close()

    assert info["ok"] is True
    assert info["extension_loaded"] is False
    assert info["scoring"] == "python-cosine-fullscan", (
        "没写明打分路径，extension_loaded 会被读成「已经在用向量索引加速」"
    )


# ── snapshot 自毁防护 ────────────────────────────────────────────

def test_snapshot_refuses_to_target_the_source_database(_no_singleton):
    """备份到自己 = 静默自毁，必须报错而不是回一句「快照成功」。"""
    path = _tmp_path("src.sqlite")
    backend = SQLiteVecBackend(path)
    try:
        backend.upsert("a", [1.0, 0.0], {"k": "v"})
        with pytest.raises(BackendError):
            backend.snapshot(path)
        # 迂回写法（./ 前缀）同样要挡住
        with pytest.raises(BackendError):
            backend.snapshot(os.path.join(os.path.dirname(path), ".", os.path.basename(path)))
        # 正面锚点：源库还在，数据没被清空
        assert backend.count() == 1, "源库被自毁了"
    finally:
        backend.close()


def test_snapshot_to_a_real_destination_still_works(_no_singleton):
    """正面锚点：正常备份路径不能被上面的守卫误伤。"""
    src, dst = _tmp_path("s.sqlite"), _tmp_path("d.sqlite")
    backend = SQLiteVecBackend(src)
    try:
        backend.upsert("a", [1.0, 0.0], {"k": "v"})
        backend.snapshot(dst)
    finally:
        backend.close()

    conn = sqlite3.connect(dst)
    ids = {r[0] for r in conn.execute("SELECT id FROM vectors").fetchall()}
    conn.close()
    assert ids == {"a"}, f"快照内容不对: {ids}"


# ── restore：备份取不出来就不叫备份 ──────────────────────────────

_SEVEN = ("upsert", "search", "delete", "count", "health", "snapshot", "restore")


def test_vector_backend_contract_declares_all_seven_methods(_no_singleton):
    """基准 §3.2 点名七个方法。少一个 restore，「恢复演练」这项门禁就无法表达。"""
    from ducky.vector_backend import QdrantBackend, VectorBackend

    for holder in (VectorBackend, SQLiteVecBackend, QdrantBackend):
        missing = [name for name in _SEVEN if not callable(getattr(holder, name, None))]
        assert not missing, f"{holder.__name__} 缺少契约方法: {missing}"
        # 负向对照：防止 getattr 恒真把上面这句变成空断言
        assert getattr(holder, "restore_all_the_things", None) is None, \
            f"{holder.__name__} 上凭空长出了不存在的方法，这条断言是假的"


def test_restore_is_the_true_inverse_of_snapshot(_no_singleton):
    """往返演练：snapshot → 改坏 → restore → ID / payload / 向量逐项还原。"""
    src, snap = _tmp_path("live.sqlite"), _tmp_path("snap.sqlite")
    backend = SQLiteVecBackend(src)
    try:
        backend.upsert("a", [1.0, 0.0], {"bank_id": "default", "text": "甲"})
        backend.upsert("b", [0.0, 1.0], {"bank_id": "default", "text": "乙"})
        before = {r["id"]: r["payload"] for r in backend.search([1.0, 1.0], top_k=10)}
        backend.snapshot(snap)

        # 把现场改坏：删一条、加一条、再改一条的 payload
        assert backend.delete(["a"]) == 1
        backend.upsert("c", [1.0, 1.0], {"bank_id": "other", "text": "丙"})
        backend.upsert("b", [0.0, 1.0], {"bank_id": "other", "text": "被改坏了"})
        assert backend.count() == 2
        assert {r["id"] for r in backend.search([1.0, 1.0], top_k=10)} == {"b", "c"}

        restored = backend.restore(snap)
        assert restored == 2, f"restore 返回条数不对: {restored}"
        after = {r["id"]: r["payload"] for r in backend.search([1.0, 1.0], top_k=10)}
        assert after == before, f"恢复后与快照时刻不一致: {after} != {before}"
        assert backend.count() == 2
        # 被 restore 覆盖掉的那条新数据必须真的没了
        assert backend.count(filters={"bank_id": "other"}) == 0, "restore 没有覆盖掉后来的写入"
    finally:
        backend.close()


def test_restore_refuses_a_missing_snapshot_instead_of_returning_zero(_no_singleton):
    """源不存在必须炸。返回 0 会被读成「恢复成功、只是空的」。"""
    backend = SQLiteVecBackend(_tmp_path("live2.sqlite"))
    try:
        backend.upsert("a", [1.0, 0.0], {"k": "v"})
        with pytest.raises(BackendError):
            backend.restore(_tmp_path("does_not_exist.sqlite"))
        assert backend.count() == 1, "失败的 restore 动了现场"
    finally:
        backend.close()


def test_restore_refuses_a_corrupt_snapshot_and_leaves_the_store_untouched(_no_singleton):
    """先清空、再发现快照是垃圾 = 拿坏备份擦掉生产数据。校验必须在覆盖之前。"""
    bad = _tmp_path("garbage.sqlite")
    with open(bad, "wb") as fh:
        fh.write(b"this is definitely not a sqlite database\n" * 8)

    backend = SQLiteVecBackend(_tmp_path("live3.sqlite"))
    try:
        backend.upsert("keep", [1.0, 0.0], {"k": "v"})
        with pytest.raises(BackendError):
            backend.restore(bad)
        assert {r["id"] for r in backend.search([1.0, 0.0], top_k=10)} == {"keep"}, "坏快照把现场擦了"
    finally:
        backend.close()


def test_restore_refuses_a_foreign_sqlite_database(_no_singleton):
    """是个正经 SQLite，但不是本契约的库：同样要在覆盖之前失败。"""
    foreign = _tmp_path("foreign.sqlite")
    conn = sqlite3.connect(foreign)
    conn.execute("CREATE TABLE unrelated(id TEXT)")
    conn.commit()
    conn.close()

    backend = SQLiteVecBackend(_tmp_path("live4.sqlite"))
    try:
        backend.upsert("keep", [1.0, 0.0], {"k": "v"})
        with pytest.raises(BackendError):
            backend.restore(foreign)
        assert backend.count() == 1, "外来库把现场擦了"
    finally:
        backend.close()


def test_restore_refuses_the_live_database_as_its_own_source(_no_singleton):
    """自己恢复自己：和 snapshot 到自身一样，是静默自毁。"""
    path = _tmp_path("live5.sqlite")
    backend = SQLiteVecBackend(path)
    try:
        backend.upsert("a", [1.0, 0.0], {"k": "v"})
        with pytest.raises(BackendError):
            backend.restore(path)
        with pytest.raises(BackendError):
            backend.restore(os.path.join(os.path.dirname(path), ".", os.path.basename(path)))
        assert backend.count() == 1, "源库被自毁了"
    finally:
        backend.close()


def test_qdrant_restore_refuses_instead_of_faking_success(_no_singleton):
    """Qdrant 侧不提供「看着像恢复」的假实现，与 snapshot 对称地拒绝。"""
    from ducky.vector_backend import QdrantBackend

    backend = QdrantBackend(_FakeClient(), "mem0_facts")
    with pytest.raises(BackendError):
        backend.snapshot("/tmp/whatever.snapshot")
    with pytest.raises(BackendError):
        backend.restore("/tmp/whatever.snapshot")
