"""v20 vector-store contract and a reversible sqlite-vec proof of concept.

The running mem0 configuration still uses embedded Qdrant.  This module does
not silently replace it.  Instead it gives migration code and benchmark tests a
small, explicit interface whose semantics can be compared backend by backend.

``SQLiteVecBackend`` 的检索**始终是 Python 侧的全表余弦扫描**，
sqlite-vec 扩展目前只作为「这台机器具不具备条件」的显式闸门：
``require_extension=True`` 时扩展装不上就抛 :class:`BackendUnavailable`，
不会把「空结果」伪装成一次成功检索。但装上了也**不会**让检索走扩展 ——
`health()` 里的 ``scoring`` 字段如实写明这一点，免得有人把
``extension_loaded: True`` 读成「已经在用向量索引加速」。
（本模块定位是 POC 与影子比对，正确性优先于吞吐。）
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
import sqlite3
from typing import Any, Iterable, Protocol

from ducky.degradation import DegradationTracker
from ducky.utils import DATA_DIR


class BackendError(RuntimeError):
    """Base class for vector backend failures."""


class BackendUnavailable(BackendError):
    """The requested backend cannot be loaded on this host."""


class VectorBackend(Protocol):
    """Stable contract used by shadow/迁移 tooling."""

    def upsert(self, vector_id: str, vector: Iterable[float], payload: dict[str, Any] | None = None) -> None: ...
    def search(self, vector: Iterable[float], *, top_k: int = 5, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]: ...
    def delete(self, vector_ids: Iterable[str]) -> int: ...
    def count(self, filters: dict[str, Any] | None = None) -> int: ...
    def health(self) -> dict[str, Any]: ...
    def snapshot(self, destination: str) -> str: ...
    # 只有 snapshot 没有 restore 的抽象，是一个**取不出来的备份**：
    # 迁移门禁里的「恢复演练」和「备份可恢复」两项，靠这层根本没法表达。
    def restore(self, source: str) -> int: ...


def _clean_vector(vector: Iterable[float]) -> tuple[float, ...]:
    try:
        values = tuple(float(x) for x in vector)
    except (TypeError, ValueError) as exc:
        raise BackendError("vector 必须是有限数值序列") from exc
    if not values or not all(math.isfinite(x) for x in values):
        raise BackendError("vector 不能为空且必须全部为有限数值")
    return values


def _cosine(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    if len(a) != len(b):
        raise BackendError(f"向量维度不一致: {len(a)} != {len(b)}")
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if not na or not nb:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def _matches(payload: dict[str, Any], filters: dict[str, Any] | None) -> bool:
    """Exact-match payload filter shared by the POC and shadow tests."""
    if not filters:
        return True
    return all(payload.get(k) == value for k, value in filters.items())


@dataclass
class SQLiteVecBackend:
    """Small deterministic backend for POC/parity tests.

    Rows are stored as JSON vectors so the POC works on stock Python SQLite.
    When ``require_extension=True`` the configured sqlite-vec extension must
    load successfully; no fallback is attempted.  This makes extension
    availability and license/platform checks an explicit gate.
    """

    path: str
    require_extension: bool = False
    extension_path: str | None = None

    def __post_init__(self) -> None:
        self.extension_loaded = False
        # 🔴 先验扩展、再落盘。原顺序是「建目录→连库→WAL→建表→才验扩展」，
        # 于是 backend_health() 这类**只想问一句「这台机器行不行」**的调用，
        # 哪怕结论是「不行」，也已经在磁盘上留下了 vectors.sqlite 及 -wal/-shm
        # ——体检不该有副作用。改为先在内存库里试装，通过了才碰真路径。
        if self.require_extension:
            self._verify_extension_or_raise()

        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        try:
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS vectors (id TEXT PRIMARY KEY, vector TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}')"
            )
            self._conn.commit()
            if self.require_extension:
                self._load_extension_or_raise()
        except Exception:
            # 构造失败时对象不会交到调用方手里，没人替它 close()——
            # 这里自己收，避免连接（及 WAL 句柄）泄漏。
            try:
                self._conn.close()
            except Exception:
                pass
            raise

    def _verify_extension_or_raise(self) -> None:
        """在内存库上试装一次扩展，不触碰 self.path。"""
        path = self.extension_path or os.environ.get("AIDUMEM_SQLITE_VEC_EXTENSION", "")
        if not path:
            raise BackendUnavailable("sqlite-vec extension path is not configured")
        probe = sqlite3.connect(":memory:")
        try:
            probe.enable_load_extension(True)
            probe.load_extension(path)
        except Exception as exc:
            raise BackendUnavailable(f"sqlite-vec extension load failed: {exc}") from exc
        finally:
            probe.close()

    def _load_extension_or_raise(self) -> None:
        path = self.extension_path or os.environ.get("AIDUMEM_SQLITE_VEC_EXTENSION", "")
        if not path:
            raise BackendUnavailable("sqlite-vec extension path is not configured")
        try:
            self._conn.enable_load_extension(True)
            self._conn.load_extension(path)
            self.extension_loaded = True
        except Exception as exc:
            raise BackendUnavailable(f"sqlite-vec extension load failed: {exc}") from exc
        finally:
            try:
                self._conn.enable_load_extension(False)
            except Exception:
                pass

    def upsert(self, vector_id: str, vector: Iterable[float], payload: dict[str, Any] | None = None) -> None:
        if not str(vector_id).strip():
            raise BackendError("vector_id 不能为空")
        values = _clean_vector(vector)
        self._conn.execute(
            "INSERT INTO vectors(id, vector, payload) VALUES(?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET vector=excluded.vector, payload=excluded.payload",
            (str(vector_id), json.dumps(values), json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)),
        )
        self._conn.commit()

    def search(self, vector: Iterable[float], *, top_k: int = 5, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        query = _clean_vector(vector)
        limit = max(1, min(int(top_k), 1000))
        rows = []
        for row in self._conn.execute("SELECT id, vector, payload FROM vectors"):
            payload = json.loads(row["payload"] or "{}")
            if not _matches(payload, filters):
                continue
            score = _cosine(query, tuple(json.loads(row["vector"])))
            rows.append({"id": row["id"], "score": score, "payload": payload})
        rows.sort(key=lambda item: (-item["score"], item["id"]))
        return rows[:limit]

    def delete(self, vector_ids: Iterable[str]) -> int:
        ids = [str(x) for x in vector_ids if str(x).strip()]
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        cur = self._conn.execute(f"DELETE FROM vectors WHERE id IN ({marks})", ids)
        self._conn.commit()
        return int(cur.rowcount or 0)

    def count(self, filters: dict[str, Any] | None = None) -> int:
        if not filters:
            return int(self._conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0])
        return sum(1 for row in self._conn.execute("SELECT payload FROM vectors") if _matches(json.loads(row[0] or "{}"), filters))

    def health(self) -> dict[str, Any]:
        try:
            self._conn.execute("SELECT 1")
            return {
                "backend": "sqlite-vec",
                "ok": True,
                "extension_loaded": self.extension_loaded,
                # 扩展装上了也不代表检索走了它：如实写明打分路径，
                # 别让 extension_loaded=True 被读成「已加速」。
                "scoring": "python-cosine-fullscan",
                "count": self.count(),
            }
        except Exception as exc:
            return {"backend": "sqlite-vec", "ok": False, "error": str(exc)[:200]}

    def snapshot(self, destination: str) -> str:
        """Create an SQLite online-backup snapshot without mutating the source."""
        src = os.path.abspath(self.path)
        dst = os.path.abspath(destination)
        if src == dst:
            # 自己备份到自己：SQLite 会把源库当目标打开并清空后写入，
            # 是一次静默的自毁。宁可报错，也不给「快照成功」这四个字。
            raise BackendError(f"snapshot 目标不能是源库自身: {dst}")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        target = sqlite3.connect(destination)
        try:
            self._conn.backup(target)
            target.commit()
        finally:
            target.close()
        return destination

    def restore(self, source: str) -> int:
        """Restore this store **from** a snapshot: the exact inverse of :meth:`snapshot`.

        全部校验都发生在**覆盖之前**。一次「先清空、再发现快照是垃圾」的恢复，
        比不恢复更糟 —— 那是拿一个坏备份把生产数据擦掉。所以：
        源不存在、打不开、没有本契约的表，都在原地炸掉，现场保持不动。

        返回恢复出的条数。故意不返回 ``None``：让「什么都没恢复出来」
        没法被读成「恢复成功」。
        """
        src = os.path.abspath(source)
        dst = os.path.abspath(self.path)
        if src == dst:
            raise BackendError(f"restore 源不能是当前库自身: {src}")
        if not os.path.isfile(src):
            raise BackendError(f"restore 源快照不存在: {src}")
        probe = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        try:
            expected = int(probe.execute("SELECT COUNT(*) FROM vectors").fetchone()[0])
        except Exception as exc:
            # 文件损坏、或压根不是本契约的库：在动手之前失败。
            raise BackendError(f"restore 源快照不可用: {exc}") from exc
        finally:
            probe.close()

        # 校验通过才覆盖。用在线备份反向写回，活着的连接与句柄继续有效，
        # 不需要调用方重新 connect。
        source_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        try:
            source_conn.backup(self._conn)
        finally:
            source_conn.close()
        self._conn.commit()
        restored = int(self._conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0])
        if restored != expected:
            # 后置校验：抄少了也算失败，不许「部分恢复」冒充成功。
            raise BackendError(f"restore 后条数与快照不一致: {restored} != {expected}")
        return restored

    def close(self) -> None:
        self._conn.close()


class QdrantBackend:
    """Thin contract adapter around qdrant-client's local/remote client."""

    def __init__(self, client: Any, collection: str):
        self.client = client
        self.collection = collection

    def upsert(self, vector_id: str, vector: Iterable[float], payload: dict[str, Any] | None = None) -> None:
        try:
            from qdrant_client.models import PointStruct
            self.client.upsert(self.collection, [PointStruct(id=vector_id, vector=list(_clean_vector(vector)), payload=payload or {})])
        except Exception as exc:
            raise BackendError(f"qdrant upsert failed: {exc}") from exc

    def search(self, vector: Iterable[float], *, top_k: int = 5, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        try:
            query_filter = None
            if filters:
                from qdrant_client.models import FieldCondition, Filter, MatchValue
                query_filter = Filter(must=[FieldCondition(key=k, match=MatchValue(value=v)) for k, v in filters.items()])
            hits = self.client.query_points(collection_name=self.collection, query=list(_clean_vector(vector)), limit=max(1, int(top_k)), query_filter=query_filter).points
            return [{"id": str(hit.id), "score": float(hit.score), "payload": hit.payload or {}} for hit in hits]
        except Exception as exc:
            raise BackendError(f"qdrant search failed: {exc}") from exc

    def delete(self, vector_ids: Iterable[str]) -> int:
        ids = [str(x) for x in vector_ids if str(x).strip()]
        if not ids:
            return 0
        try:
            from qdrant_client.models import PointIdsList
            self.client.delete(self.collection, points_selector=PointIdsList(points=ids))
            return len(ids)
        except Exception as exc:
            raise BackendError(f"qdrant delete failed: {exc}") from exc

    def count(self, filters: dict[str, Any] | None = None) -> int:
        try:
            query_filter = None
            if filters:
                from qdrant_client.models import FieldCondition, Filter, MatchValue
                query_filter = Filter(must=[FieldCondition(key=k, match=MatchValue(value=v)) for k, v in filters.items()])
            return int(self.client.count(self.collection, count_filter=query_filter, exact=True).count)
        except Exception as exc:
            raise BackendError(f"qdrant count failed: {exc}") from exc

    def health(self) -> dict[str, Any]:
        try:
            self.client.get_collection(self.collection)
            return {"backend": "qdrant", "ok": True, "collection": self.collection, "count": self.count()}
        except Exception as exc:
            return {"backend": "qdrant", "ok": False, "collection": self.collection, "error": str(exc)[:200]}

    def snapshot(self, destination: str) -> str:
        raise BackendError("Qdrant snapshot must use the deployment's official snapshot API")

    def restore(self, source: str) -> int:
        # 与 snapshot 对称地拒绝：Qdrant 的恢复必须走它自己的 snapshot API，
        # 这里不提供一个「看着像恢复」的假实现。
        raise BackendError("Qdrant restore must use the deployment's official snapshot API")


def _live_vector_store() -> Any | None:
    """取 mem0 单例已建好的 vector_store；单例没起就返回 None。

    刻意不调用 ``get_memory()``：体检不该顺手把一个重量级单例建起来。
    """
    try:
        import sys
        from ducky.mem0_runtime import is_mem_ready
        if not is_mem_ready():
            return None
        mem = getattr(sys, "_aidumem_singleton", None)
        return getattr(mem, "vector_store", None)
    except Exception:
        return None


def _qdrant_live_health() -> dict[str, Any]:
    """对活着的 Qdrant 做一次只读探测；探不到就诚实说探不到。

    三态，各自有各自的含义，不许互相冒充：

    ==============  ==========================================================
    ``probed``      含义
    ==============  ==========================================================
    ``True``  ok    真查到了集合元数据，``points`` 是当时的向量条数
    ``True``  !ok   客户端在、查询炸了 —— 这是真故障，记降级
    ``False``       单例/客户端尚未就绪（冷启动常态）—— 不记降级，也**不报绿**
    ==============  ==========================================================
    """
    store = _live_vector_store()
    client = getattr(store, "client", None) if store is not None else None
    collection = str(getattr(store, "collection_name", "") or "") if store is not None else ""
    if client is None or not collection:
        # 冷启动常态：不是故障，但也绝不是「健康」。
        return {
            "backend": "qdrant", "ok": False, "probed": False, "degraded": [],
            "managed_by": "mem0", "detail": "mem0 向量库单例尚未就绪，未探测",
        }
    try:
        info = client.get_collection(collection)
        points = getattr(info, "points_count", None)
        return {
            "backend": "qdrant", "ok": True, "probed": True, "degraded": [],
            "managed_by": "mem0", "collection": collection,
            "points": int(points) if points is not None else None,
        }
    except Exception as exc:
        DegradationTracker.record_degradation("vector_backend", f"qdrant probe failed: {exc}")
        return {
            "backend": "qdrant", "ok": False, "probed": True,
            "degraded": ["vector_backend"], "managed_by": "mem0",
            "collection": collection, "error": str(exc)[:200],
        }


def backend_health() -> dict[str, Any]:
    """Report configured backend without opening/mutating the production store."""
    configured = os.environ.get("AIDUMEM_VECTOR_BACKEND", "qdrant").strip().lower() or "qdrant"
    if configured not in {"qdrant", "sqlite-vec", "sqlite_vec"}:
        reason = f"unsupported vector backend: {configured}"
        DegradationTracker.record_degradation("vector_backend", reason)
        return {"backend": configured, "ok": False, "degraded": ["vector_backend"], "error": reason}
    if configured in {"sqlite-vec", "sqlite_vec"}:
        # Explicit experimental mode: do not silently fall back to Qdrant.
        path = os.environ.get("AIDUMEM_SQLITE_VEC_PATH", os.path.join(DATA_DIR, "vectors.sqlite"))
        try:
            backend = SQLiteVecBackend(path, require_extension=True)
            result = backend.health()
            backend.close()
            if not result.get("ok"):
                DegradationTracker.record_degradation("vector_backend", result.get("error", "sqlite-vec unavailable"))
            return {**result, "degraded": [] if result.get("ok") else ["vector_backend"]}
        except BackendError as exc:
            DegradationTracker.record_degradation("vector_backend", str(exc))
            return {"backend": "sqlite-vec", "ok": False, "degraded": ["vector_backend"], "error": str(exc)[:200]}
    # Qdrant 由 mem0 托管。此前这里直接 return ok=True —— 无论 Qdrant 是死是活，
    # /health 都报 vector_backend_ok: true。这是标准的假绿灯：生产默认就走这条
    # 分支，等于**这项探针从来没探过**，出事时体检还在报平安。
    #
    # 改为复用 mem0 **已经建好**的那个客户端做一次只读元数据查询。
    # 绝不为了体检去 get_memory() 把单例建起来（那会读配置、清 Qdrant 锁、
    # 连 LLM）—— 单例没起时如实说「尚未探测」，不冒充健康。
    return _qdrant_live_health()


__all__ = [
    "BackendError", "BackendUnavailable", "QdrantBackend", "SQLiteVecBackend",
    "VectorBackend", "backend_health",
]
