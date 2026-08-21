#!/usr/bin/env python3
"""aiduMEI v20 P0-5 — 向量后端影子迁移与校验脚本（只读源库，绝不改生产数据）。

这是「换库先影子」的落地工具，配套 ducky/vector_backend.py 的后端契约：

  影子迁移   把一份 Qdrant 本地库（**必须是拷贝出来的快照目录，不是在跑的
             生产目录**）逐批导出到 SQLiteVecBackend 影子库。分批推进、
             检查点落盘、可断点续跑；一条不丢、一条不改。
  平价校验   迁完之后拿公开契约接口对账：总数对总数、逐点自检索
             （每个采样点的向量在影子库里搜自己，必须以 ≈1.0 的分数命中
             自己）、采样查询 top-k 集合对照（Jaccard / 完全一致率 /
             分数偏差），再各测一轮检索耗时。
  自测模式   --selftest 用内存 Qdrant + 临时 SQLite 把整条管线真跑一遍，
             两个后端都是真实现，不是 mock——这就是 ADR 里性能与能力
             实测数字的来源。
  规模档     --scale 按 1k/10k/100k 三档量两个后端的 p50/p95、影子库占盘、
             峰值内存与 recall@k。recall 的标准答案是 numpy 精确余弦，
             不是"另一个后端"——两个后端互相对照只能证明它们一致，
             证明不了它们对。每档跑在**独立子进程**里，好让峰值内存
             真的归属于那一档（ru_maxrss 只增不减）。

安全边界（每一条都有测试盯着，见 tests/test_v20_vector_migration_poc.py）：

  1. 源库只读是**构造出来的**，不是靠自觉：源 client 被 ReadOnlyQdrant
     代理包住，白名单之外的方法（upsert/delete/create_collection/...）
     一碰就炸，迁移和校验全程只能读。
  2. 源目录里有 .lock 就拒绝开工——那说明可能有活服务正抱着这个目录，
     影子迁移只许对着拷贝出来的快照跑。
  3. 目标影子库已存在而没有对应检查点时拒绝覆盖；检查点与
     （collection, dest）不匹配时拒绝续跑——不给「悄悄写错地方」留门。
  4. 回退 = 删掉影子文件。默认后端始终是 Qdrant，本脚本不改任何配置、
     不动任何环境变量，跑失败对生产数据零影响。

跑法：
  .venv/bin/python scripts/vector_shadow_poc.py --selftest --report /tmp/shadow_report.json
  .venv/bin/python scripts/vector_shadow_poc.py --scale --report /tmp/scale_report.json
  .venv/bin/python scripts/vector_shadow_poc.py \
      --source /path/to/COPY_of_qdrant_dir --collection mem0 \
      --dest /tmp/shadow_vectors.sqlite --checkpoint /tmp/shadow_ckpt.json
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import random
import sqlite3
import sys
import tempfile
import time
import uuid
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ducky.vector_backend import (  # noqa: E402
    BackendError,
    BackendUnavailable,
    QdrantBackend,
    SQLiteVecBackend,
)

# 源端只读白名单：读点、数点、读集合元信息、查询。没有任何写动词。
_READONLY_ALLOWED = frozenset({
    "scroll", "count", "retrieve", "query_points",
    "get_collection", "get_collections", "collection_exists",
})


class ReadOnlyQdrant:
    """只读代理：白名单之外的属性访问直接抛错，写不进去是物理性的。"""

    def __init__(self, client: Any):
        self._client = client

    def __getattr__(self, name: str) -> Any:
        if name not in _READONLY_ALLOWED:
            raise BackendError(
                f"只读源库：方法 '{name}' 不在白名单内，影子迁移全程禁止写源库"
            )
        return getattr(self._client, name)


def refuse_live_source(source_path: str) -> None:
    """源目录里有 .lock 就拒绝：可能有活服务抱着它，只许对快照拷贝跑。"""
    lock = os.path.join(source_path, ".lock")
    if os.path.exists(lock):
        raise BackendError(
            f"源目录存在 .lock（{lock}）——疑似有在跑的服务持有该库。"
            "影子迁移只允许对**拷贝出来的快照目录**执行，绝不对生产目录动手。"
        )


def _load_checkpoint(checkpoint_path: str, collection: str, dest: str) -> dict:
    """读检查点并校验归属；不存在返回全新起点。"""
    if not os.path.exists(checkpoint_path):
        return {"collection": collection, "dest": os.path.abspath(dest),
                "next_offset": None, "migrated": 0, "done": False}
    with open(checkpoint_path, "r", encoding="utf-8") as f:
        ckpt = json.load(f)
    if ckpt.get("collection") != collection or ckpt.get("dest") != os.path.abspath(dest):
        raise BackendError(
            "检查点与本次任务不匹配（collection/dest 不一致）——"
            "拒绝续跑，防止把点写进别人的影子库"
        )
    return ckpt


def _save_checkpoint(checkpoint_path: str, ckpt: dict) -> None:
    tmp = checkpoint_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(ckpt, f, ensure_ascii=False, indent=2)
    os.replace(tmp, checkpoint_path)


def _point_vector(point: Any) -> list[float]:
    vec = getattr(point, "vector", None)
    if isinstance(vec, dict):
        raise BackendError(
            "遇到命名向量（named vectors）——本 POC 只支持单一无名向量，"
            "请先确认源集合结构再扩展脚本"
        )
    if not isinstance(vec, (list, tuple)):
        raise BackendError(f"点 {point.id} 缺向量（scroll 需要 with_vectors=True）")
    return list(vec)


def migrate(source_client: Any, collection: str, dest: str,
            checkpoint_path: str, *, batch: int = 256,
            max_batches: int | None = None) -> dict:
    """检查点续跑的逐批影子迁移。source_client 应当已被 ReadOnlyQdrant 包住。

    返回 {"migrated": 总数, "done": 是否扫完, "seconds": 耗时}。
    """
    ckpt = _load_checkpoint(checkpoint_path, collection, dest)
    if os.path.exists(dest) and not os.path.exists(checkpoint_path):
        raise BackendError(
            f"目标影子库已存在（{dest}）但没有检查点——"
            "拒绝覆盖。要重跑请先删掉旧影子文件，要续跑请带上原检查点"
        )
    if ckpt.get("done"):
        return {"migrated": int(ckpt["migrated"]), "done": True, "seconds": 0.0}

    shadow = SQLiteVecBackend(path=dest)
    t0 = time.perf_counter()
    batches = 0
    try:
        offset = ckpt.get("next_offset")
        while True:
            points, next_offset = source_client.scroll(
                collection_name=collection, limit=max(1, int(batch)),
                offset=offset, with_payload=True, with_vectors=True,
            )
            for p in points:
                shadow.upsert(str(p.id), _point_vector(p), dict(p.payload or {}))
            ckpt["migrated"] = int(ckpt["migrated"]) + len(points)
            ckpt["next_offset"] = (
                str(next_offset) if isinstance(next_offset, uuid.UUID) else next_offset
            )
            ckpt["done"] = next_offset is None
            _save_checkpoint(checkpoint_path, ckpt)
            batches += 1
            offset = next_offset
            if ckpt["done"]:
                break
            if max_batches is not None and batches >= max_batches:
                break
    finally:
        shadow.close()
    return {"migrated": int(ckpt["migrated"]), "done": bool(ckpt["done"]),
            "seconds": round(time.perf_counter() - t0, 3)}


def verify(source_client: Any, collection: str, dest_backend: SQLiteVecBackend, *,
           sample_points: int = 64, search_samples: int = 16, top_k: int = 5,
           filter_probe: dict | None = None, seed: int = 7) -> dict:
    """只用公开契约接口对账，一次写操作都没有。"""
    source = QdrantBackend(source_client, collection)
    rng = random.Random(seed)

    report: dict[str, Any] = {
        "count_source": source.count(),
        "count_dest": dest_backend.count(),
    }
    report["count_match"] = report["count_source"] == report["count_dest"]

    # 逐点自检索：采样点的向量在影子库里搜自己，必须以 ≈1.0 命中自己，
    # 且命中点的 payload 与源库逐字节一致——同时验证了存在性与向量保真。
    points, _ = source_client.scroll(
        collection_name=collection, limit=max(1, sample_points),
        offset=None, with_payload=True, with_vectors=True,
    )
    vec_mismatch = payload_mismatch = 0
    for p in points:
        hits = dest_backend.search(_point_vector(p), top_k=1)
        top = hits[0] if hits else None
        if not top or top["id"] != str(p.id) or top["score"] < 0.9999:
            vec_mismatch += 1
        elif top["payload"] != dict(p.payload or {}):
            payload_mismatch += 1
    report["self_hit_checked"] = len(points)
    report["vector_mismatches"] = vec_mismatch
    report["payload_mismatches"] = payload_mismatch

    # 采样查询平价：同一查询向量在两个后端各跑 top-k，比集合。
    sample = rng.sample(points, k=min(search_samples, len(points))) if points else []
    exact = 0
    jaccards: list[float] = []
    max_score_delta = 0.0
    t_src = t_dst = 0.0
    for p in sample:
        v = _point_vector(p)
        t = time.perf_counter(); src_hits = source.search(v, top_k=top_k); t_src += time.perf_counter() - t
        t = time.perf_counter(); dst_hits = dest_backend.search(v, top_k=top_k); t_dst += time.perf_counter() - t
        src_ids = {h["id"] for h in src_hits}
        dst_ids = {h["id"] for h in dst_hits}
        if src_ids == dst_ids:
            exact += 1
        union = src_ids | dst_ids
        jaccards.append(len(src_ids & dst_ids) / len(union) if union else 1.0)
        src_scores = {h["id"]: h["score"] for h in src_hits}
        for h in dst_hits:
            if h["id"] in src_scores:
                max_score_delta = max(max_score_delta, abs(h["score"] - src_scores[h["id"]]))
    n = len(sample)
    report["search_samples"] = n
    report["topk_exact_rate"] = round(exact / n, 4) if n else None
    report["topk_jaccard_avg"] = round(sum(jaccards) / n, 4) if n else None
    report["max_score_delta"] = round(max_score_delta, 6)
    report["qdrant_search_avg_ms"] = round(t_src / n * 1000, 3) if n else None
    report["sqlite_search_avg_ms"] = round(t_dst / n * 1000, 3) if n else None

    # 过滤下推平价（可选）：同一 payload 过滤在两侧计数必须一致。
    if filter_probe:
        report["filter_probe"] = dict(filter_probe)
        report["filter_count_source"] = source.count(filters=filter_probe)
        report["filter_count_dest"] = dest_backend.count(filters=filter_probe)
        report["filter_count_match"] = (
            report["filter_count_source"] == report["filter_count_dest"]
        )

    report["dest_health"] = dest_backend.health()
    return report


def extension_gate_report() -> dict:
    """sqlite-vec 扩展门禁实测：装得上就说装得上，装不上就说为什么。

    利用 require_extension=True 的「体检无副作用」性质——失败不落任何文件。
    """
    configured = bool(os.environ.get("AIDUMEM_SQLITE_VEC_EXTENSION", "").strip())
    probe_path = os.path.join(tempfile.mkdtemp(prefix="aidumem_ext_gate_"), "probe.sqlite")
    try:
        be = SQLiteVecBackend(path=probe_path, require_extension=True)
        be.close()
        return {"configured": configured, "ok": True}
    except BackendUnavailable as exc:
        return {"configured": configured, "ok": False, "reason": str(exc)[:200]}
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(probe_path + suffix)
            except OSError:
                pass


def _platform_report() -> dict:
    from importlib.metadata import version as _pkg_version
    try:
        qc_ver = _pkg_version("qdrant-client")
    except Exception:
        qc_ver = "unknown"
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "sqlite": sqlite3.sqlite_version,
        "qdrant_client": qc_ver,
    }


def selftest(n: int = 200, dim: int = 32, seed: int = 7,
             work_dir: str | None = None) -> dict:
    """内存 Qdrant + 临时 SQLite 真跑整条「迁移→校验」管线。"""
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams

    rng = random.Random(seed)
    work = work_dir or tempfile.mkdtemp(prefix="aidumem_shadow_selftest_")
    dest = os.path.join(work, "shadow_vectors.sqlite")
    ckpt = os.path.join(work, "shadow_ckpt.json")
    collection = "selftest"

    raw = QdrantClient(":memory:")
    raw.create_collection(collection, vectors_config=VectorParams(size=dim, distance=Distance.COSINE))
    writer = QdrantBackend(raw, collection)
    banks = ("default", "bank_a", "bank_b")
    t0 = time.perf_counter()
    for i in range(n):
        vec = [rng.gauss(0.0, 1.0) for _ in range(dim)]
        writer.upsert(
            str(uuid.uuid5(uuid.NAMESPACE_URL, f"aidumem-selftest-{i}")),
            vec,
            {"user_id": "selftest", "bank_id": banks[i % 3], "seq": i},
        )
    seed_seconds = round(time.perf_counter() - t0, 3)

    ro = ReadOnlyQdrant(raw)  # 种完子立刻锁只读：迁移与校验全程写不了源库
    mig = migrate(ro, collection, dest, ckpt, batch=64)
    shadow = SQLiteVecBackend(path=dest)
    try:
        rep = verify(ro, collection, shadow, sample_points=min(n, 64),
                     search_samples=16, top_k=5,
                     filter_probe={"bank_id": "bank_a"}, seed=seed)
    finally:
        shadow.close()

    return {
        "mode": "selftest", "n": n, "dim": dim,
        "seed_seconds": seed_seconds,
        "migrate": mig,
        "verify": rep,
        "extension_gate": extension_gate_report(),
        "platform": _platform_report(),
    }


# ── 规模档实测（ADR-001 的 1k/10k/100k 表就是这里跑出来的）────────────

#: 三档规模。改这里就必须同步改 ADR-001 的表——有测试盯着，见
#: tests/test_v20_vector_migration_poc.py::test_adr_scale_table_matches_script.
DEFAULT_SCALE_SIZES = (1_000, 10_000, 100_000)


def _percentile(values: list[float], q: float) -> float:
    """线性插值分位数。样本少的时候不假装精确，但也不四舍五入成谎话。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


def _rss_peak_mb() -> float:
    """本进程峰值 RSS（MB）。

    ``ru_maxrss`` 的单位随平台变：**macOS 是字节，Linux 是 KiB**。不换算就
    会把 Linux 的数字报小 1024 倍——这种"看着挺合理"的错数比报错更难发现。
    """
    import resource
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(raw / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)


def _sqlite_disk_bytes(path: str) -> int:
    """影子库真实占盘：主文件 + WAL + SHM，一个都不许漏算。"""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(path + suffix)
        except OSError:
            pass
    return total


def scale_one(size: int, *, dim: int = 64, queries: int = 20, top_k: int = 10,
              seed: int = 7, work_dir: str | None = None) -> dict:
    """量一个规模档：两后端的 p50/p95、占盘、峰值内存、recall@k。

    recall@k 的**标准答案不是另一个后端**，而是 numpy 算的精确余弦 top-k
    ——两个后端互相对照只能证明"它们一致"，证明不了"它们对"。
    """
    import numpy as np
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams

    rss_start = _rss_peak_mb()
    rng = np.random.default_rng(seed * 1_000_003 + size)
    matrix = rng.standard_normal((size, dim), dtype=np.float64)
    probes = rng.standard_normal((queries, dim), dtype=np.float64)
    ids = [str(uuid.uuid5(uuid.NAMESPACE_URL, f"aidumem-scale-{size}-{i}"))
           for i in range(size)]

    work = work_dir or tempfile.mkdtemp(prefix=f"aidumem_scale_{size}_")
    dest = os.path.join(work, "shadow_vectors.sqlite")
    collection = f"scale{size}"

    raw = QdrantClient(":memory:")
    raw.create_collection(
        collection, vectors_config=VectorParams(size=dim, distance=Distance.COSINE))
    qdrant = QdrantBackend(raw, collection)
    sqlite = SQLiteVecBackend(path=dest)
    try:
        # 灌注：两边都走契约面 upsert，不抄近道，测的就是契约面的成本。
        rows = matrix.tolist()
        t0 = time.perf_counter()
        for pid, vec in zip(ids, rows):
            qdrant.upsert(pid, vec, {"user_id": "scale", "seq": 0})
        qdrant_ingest = round(time.perf_counter() - t0, 2)
        t0 = time.perf_counter()
        for pid, vec in zip(ids, rows):
            sqlite.upsert(pid, vec, {"user_id": "scale", "seq": 0})
        sqlite_ingest = round(time.perf_counter() - t0, 2)

        # 标准答案：精确余弦全量排序（numpy，独立于两个被测后端）。
        norms = matrix / np.linalg.norm(matrix, axis=1, keepdims=True)
        truth: list[set[str]] = []
        for probe in probes:
            sims = norms @ (probe / np.linalg.norm(probe))
            best = np.argpartition(-sims, top_k)[:top_k]
            truth.append({ids[i] for i in best})

        lat: dict[str, list[float]] = {"qdrant": [], "sqlite": []}
        hit: dict[str, int] = {"qdrant": 0, "sqlite": 0}
        for probe, expected in zip(probes.tolist(), truth):
            for name, backend in (("qdrant", qdrant), ("sqlite", sqlite)):
                t0 = time.perf_counter()
                hits = backend.search(probe, top_k=top_k)
                lat[name].append((time.perf_counter() - t0) * 1000)
                hit[name] += len({h["id"] for h in hits} & expected)

        out = {
            "size": size, "dim": dim, "queries": queries, "top_k": top_k,
            "qdrant_ingest_s": qdrant_ingest,
            "sqlite_ingest_s": sqlite_ingest,
            "sqlite_disk_bytes": _sqlite_disk_bytes(dest),
            "rss_start_mb": rss_start,
            "rss_peak_mb": _rss_peak_mb(),
        }
        for name in ("qdrant", "sqlite"):
            out[f"{name}_p50_ms"] = round(_percentile(lat[name], 0.50), 2)
            out[f"{name}_p95_ms"] = round(_percentile(lat[name], 0.95), 2)
            out[f"{name}_recall_at_k"] = round(hit[name] / (queries * top_k), 4)
        return out
    finally:
        sqlite.close()
        try:
            raw.close()
        except Exception:
            pass


def scale_probe(sizes: tuple[int, ...] = DEFAULT_SCALE_SIZES, *, dim: int = 64,
                queries: int = 20, top_k: int = 10, seed: int = 7,
                in_process: bool = False) -> dict:
    """跑完整规模表。

    默认**每档一个子进程**：``ru_maxrss`` 是进程累计峰值、只增不减，同进程
    里跑完 100k 再报 1k 的"峰值内存"就是拿大档的数字冒充小档。子进程让
    每一行的内存数真的归属于那一行。``in_process=True`` 只给测试用（省去
    进程开销），此时内存列会被标记为不可归因。
    """
    rows = []
    for size in sizes:
        if in_process:
            row = scale_one(size, dim=dim, queries=queries, top_k=top_k, seed=seed)
            row["rss_attributable"] = False
        else:
            import subprocess
            proc = subprocess.run(
                [sys.executable, os.path.abspath(__file__), "--scale-one", str(size),
                 "--dim", str(dim), "--queries", str(queries),
                 "--top-k", str(top_k), "--seed", str(seed)],
                capture_output=True, text=True, check=False,
            )
            if proc.returncode != 0:
                raise BackendError(
                    f"规模档 {size} 子进程失败（exit={proc.returncode}）: "
                    f"{proc.stderr.strip()[-300:]}"
                )
            row = json.loads(proc.stdout)
            row["rss_attributable"] = True
        rows.append(row)
    return {
        "mode": "scale",
        "sizes": list(sizes),
        "rows": rows,
        "extension_gate": extension_gate_report(),
        "platform": _platform_report(),
    }


def run_migration(source_path: str, collection: str, dest: str,
                  checkpoint_path: str, *, batch: int = 256,
                  max_batches: int | None = None) -> dict:
    """真实（快照拷贝）源库的迁移+校验入口。"""
    refuse_live_source(source_path)
    from qdrant_client import QdrantClient

    raw = QdrantClient(path=source_path)
    try:
        ro = ReadOnlyQdrant(raw)
        mig = migrate(ro, collection, dest, checkpoint_path,
                      batch=batch, max_batches=max_batches)
        out: dict[str, Any] = {"mode": "migrate", "source": source_path,
                               "collection": collection, "dest": dest,
                               "migrate": mig}
        if mig["done"]:
            shadow = SQLiteVecBackend(path=dest)
            try:
                out["verify"] = verify(ro, collection, shadow)
            finally:
                shadow.close()
        out["extension_gate"] = extension_gate_report()
        out["platform"] = _platform_report()
        return out
    finally:
        try:
            raw.close()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selftest", action="store_true", help="内存双后端真跑整条管线")
    ap.add_argument("--scale", action="store_true",
                    help="跑规模档实测（1k/10k/100k × p50/p95/占盘/内存/recall@k）")
    ap.add_argument("--scale-one", type=int, default=None,
                    help="内部用：只跑一个规模档并打印一行 JSON（--scale 的子进程入口，"
                         "为的是让峰值内存能归因到单一档位）")
    ap.add_argument("--sizes", default=None,
                    help="逗号分隔的规模档，默认 "
                         + ",".join(str(s) for s in DEFAULT_SCALE_SIZES))
    ap.add_argument("--queries", type=int, default=20, help="每档查询次数")
    ap.add_argument("--top-k", type=int, default=10, help="规模档 top-k")
    ap.add_argument("--seed", type=int, default=7, help="随机种子（可复现）")
    ap.add_argument("--n", type=int, default=200, help="selftest 点数")
    ap.add_argument("--dim", type=int, default=None,
                    help="向量维度（selftest 默认 32，规模档默认 64）")
    ap.add_argument("--source", help="Qdrant 本地库目录（必须是快照拷贝，非生产目录）")
    ap.add_argument("--collection", help="源集合名")
    ap.add_argument("--dest", help="影子 SQLite 文件路径")
    ap.add_argument("--checkpoint", help="检查点 JSON 路径")
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--max-batches", type=int, default=None,
                    help="本次最多跑几批（分次迁移用，下次带同一检查点续跑）")
    ap.add_argument("--report", help="把报告 JSON 写到该路径（默认只打印）")
    args = ap.parse_args(argv)

    if args.scale_one is not None:
        # 子进程入口：只打印这一档的 JSON，父进程负责拼表。
        print(json.dumps(scale_one(
            args.scale_one, dim=args.dim or 64, queries=args.queries,
            top_k=args.top_k, seed=args.seed), ensure_ascii=False))
        return 0

    if args.scale:
        sizes = (tuple(int(s) for s in args.sizes.split(",") if s.strip())
                 if args.sizes else DEFAULT_SCALE_SIZES)
        report = scale_probe(sizes, dim=args.dim or 64, queries=args.queries,
                             top_k=args.top_k, seed=args.seed)
    elif args.selftest:
        report = selftest(n=args.n, dim=args.dim or 32)
    else:
        missing = [k for k in ("source", "collection", "dest", "checkpoint")
                   if not getattr(args, k)]
        if missing:
            ap.error(f"迁移模式缺参数: {', '.join('--' + m for m in missing)}（或改用 --selftest）")
        report = run_migration(args.source, args.collection, args.dest,
                               args.checkpoint, batch=args.batch,
                               max_batches=args.max_batches)

    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    if report.get("mode") == "scale":
        # 规模档是**测量**，不是判定：数字难看也算跑成功，成功的定义是
        # 每一档都真的量到了。少一档就非零退出，别让缺失被读成"没问题"。
        return 0 if len(report.get("rows") or []) == len(report["sizes"]) else 1
    v = report.get("verify") or {}
    ok = (report.get("migrate", {}).get("done", False)
          and v.get("count_match", False)
          and v.get("vector_mismatches", 1) == 0
          and v.get("payload_mismatches", 1) == 0)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
