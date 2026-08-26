"""tests/test_v20_2_autoshift.py — v20.2 智慧引擎自动挡点名验收

对应《v20.2-pre 升级预案》第四节验收门槛：
  1. 断供演练全链（掐云→写入照落→lite 召回命中→挡位如实→恢复→补账→升挡）
  2. 双索引一致性（同源 id / 域隔离 / 删除链两库同清）
  3. 负向对照区分力（降挡期间云索引零写入；假恢复不许过早升挡）
"""
from __future__ import annotations

import hashlib
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ducky.gear as gear  # noqa: E402


# ══════════════════════════════════════════════════════════════════
# 共用替身：确定性伪嵌入 + 余弦向量库（R-11 同族，双 collection）
# ══════════════════════════════════════════════════════════════════

def _bigram_vec(text: str, dims: int = 64):
    v = [0.0] * dims
    t = "".join(str(text).split())
    for i in range(max(len(t) - 1, 0)):
        h = int(hashlib.md5(t[i:i + 2].encode()).hexdigest(), 16)
        v[h % dims] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class _FakeQdrant:
    """qdrant client 替身：多 collection、must 过滤、真余弦排序。"""

    def __init__(self):
        self.cols: dict = {}

    # -- 集合管理 --
    def get_collections(self):
        class _C:  # noqa: N801
            def __init__(s, names): s.collections = [type("N", (), {"name": n})() for n in names]
        return _C(list(self.cols))

    def create_collection(self, collection_name, vectors_config=None, **kw):
        self.cols[collection_name] = {}

    # -- 点位 --
    def upsert(self, collection_name, points):
        col = self.cols.setdefault(collection_name, {})
        for p in points:
            col[str(p.id)] = (list(p.vector), dict(p.payload or {}))

    def delete(self, collection_name, points_selector):
        col = self.cols.get(collection_name, {})
        if isinstance(points_selector, list):
            for pid in points_selector:
                col.pop(str(pid), None)
        else:  # FilterSelector
            doomed = [pid for pid, (_, pl) in col.items()
                      if self._match(pl, points_selector.filter)]
            for pid in doomed:
                col.pop(pid)

    def retrieve(self, collection_name, ids, **kw):
        col = self.cols.get(collection_name, {})
        return [type("P", (), {"id": i})() for i in ids if str(i) in col]

    def count(self, collection_name, count_filter=None, exact=True):
        col = self.cols.get(collection_name, {})
        n = sum(1 for _, (v, pl) in col.items()
                if count_filter is None or self._match(pl, count_filter))
        return type("R", (), {"count": n})()

    # 生产 API 面对齐：新版 qdrant-client **没有** search，只有 query_points。
    # 替身若多实现一个生产没有的方法，就会像首版那样给假绿灯 ——
    # 这里刻意只实现 query_points。
    def query_points(self, collection_name, query, query_filter=None,
                     limit=10, with_payload=True):
        col = self.cols.get(collection_name, {})
        hits = []
        for pid, (vec, pl) in col.items():
            if query_filter is not None and not self._match(pl, query_filter):
                continue
            score = sum(a * b for a, b in zip(query, vec))
            hits.append(type("H", (), {"id": pid, "score": score, "payload": pl})())
        hits.sort(key=lambda h: h.score, reverse=True)
        return type("R", (), {"points": hits[:limit]})()

    @staticmethod
    def _match(payload, flt) -> bool:
        for cond in getattr(flt, "must", []) or []:
            if payload.get(cond.key) != cond.match.value:
                return False
        return True


class _CloudEmbedder:
    """可开关的云嵌入替身：关闸时抛（模拟嵌入服务断供）。"""

    def __init__(self):
        self.up = True
        self.calls = 0

    def embed(self, text, action=None):
        self.calls += 1
        if not self.up:
            raise RuntimeError("cloud embedding unreachable（演练断供）")
        return _bigram_vec(f"cloud::{text}")


class _FakeMemory:
    def __init__(self, client):
        self.embedding_model = _CloudEmbedder()
        self.vector_store = type("VS", (), {"client": client})()
        self.add_calls: list = []
        client.create_collection("mem0")

    def add(self, messages, user_id=None, metadata=None, **kw):
        text = messages if isinstance(messages, str) else str(messages)
        self.embedding_model.embed(text, "add")  # 云嵌入在环：断供时此处抛
        self.add_calls.append({"messages": messages, "user_id": user_id})
        return {"results": [{"id": f"m_{len(self.add_calls)}", "memory": text[:50]}]}

    def search(self, query, filters=None, limit=None, top_k=None, **kw):
        qv = self.embedding_model.embed(query, "search")
        col = self.vector_store.client.cols.get("mem0", {})
        out = []
        for pid, (vec, pl) in col.items():
            if filters and any(pl.get(k) != v for k, v in filters.items()):
                continue
            out.append({"id": pid, "memory": pl.get("data", ""),
                        "score": sum(a * b for a, b in zip(qv, vec)),
                        "created_at": pl.get("created_at"), "user_id": pl.get("user_id"),
                        "metadata": {k: v for k, v in pl.items() if k not in ("data", "user_id")}})
        out.sort(key=lambda r: r["score"], reverse=True)
        return {"results": out[: (limit or top_k or 10)]}

    def get_all(self, *a, **kw):
        return {"results": []}


@pytest.fixture()
def rig(monkeypatch, tmp_path):
    """自动挡演练台：沙箱库 + 双 collection 替身 + 本地嵌入替身 + 真路由。"""
    import ducky.dual_index as di
    import ducky.hot.search as hot_search
    import ducky.local_embed as le
    import ducky.mem0_runtime as runtime
    import ducky.utils as utils
    from ducky.schema_bootstrap import ensure_core_schema
    from ducky.text_fts import _init_text_fts

    monkeypatch.setattr(utils, "FACTS_DB", str(tmp_path / "facts.db"))
    monkeypatch.setattr(utils, "TEXT_FTS_DB", str(tmp_path / "text_fts.db"))
    gear.reset_gear_for_tests()
    monkeypatch.setenv("AIDUMEI_GEAR_COOLDOWN_SEC", "60")

    client = _FakeQdrant()
    fake = _FakeMemory(client)
    monkeypatch.setattr(runtime, "get_memory", lambda: fake)
    monkeypatch.setattr(hot_search, "get_memory", lambda: fake)
    import ducky.hot.add as hot_add
    monkeypatch.setattr(hot_add, "get_memory", lambda: fake, raising=False)
    # 本地嵌入替身：与云不同的确定性空间（512 维语义由 dual_index 层承接，
    # 这里只需「同语言可比、与云不可比」的性质成立）。
    monkeypatch.setattr(le, "local_embed_texts",
                        lambda ts: [_bigram_vec(f"local::{t}") for t in ts])
    monkeypatch.setattr(di, "local_embed_texts",
                        lambda ts: [_bigram_vec(f"local::{t}") for t in ts])
    ensure_core_schema(force=True)
    _init_text_fts()
    di.ensure_pending_schema()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ducky.hot.add import register_add_routes
    hot_search_app = FastAPI()
    register_add_routes(hot_search_app)
    hot_search.register_search_routes(hot_search_app)
    return client, fake, TestClient(hot_search_app), di


# ══════════════════════════════════════════════════════════════════
# WP-G · 熔断器三态机
# ══════════════════════════════════════════════════════════════════

class TestGear:
    def setup_method(self):
        gear.reset_gear_for_tests()

    def test_n_consecutive_failures_trip(self):
        t = 1000.0
        gear.record_cloud_failure("x", now=t)
        gear.record_cloud_failure("x", now=t + 1)
        assert gear.current_mode(now=t + 2) == "full", "两次失败不到阈值就降挡——过敏"
        gear.record_cloud_failure("x", now=t + 3)
        assert gear.current_mode(now=t + 4) == "lite"

    def test_interrupted_failures_do_not_trip(self):
        t = 1000.0
        gear.record_cloud_failure("x", now=t)
        gear.record_cloud_failure("x", now=t + 1)
        gear.record_cloud_success(now=t + 2)  # 中断连击
        gear.record_cloud_failure("x", now=t + 3)
        gear.record_cloud_failure("x", now=t + 4)
        assert gear.current_mode(now=t + 5) == "full", "非连续失败被累计——计数没清"

    def test_false_recovery_does_not_upshift(self):
        t = 1000.0
        for i in range(3):
            gear.record_cloud_failure("x", now=t + i)
        assert gear.current_mode(now=t + 70) == "lite"  # 冷却毕 → half-open 仍报 lite
        gear.record_cloud_success(now=t + 71)           # 假恢复：单次成功
        assert gear.current_mode(now=t + 72) == "lite", \
            "half-open 单次成功就升挡——防抖失守（验收门槛 3）"
        gear.record_cloud_failure("x", now=t + 73)      # 又断 → 回 open 重新冷却
        assert gear.current_mode(now=t + 74) == "lite"
        gear.record_cloud_success(now=t + 75)
        assert gear.current_mode(now=t + 76) == "lite", "open 态成功信号不该直接生效"

    def test_recovery_after_m_successes(self):
        t = 1000.0
        for i in range(3):
            gear.record_cloud_failure("x", now=t + i)
        assert gear.current_mode(now=t + 70) == "lite"
        gear.record_cloud_success(now=t + 71)
        gear.record_cloud_success(now=t + 72)
        assert gear.current_mode(now=t + 73) == "full"
        assert gear.gear_status()["shift_count"] == 2  # 一降一升

    def test_invalid_env_raises_by_name(self, monkeypatch):
        monkeypatch.setenv("AIDUMEI_GEAR_TRIP_FAILURES", "很多次")
        with pytest.raises(ValueError, match="AIDUMEI_GEAR_TRIP_FAILURES"):
            gear.trip_threshold()


# ══════════════════════════════════════════════════════════════════
# WP-E · 本地嵌入备胎（依赖缺失的诚实态）
# ══════════════════════════════════════════════════════════════════

class TestLocalEmbed:
    def test_probe_never_raises_when_dependency_missing(self, monkeypatch):
        import ducky.local_embed as le
        monkeypatch.setattr(le, "_FASTEMBED_IMPORTABLE", False)
        monkeypatch.setattr(le, "_model", None)
        assert le.is_local_embed_available() is False  # 不抛
        st = le.local_embed_status()
        assert st["dependency"] is False and st["model_loaded"] is False

    def test_embed_raises_by_name_when_unavailable(self, monkeypatch):
        import ducky.local_embed as le
        monkeypatch.setattr(le, "_FASTEMBED_IMPORTABLE", False)
        monkeypatch.setattr(le, "_model", None)
        with pytest.raises(RuntimeError, match="fastembed 未安装"):
            le.local_embed_texts(["x"])

    def test_real_model_dim_and_semantics(self):
        """真备胎在环（fastembed + 模型文件都在才跑：第十一条跳过轴）。"""
        pytest.importorskip("fastembed", reason="fastembed 未安装：备胎真模型测试跳过")
        import ducky.local_embed as le
        if not le.is_local_embed_available():
            pytest.skip("本地嵌入模型文件未部署（fetch_local_embed_model.py）")
        va, vb, vc = le.local_embed_texts(
            ["网关端口是多少", "网关服务监听的端口号", "清晨的手冲咖啡"])
        assert len(va) == le.LOCAL_EMBED_DIM

        def cos(a, b):
            return sum(x * y for x, y in zip(a, b)) / (
                math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(x * x for x in b)))
        assert cos(va, vb) > cos(va, vc), "近义 < 无关 —— 本地模型语义失灵"


# ══════════════════════════════════════════════════════════════════
# WP-F · 双索引：同源 id / 域隔离 / 删除链 / 欠账
# ══════════════════════════════════════════════════════════════════

class TestDualIndex:
    def test_verbatim_id_is_deterministic_and_idempotent(self, rig):
        client, fake, _, di = rig
        di.upsert_local_verbatim("u1", "default", "同一句原文")
        di.upsert_local_verbatim("u1", "default", "同一句原文")
        assert len(client.cols[di.LOCAL_COLLECTION]) == 1, "同文重发堆点 —— 幂等失守"

    def test_scope_isolation_in_search_and_delete(self, rig):
        client, fake, _, di = rig
        di.upsert_local_verbatim("alice6", "default", "甲租户的机密备忘")
        di.upsert_local_verbatim("bob6", "default", "乙租户的机密备忘")
        hits = di.search_local("机密备忘", "alice6")
        assert hits and all(h["user_id"] == "alice6" for h in hits), "跨租户漏进召回"
        n = di.delete_local_by_scope("alice6", "default")
        assert n == 1
        assert di.search_local("机密备忘", "alice6") == []
        assert di.search_local("机密备忘", "bob6"), "删甲连坐了乙"

    def test_pending_ledger_roundtrip_and_scope_delete(self, rig, monkeypatch):
        client, fake, _, di = rig
        # 制造本地写失败 → 欠账
        monkeypatch.setattr(di, "local_embed_texts",
                            lambda ts: (_ for _ in ()).throw(RuntimeError("模拟本地失败")))
        ok = di.upsert_local("p1", "欠账正文", {"user_id": "u_pend", "bank_id": "default"})
        assert ok is False
        assert di.pending_counts()["local"] == 1
        assert di.delete_pending_by_scope("u_pend", "default") == 1, \
            "欠账载荷含用户原文，删除链必须清"

    def test_delete_all_clears_local_leg_and_pending(self, rig):
        client, fake, _, di = rig
        from ducky.wal_engine import cascade_delete_all
        di.upsert_local_verbatim("dc_auto", "default", "将被清空的原文")
        di.enqueue_cloud_add({"messages": "欠着的蒸馏"}, "dc_auto", "default")
        out = cascade_delete_all("dc_auto")
        det = out["details"]
        assert det.get("local_vectors_deleted") == 1, det
        assert det.get("pending_embeddings_deleted") == 1, det
        assert di.search_local("将被清空的原文", "dc_auto") == []


# ══════════════════════════════════════════════════════════════════
# 断供演练（验收门槛 1 · 全链端到端）
# ══════════════════════════════════════════════════════════════════

class TestAutoshiftDrill:
    def test_full_lifecycle(self, rig, monkeypatch):
        client, fake, http, di = rig
        T = {"now": 1000.0}
        monkeypatch.setattr(gear.time, "time", lambda: T["now"])

        # ① full 挡写入正常
        r = http.post("/add", json={"messages": "演练前正常写入：网关端口是 9099",
                                    "user_id": "drill"})
        assert r.status_code == 200 and r.json().get("engine_mode") != "lite"
        cloud_points_before = len(client.cols.get("mem0", {}))

        # ② 断供：掐死云嵌入 → 连续查询失败触发降挡
        fake.embedding_model.up = False
        for _ in range(3):
            T["now"] += 1
            http.post("/search", json={"query": "网关端口", "user_id": "drill"})
        assert gear.current_mode(now=T["now"]) == "lite", "三连断供没降挡"

        # ③ lite 挡写入：确定性层照落 + 蒸馏欠账 + 挡位如实
        T["now"] += 1
        r = http.post("/add", json={
            "messages": "断供期间的重要约定：应急联系口令是青竹",
            "user_id": "drill"})
        body = r.json()
        assert body.get("engine_mode") == "lite"
        assert body.get("action") == "deferred_distillation"
        assert di.pending_counts()["cloud"] == 1, "蒸馏欠账没入账"
        assert fake.add_calls == [] or all("青竹" not in str(c) for c in fake.add_calls), \
            "lite 挡写入碰了 mem0 主体"
        import sqlite3
        import ducky.utils as utils
        conn = sqlite3.connect(utils.FACTS_DB)
        n_facts = conn.execute(
            "SELECT COUNT(*) FROM facts WHERE user_id='drill'").fetchone()[0]
        conn.close()
        assert n_facts >= 1, "lite 挡 pattern facts 没落 —— 确定性层断了"

        # ④ lite 挡语义召回：本地索引命中断供期原文，挡位与口径如实
        T["now"] += 1
        r = http.post("/search", json={"query": "应急联系口令", "user_id": "drill"})
        body = r.json()
        assert body["engine_mode"] == "lite"
        assert body.get("confidence_scale") == "local-bge-small-zh"
        texts = " ".join(x.get("memory", "") for x in body.get("results", []))
        assert "青竹" in texts, f"lite 挡语义召回没命中断供期写入：{texts[:120]}"

        # 负向（验收门槛 3）：降挡期间云索引零新增
        assert len(client.cols.get("mem0", {})) == cloud_points_before, \
            "降挡期间云索引被写入 —— 跨语言污染"

        # ⑤ 恢复 + 冷却 + 半开双成功 → 升挡
        fake.embedding_model.up = True
        T["now"] += 61  # 过冷却
        assert gear.current_mode(now=T["now"]) == "lite"  # half-open 仍报 lite
        http.post("/search", json={"query": "网关端口", "user_id": "drill"})
        T["now"] += 1
        http.post("/search", json={"query": "网关端口", "user_id": "drill"})
        assert gear.current_mode(now=T["now"]) == "full", "双成功后没升挡"

        # ⑥ 欠账重放：升挡已触发后台自动重放（设计行为——测试与后台
        # 线程会抢同一笔账，全量时序下谁先到都合法）；手动 replay 幂等
        # 兜底，只断**最终态**：账面清零 + 断供期写入进了完整蒸馏管线。
        import time as _t
        for _ in range(40):
            di.replay_pending(apply=True)
            if di.pending_counts()["cloud"] == 0:
                break
            _t.sleep(0.05)
        assert di.pending_counts()["cloud"] == 0, "欠账没清零"
        assert any("青竹" in str(c.get("messages", "")) for c in fake.add_calls), \
            "重放没把断供期写入送进完整蒸馏管线"


# ══════════════════════════════════════════════════════════════════
# 自审补项（无外审轮的质量补位）
# ══════════════════════════════════════════════════════════════════

class TestSelfAuditAdditions:
    def test_replay_race_deleted_tenant_does_not_resurrect(self, rig):
        """w 式组合拳变体（自审发现）：欠账重放拿的是快照行 ——
        delete_all 清租户后，快照行必须抢占失败并跳过，绝不补蒸馏复活。"""
        client, fake, _, di = rig
        from ducky.wal_engine import cascade_delete_all
        di.enqueue_cloud_add({"messages": "将被删除租户的欠账原文"}, "race_u", "default")
        rows_snapshot_taken = di.pending_counts()["cloud"] == 1
        assert rows_snapshot_taken
        cascade_delete_all("race_u")  # §15 清欠账
        report = di.replay_pending(apply=True)
        assert report["replayed"] == 0, "已删租户的欠账被重放 —— 复活"
        assert not any("将被删除租户" in str(c.get("messages", ""))
                       for c in fake.add_calls), "已删原文进了蒸馏管线"

    def test_full_gear_does_not_double_count_local_index(self, rig):
        """验收门槛 3 显式化：full 挡召回绝不掺本地索引的点（不双计）。"""
        client, fake, http, di = rig
        gear.reset_gear_for_tests()
        di.upsert_local_verbatim("nodup", "default", "只在本地库存在的句子甲乙丙")
        body = http.post("/search", json={
            "query": "只在本地库存在的句子甲乙丙", "user_id": "nodup"}).json()
        assert body.get("engine_mode") == "full"
        texts = [r.get("memory", "") for r in body.get("results", [])]
        assert not any("句子甲乙丙" in x for x in texts), \
            "full 挡吃到了 local-only 点 —— 双计/跨语言污染"

    def test_core_audit_covers_local_leg(self, rig, monkeypatch):
        """验收门槛 2 补齐：核心块对账必须看得见本地腿缺失。"""
        import ducky.core_memory as cm
        import ducky.utils as utils
        client, fake, _, di = rig
        monkeypatch.setattr(cm, "_initialized", False)
        cm._initialized_scopes.clear()

        class _CoreVS:
            def __init__(self, c):
                self.client = c
            def insert(self, vectors, payloads=None, ids=None):
                self.client.create_collection("mem0")
                from types import SimpleNamespace
                pts = [SimpleNamespace(id=i, vector=v, payload=p)
                       for i, v, p in zip(ids, vectors, payloads)]
                self.client.upsert("mem0", pts)
            def get(self, vid):
                return self.client.cols.get("mem0", {}).get(str(vid))
        class _CoreMem:
            embedding_model = fake.embedding_model
            vector_store = _CoreVS(client)
        import ducky.mem0_runtime as runtime
        monkeypatch.setattr(runtime, "get_memory", lambda: _CoreMem())

        cm.put_block("core_current_project", "对账演练之本地腿内容一二三",
                     user_id="audit_l")
        rep = cm.audit_core_replicas(user_id="audit_l")
        assert rep["checked"] == 1 and rep["gaps"] == [], rep  # 三腿+本地腿全齐

        # 掐掉本地副本 → 对账必须看见 local_vector=False
        pid = cm.core_vector_point_id("core_current_project", "audit_l", "default")
        client.cols[di.LOCAL_COLLECTION].pop(pid, None)
        rep = cm.audit_core_replicas(user_id="audit_l")
        assert rep["gaps"] and rep["gaps"][0].get("local_vector") is False, \
            "本地腿缺失没被对账看见 —— 第四副本静默缺腿"
