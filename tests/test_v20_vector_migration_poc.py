"""aiduMEI v20 P0-5 — 影子迁移与校验脚本（scripts/vector_shadow_poc.py）测试

盯住的安全边界（与脚本 docstring 一一对应）：
  ① 自测管线达成全平价（计数/逐点自检索/payload/top-k/过滤计数）；
  ② 迁移检查点落盘、断点续跑一条不丢；
  ③ 源库只读是构造出来的——ReadOnlyQdrant 白名单外的写动词一碰就炸；
  ④ 源目录有 .lock 拒绝开工（不许对着在跑的生产目录做影子迁移）；
  ⑤ 目标影子库已存在而无检查点时拒绝覆盖；
  ⑥ 检查点与（collection, dest）不匹配时拒绝续跑；
  ⑦ 报告如实：打分路径写明 python 全表扫描、扩展门禁未配置就说未配置；
  ⑧ 规模档如实：p50/p95 是插值、ru_maxrss 按平台换算、同进程模式自认
     峰值内存不可归因、recall 对着 numpy 标准答案而非另一个后端，
     且 ADR-001 的规模表与脚本默认档位不许走散。

跑法：cd <仓库根> && .venv/bin/pytest tests/test_v20_vector_migration_poc.py -v
全部在内存 Qdrant / 临时文件上跑，绝不碰生产库、绝不调 LLM。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import uuid

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from ducky.vector_backend import BackendError, QdrantBackend, SQLiteVecBackend  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "vector_shadow_poc", os.path.join(_REPO, "scripts", "vector_shadow_poc.py"))
poc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(poc)


def _seeded_qdrant(n: int, dim: int = 8, collection: str = "t"):
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, VectorParams

    raw = QdrantClient(":memory:")
    raw.create_collection(collection,
                          vectors_config=VectorParams(size=dim, distance=Distance.COSINE))
    writer = QdrantBackend(raw, collection)
    import random
    rng = random.Random(42)
    for i in range(n):
        writer.upsert(str(uuid.uuid5(uuid.NAMESPACE_URL, f"mig-test-{i}")),
                      [rng.gauss(0.0, 1.0) for _ in range(dim)],
                      {"bank_id": "bank_a" if i % 2 else "default", "seq": i})
    return raw, collection


# ═══════════════ ① 自测管线全平价 ═══════════════
def test_selftest_pipeline_reaches_full_parity(tmp_path):
    report = poc.selftest(n=40, dim=8, work_dir=str(tmp_path))
    assert report["migrate"]["done"] and report["migrate"]["migrated"] == 40
    v = report["verify"]
    assert v["count_match"], "迁移后两侧计数不等"
    assert v["vector_mismatches"] == 0, "有采样点在影子库里搜不回自己——向量失真"
    assert v["payload_mismatches"] == 0, "payload 迁移后不一致"
    assert v["topk_exact_rate"] == 1.0 and v["topk_jaccard_avg"] == 1.0, \
        "两后端 top-k 集合不一致——检索语义平价被打破"
    assert v["max_score_delta"] < 1e-4
    assert v["filter_count_match"], "过滤下推两侧计数不一致"


# ═══════════════ ② 检查点断点续跑 ═══════════════
def test_migration_is_checkpointed_and_resumable(tmp_path):
    raw, coll = _seeded_qdrant(25)
    ro = poc.ReadOnlyQdrant(raw)
    dest = str(tmp_path / "shadow.sqlite")
    ckpt = str(tmp_path / "ckpt.json")

    first = poc.migrate(ro, coll, dest, ckpt, batch=10, max_batches=1)
    assert first["migrated"] == 10 and first["done"] is False, \
        "max_batches=1 后应恰好迁了一批且未完成"
    saved = json.loads(open(ckpt, encoding="utf-8").read())
    assert saved["migrated"] == 10 and saved["done"] is False
    assert saved["next_offset"] is not None, "检查点必须记下续跑偏移"

    second = poc.migrate(ro, coll, dest, ckpt, batch=10)
    assert second["done"] is True and second["migrated"] == 25, \
        f"续跑后应恰好 25 条（实际 {second['migrated']}）——断点丢数或重复计数"
    shadow = SQLiteVecBackend(path=dest)
    try:
        assert shadow.count() == 25, "影子库总数不等于源库——续跑丢点"
    finally:
        shadow.close()

    third = poc.migrate(ro, coll, dest, ckpt, batch=10)
    assert third["done"] is True and third["migrated"] == 25, \
        "已完成的检查点再跑一次必须是幂等空转"


# ═══════════════ ③ 源库只读是构造出来的 ═══════════════
def test_readonly_proxy_blocks_all_write_verbs(tmp_path):
    raw, coll = _seeded_qdrant(6)
    ro = poc.ReadOnlyQdrant(raw)

    for verb in ("upsert", "delete", "create_collection", "delete_collection",
                 "set_payload", "update_collection"):
        with pytest.raises(BackendError):
            getattr(ro, verb)
    # 经 QdrantBackend 契约面写也一样被挡（包成 BackendError）
    with pytest.raises(BackendError):
        QdrantBackend(ro, coll).upsert("x", [0.1] * 8, {})
    with pytest.raises(BackendError):
        QdrantBackend(ro, coll).delete(["x"])

    # 正向对照：读动词照常可用，整条迁移+校验管线在只读代理下能走通
    dest = str(tmp_path / "shadow.sqlite")
    ckpt = str(tmp_path / "ckpt.json")
    assert poc.migrate(ro, coll, dest, ckpt, batch=4)["done"] is True
    shadow = SQLiteVecBackend(path=dest)
    try:
        rep = poc.verify(ro, coll, shadow, sample_points=6, search_samples=4)
        assert rep["count_match"] and rep["vector_mismatches"] == 0
    finally:
        shadow.close()


# ═══════════════ ④ 活源目录拒绝开工 ═══════════════
def test_refuses_live_source_with_lock_file(tmp_path):
    live_dir = tmp_path / "qdrant_prod"
    live_dir.mkdir()
    (live_dir / ".lock").write_text("held by a running service")

    with pytest.raises(BackendError, match="lock"):
        poc.refuse_live_source(str(live_dir))
    with pytest.raises(BackendError, match="lock"):
        poc.run_migration(str(live_dir), "mem0",
                          str(tmp_path / "shadow.sqlite"), str(tmp_path / "ckpt.json"))
    assert not (tmp_path / "shadow.sqlite").exists(), \
        "拒绝开工后不许留下任何影子文件（负向对照）"


# ═══════════════ ⑤ 已存在的目标库拒绝盲覆盖 ═══════════════
def test_refuses_to_overwrite_existing_dest_without_checkpoint(tmp_path):
    raw, coll = _seeded_qdrant(4)
    ro = poc.ReadOnlyQdrant(raw)
    dest = str(tmp_path / "shadow.sqlite")
    pre = SQLiteVecBackend(path=dest)
    pre.upsert("precious", [1.0] * 8, {"note": "别人家的影子库"})
    pre.close()

    with pytest.raises(BackendError, match="检查点"):
        poc.migrate(ro, coll, dest, str(tmp_path / "no_such_ckpt.json"), batch=4)
    survivor = SQLiteVecBackend(path=dest)
    try:
        assert survivor.count() == 1, "拒绝覆盖时原影子库必须毫发无损（负向对照）"
    finally:
        survivor.close()


# ═══════════════ ⑥ 检查点归属不匹配拒绝续跑 ═══════════════
def test_checkpoint_mismatch_refuses_to_resume(tmp_path):
    raw, coll = _seeded_qdrant(4)
    ro = poc.ReadOnlyQdrant(raw)
    dest = str(tmp_path / "shadow.sqlite")
    ckpt = str(tmp_path / "ckpt.json")
    with open(ckpt, "w", encoding="utf-8") as f:
        json.dump({"collection": "someone_elses", "dest": os.path.abspath(dest),
                   "next_offset": None, "migrated": 999, "done": False}, f)

    with pytest.raises(BackendError, match="不匹配"):
        poc.migrate(ro, coll, dest, ckpt, batch=4)
    assert not os.path.exists(dest), \
        "归属校验必须发生在建影子库之前（负向对照：不落文件）"


# ═══════════════ ⑦ 报告如实 ═══════════════
def test_report_admits_fullscan_and_extension_gate_honesty(tmp_path, monkeypatch):
    monkeypatch.delenv("AIDUMEM_SQLITE_VEC_EXTENSION", raising=False)
    report = poc.selftest(n=12, dim=8, work_dir=str(tmp_path))

    health = report["verify"]["dest_health"]
    assert health["scoring"] == "python-cosine-fullscan", \
        "报告必须写明检索是 Python 全表扫描——不许被读成「已加速」"
    gate = report["extension_gate"]
    assert gate["configured"] is False and gate["ok"] is False, \
        "扩展未配置时门禁必须如实报 not ok，不许假绿"
    assert "not configured" in gate["reason"]
    assert report["platform"]["qdrant_client"], "报告须带环境指纹供 ADR 引用"


# ═══════════════ ⑧ 规模档：量得出来，且量到的东西不许和 ADR 走散 ═══════════════
def test_percentile_interpolates_and_handles_single_and_edge_values():
    """p50/p95 是线性插值，不是"取第几个"。边界不许崩。"""
    vs = [1.0, 2.0, 3.0, 4.0]
    assert poc._percentile(vs, 0.0) == 1.0
    assert poc._percentile(vs, 1.0) == 4.0
    assert poc._percentile(vs, 0.5) == 2.5, "偶数个样本的中位数要插值，不是取中间那个"
    assert poc._percentile([7.0], 0.95) == 7.0, "只有一个样本时 p95 就是它自己"
    # 乱序输入也要给出同一个答案（内部排序），否则 p95 会随灌入顺序漂
    assert poc._percentile([4.0, 1.0, 3.0, 2.0], 0.5) == 2.5


def test_rss_peak_mb_converts_bytes_on_macos_and_kib_on_linux(monkeypatch):
    """``ru_maxrss`` 单位随平台变：macOS 字节、Linux KiB。

    负向对照就是这道测试的全部意义——同一个原始数字在两个平台上**必须**
    换算出相差 1024 倍的结果。不换算的话 Linux 会被报小 1024 倍，
    而"看着挺合理"的错数比报错更难发现。
    """
    import resource

    one_gib = 1024 * 1024 * 1024

    class _RU:
        ru_maxrss = one_gib

    monkeypatch.setattr(resource, "getrusage", lambda _who: _RU())

    monkeypatch.setattr(poc.sys, "platform", "darwin")
    mac = poc._rss_peak_mb()
    monkeypatch.setattr(poc.sys, "platform", "linux")
    linux = poc._rss_peak_mb()

    assert mac == 1024.0, "macOS 的 ru_maxrss 是字节，1 GiB 就该报 1024 MB"
    assert linux == 1048576.0, "Linux 的 ru_maxrss 是 KiB，同一个数字是 1 TiB 量级"
    assert linux == pytest.approx(mac * 1024), "两个平台必须差且只差 1024 倍"


def test_scale_probe_in_process_admits_rss_is_not_attributable(tmp_path):
    """同进程连跑时必须自己承认峰值内存不可归因。

    ``ru_maxrss`` 是进程累计峰值、只增不减，同进程里跑完大档再报小档的
    "峰值内存"就是拿大档的数字冒充小档。这里钉的是**报告如实**，
    不是"内存量得准"——量不准可以，谎报不行。
    """
    report = poc.scale_probe((60, 80), dim=8, queries=3, top_k=3, in_process=True)

    assert report["mode"] == "scale"
    assert report["sizes"] == [60, 80]
    assert len(report["rows"]) == 2, "少一档就等于没量到，不许静默缺行"
    for row in report["rows"]:
        assert row["rss_attributable"] is False, \
            "同进程模式必须标 False——不可归因的数字不许穿上可归因的外衣"


def test_scale_probe_subprocess_mode_marks_rss_attributable():
    """默认（子进程）模式下每档独立进程，峰值内存才真的属于这一档。"""
    report = poc.scale_probe((40,), dim=8, queries=2, top_k=3)

    assert len(report["rows"]) == 1
    row = report["rows"][0]
    assert row["rss_attributable"] is True
    assert row["size"] == 40


def test_scale_row_reports_recall_against_numpy_oracle_and_all_adr_columns():
    """recall 的标准答案是 numpy 精确余弦，不是"另一个后端"。

    两个后端互相对照只能证明"它们一致"，证明不了"它们对"。同时钉住
    ADR 那张表要引用的每一列都真的在报告里——列缺了，表就只能靠手写。
    """
    row = poc.scale_one(300, dim=16, queries=5, top_k=5, seed=3)

    for col in ("size", "dim", "queries", "top_k",
                "qdrant_ingest_s", "sqlite_ingest_s", "sqlite_disk_bytes",
                "rss_start_mb", "rss_peak_mb",
                "qdrant_p50_ms", "qdrant_p95_ms", "qdrant_recall_at_k",
                "sqlite_p50_ms", "sqlite_p95_ms", "sqlite_recall_at_k"):
        assert col in row, f"ADR 规模表引用了 {col}，报告里必须有这一列"

    assert row["qdrant_recall_at_k"] == 1.0
    assert row["sqlite_recall_at_k"] == 1.0, \
        "sqlite-vec 路径是全表扫描精确余弦，对着 numpy 标准答案就该满分"
    assert row["sqlite_disk_bytes"] > 0, "占盘含 -wal/-shm，不该是 0"


def _adr_scale_sizes() -> list[int]:
    path = os.path.join(_REPO, "docs", "ADR-001-vector-backend-contract-and-poc.md")
    with open(path, encoding="utf-8") as f:
        text = f.read()
    head = "## 规模实测"
    assert head in text, "ADR 里必须有规模实测一节"
    section = text.split(head, 1)[1].split("\n## ", 1)[0]
    assert "--scale" in section, "这一节必须给出可复现命令"
    sizes = []
    for line in section.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        first = line.strip("|").split("|")[0].strip().replace(",", "").replace("*", "")
        if first.isdigit():
            sizes.append(int(first))
    return sizes


def test_adr_scale_table_matches_script():
    """ADR 那张表的档位必须与脚本默认档位一致。

    档位是"这张表在说哪三个规模"的唯一锚点。脚本改了档、表没改，
    读者就会拿 100k 的结论去推一个从没跑过的规模——这是文档说谎，
    比数字难看严重得多。
    """
    assert _adr_scale_sizes() == list(poc.DEFAULT_SCALE_SIZES), (
        "docs/ADR-001 的规模表与 scripts/vector_shadow_poc.py 的 "
        "DEFAULT_SCALE_SIZES 走散了——改一处必须同步改另一处"
    )
