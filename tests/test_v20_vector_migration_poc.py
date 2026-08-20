"""aiduMEI v20 P0-5 — 影子迁移与校验脚本（scripts/vector_shadow_poc.py）测试

盯住的安全边界（与脚本 docstring 一一对应）：
  ① 自测管线达成全平价（计数/逐点自检索/payload/top-k/过滤计数）；
  ② 迁移检查点落盘、断点续跑一条不丢；
  ③ 源库只读是构造出来的——ReadOnlyQdrant 白名单外的写动词一碰就炸；
  ④ 源目录有 .lock 拒绝开工（不许对着在跑的生产目录做影子迁移）；
  ⑤ 目标影子库已存在而无检查点时拒绝覆盖；
  ⑥ 检查点与（collection, dest）不匹配时拒绝续跑；
  ⑦ 报告如实：打分路径写明 python 全表扫描、扩展门禁未配置就说未配置。

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
