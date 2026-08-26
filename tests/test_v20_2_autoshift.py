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
    yield client, fake, TestClient(hot_search_app), di
    # 收尾：重放守护线程不许活过本测试的猴补丁世界（活过去=在别人的
    # caplog 窗口里打日志、摸别人的库——全轴序闪烁红灯的根）。
    di.join_replay_for_tests()


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

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        """v20.2.1（外审 R1）拆配置雷：非法 env **回退默认 + 探针点名**，
        绝不抛 —— 旧语义 raise 站在 should_try_cloud 主路径上，一个
        配置笔误就把「断供保命」反转成「全站搜索 500」。"""
        monkeypatch.setenv("AIDUMEI_GEAR_TRIP_FAILURES", "很多次")
        assert gear.trip_threshold() == 3
        errs = gear.gear_status()["config_errors"]
        assert errs and "AIDUMEI_GEAR_TRIP_FAILURES" in errs

    def test_bad_env_system_still_shifts(self, monkeypatch):
        """R1 负向对照：env 全填坏值，熔断照常降挡→冷却→半开→升挡 ——
        保命路径在坏配置下完整活着（这正是旧 raise 语义做不到的）。"""
        monkeypatch.setenv("AIDUMEI_GEAR_TRIP_FAILURES", "-5")
        monkeypatch.setenv("AIDUMEI_GEAR_COOLDOWN_SEC", "60s")
        t = 1000.0
        for i in range(3):
            gear.record_cloud_failure("x", now=t + i)
        assert gear.current_mode(now=t + 3) == "lite", "默认 N=3 未生效"
        assert gear.should_try_cloud(now=t + 64) is True, "默认 T=60 未生效"
        gear.record_cloud_success(now=t + 65)
        gear.record_cloud_success(now=t + 66)
        assert gear.current_mode(now=t + 67) == "full", "默认 M=2 未生效"
        errs = gear.gear_status(now=t + 68)["config_errors"] or {}
        assert set(errs) == {"AIDUMEI_GEAR_TRIP_FAILURES",
                             "AIDUMEI_GEAR_COOLDOWN_SEC"}

    def test_valid_env_clears_config_error(self, monkeypatch):
        """回退≠忽略配置：合法值照常生效，且错误记录被清掉。"""
        monkeypatch.setenv("AIDUMEI_GEAR_TRIP_FAILURES", "废话")
        assert gear.trip_threshold() == 3
        monkeypatch.setenv("AIDUMEI_GEAR_TRIP_FAILURES", "5")
        assert gear.trip_threshold() == 5
        assert not (gear.gear_status()["config_errors"] or {})


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


# ══════════════════════════════════════════════════════════════════
# v20.2.1 · 外审整改验收（生产侧复审 R2/R3/R4 + 外部审计残窗建议 + Y2）
# R1 的验收在上方 TestGear（回退语义三连测）。
# ══════════════════════════════════════════════════════════════════

def _sandbox_wal(monkeypatch, tmp_path):
    import ducky.wal_engine as we
    monkeypatch.setattr(
        we.WALEngine, "get_instance",
        classmethod(lambda cls: we.WALEngine(wal_dir=str(tmp_path / "wal"))))
    return we


class TestReplayTriggersOnStartup:
    """R2：重放此前只挂在升挡事件上，重启（挡位回 closed）= 永久赖账。"""

    def test_reconcile_startup_drains_debt(self, rig, monkeypatch, tmp_path):
        client, fake, http, di = rig
        we = _sandbox_wal(monkeypatch, tmp_path)
        di.enqueue_cloud_add({"messages": "断供期间说的话：暗号是青蛙",
                              "metadata": {}}, "u_r2", "default")
        assert di.pending_counts()["cloud"] == 1
        gear.reset_gear_for_tests()  # 模拟重启：挡位回 closed，升挡事件不会再来

        report = we.reconcile_startup()
        assert report["pending_replay_spawned"] is True

        import time as _time
        deadline = _time.time() + 8
        while _time.time() < deadline:
            if di.pending_counts()["cloud"] == 0 and di.last_replay_status():
                break
            _time.sleep(0.05)
        assert di.pending_counts()["cloud"] == 0, "启动对账没把欠账还上"
        assert any("青蛙" in str(c["messages"]) for c in fake.add_calls), \
            "欠账被清了但没重放进 mem0——是删账不是还账"
        lr = di.last_replay_status()
        assert lr and lr["source"] == "reconcile_startup"
        assert lr["report"]["replayed"] == 1

    def test_spawn_skips_when_no_debt(self, rig):
        _, _, _, di = rig
        assert di.unreplayed_count() == 0
        assert di.spawn_replay_daemon(source="test") is False, \
            "零欠账也起线程——启动面白烧一根线程"


class TestVerbatimLocalSingleDelete:
    """R3：verbatim 本地点 id 由 (原文,域) 派生、不与 memory_id 同源 ——
    单删必须重演派生才够得着，否则降挡窗口里已删内容从备胎复活。"""

    def test_pid_derivation_pure_and_scoped(self, rig):
        _, _, _, di = rig
        a = di.verbatim_local_pid("u1", "default", "同一句话")
        assert a == di.verbatim_local_pid("u1", "default", "  同一句话  "), \
            "去空白后同文不同 id——写删两侧会错位"
        assert a != di.verbatim_local_pid("u2", "default", "同一句话")
        assert a != di.verbatim_local_pid("u1", "work", "同一句话")

    def test_single_delete_reaches_verbatim_local_point(self, rig, monkeypatch, tmp_path):
        client, fake, http, di = rig
        we = _sandbox_wal(monkeypatch, tmp_path)
        text = "青蛙水彩画的开价是三百元"
        other = "无关内容：明天例会改到十点"
        assert di.upsert_local_verbatim("u_r3", "default", text)
        assert di.upsert_local_verbatim("u_r3", "default", other)
        pid = di.verbatim_local_pid("u_r3", "default", text)
        pid_other = di.verbatim_local_pid("u_r3", "default", other)
        assert pid in client.cols[di.LOCAL_COLLECTION]

        # 正文可反查（§0a 的定位现场）：FTS 里挂上该记忆的原文
        from ducky.text_fts import _index_memory
        _index_memory("mem_r3", text, user_id="u_r3", bank_id="default")

        out = we.cascade_delete_memory("mem_r3", user_id="u_r3", bank_id="default")
        det = out["details"]
        assert det.get("verbatim_local_vector_deleted") is True, det
        assert pid not in client.cols[di.LOCAL_COLLECTION], \
            "单删后 verbatim 本地点仍在——降挡时已删内容会从备胎复活（R3 未闭合）"
        # 区分力对照：同租户另一句原文纹丝不动（精确删，不是按域核平）
        assert pid_other in client.cols[di.LOCAL_COLLECTION]

        # 降挡实感对照：强制走本地腿，被删内容搜不回来
        fake.embedding_model.up = False
        for _ in range(gear.trip_threshold()):
            gear.record_cloud_failure("演练断供")
        hits = di.search_local("青蛙水彩画 开价", "u_r3", bank_id="default")
        assert all("三百元" not in h["memory"] for h in hits)


class TestReplayNoSelfReplication:
    """R4：重放失败不许再入新账 —— 否则模型持续故障下欠账表每轮 +1
    自我复制（写放大 + 告警淹没）。"""

    def test_debt_stable_under_persistent_failure_then_drains(self, rig, monkeypatch):
        client, fake, http, di = rig
        di._enqueue_pending("local", "pt_r4", "补写正文",
                            {"user_id": "u_r4", "bank_id": "default"})
        assert di.unreplayed_count() == 1

        def _broken(_ts):
            raise RuntimeError("模型文件损坏（演练持续故障）")
        monkeypatch.setattr(di, "local_embed_texts", _broken)
        for rnd in range(3):
            rep = di.replay_pending(apply=True)
            assert rep["failed"] == 1, rep
            assert di.unreplayed_count() == 1, \
                f"第 {rnd + 1} 轮重放后欠账自我复制（预期恒为 1）"

        # 区分力对照：模型恢复后同一行还得上——失败轮没有弄丢/弄脏原账
        monkeypatch.setattr(di, "local_embed_texts",
                            lambda ts: [_bigram_vec(f"local::{t}") for t in ts])
        rep = di.replay_pending(apply=True)
        assert rep["replayed"] == 1
        assert di.unreplayed_count() == 0
        assert "pt_r4" in client.cols[di.LOCAL_COLLECTION]


class TestResidualWindowRevoke:
    """残窗闭合（外部审计建议）：claiming 抢占后、mem.add 完成前，同租户 delete_all
    清了账本行 —— add 刚写回云侧的点必须当场撤销（删除意愿 > 补算完整性）。"""

    def test_cloud_replay_revokes_when_row_deleted_mid_add(self, rig, monkeypatch):
        client, fake, http, di = rig
        di.enqueue_cloud_add({"messages": "窗口内该被删掉的话",
                              "metadata": {}}, "u_win", "default")
        deleted_ids: list = []
        monkeypatch.setattr(fake, "delete",
                            lambda mid: deleted_ids.append(str(mid)),
                            raising=False)
        orig_add = fake.add

        def add_and_race(messages, user_id=None, metadata=None, **kw):
            out = orig_add(messages, user_id=user_id, metadata=metadata, **kw)
            # add 尚未返回时并发 delete_all 清账（残窗的精确时序）
            di.delete_pending_by_scope("u_win", "default")
            return out
        monkeypatch.setattr(fake, "add", add_and_race)

        rep = di.replay_pending(apply=True)
        assert rep["revoked_after_scope_delete"] == 1, rep
        assert rep["replayed"] == 0, "撤销的重放不许再计入 replayed"
        assert len(deleted_ids) == 1, "刚重放的点没有被撤销——已删内容复活"
        assert di.pending_counts()["cloud"] == 0

    def test_no_race_no_revoke(self, rig, monkeypatch):
        """区分力对照：没有交叉删除时一个点都不许撤销。"""
        client, fake, http, di = rig
        di.enqueue_cloud_add({"messages": "正常补算的话", "metadata": {}},
                             "u_calm", "default")
        deleted_ids: list = []
        monkeypatch.setattr(fake, "delete",
                            lambda mid: deleted_ids.append(str(mid)),
                            raising=False)
        rep = di.replay_pending(apply=True)
        assert rep["replayed"] == 1 and rep["revoked_after_scope_delete"] == 0
        assert deleted_ids == []


class TestOuterExceptNotCloudSignal:
    """Y2：外层 except 捕的是复筛/装配等非云腿异常 —— 不许再记云失败。
    最痛场景：半开探测**成功**（云已恢复）而装配段有 bug —— 旧代码会把
    这次成功倒打成失败，熔断被打回 open，云永远「恢复不了」。"""

    class _BoomDict(dict):
        def get(self, *a, **kw):
            raise RuntimeError("装配段炸（非云腿 bug 演练）")

    def test_assembly_error_does_not_knock_half_open_back(self, rig, monkeypatch):
        import time as _time
        client, fake, http, di = rig
        monkeypatch.setattr(fake, "search",
                            lambda *a, **kw: self_boom(), raising=False)

        # 把挡位做进「冷却已过的 open」：下一次请求自动转半开并试云
        t0 = _time.time() - 120
        gear.record_cloud_failure("旧断供", now=t0)
        gear.record_cloud_failure("旧断供", now=t0 + 1)
        gear.record_cloud_failure("旧断供", now=t0 + 2)
        assert gear.gear_status()["breaker"] == "half_open"

        r = http.post("/search", json={"query": "任意查询", "user_id": "u_y2"})
        assert r.status_code == 200, "非云腿异常炸掉了请求"
        st = gear.gear_status()
        assert st["breaker"] != "open", \
            "云探测明明成功，装配 bug 却把熔断打回 open（Y2 未闭合）"

    def test_assembly_error_never_accumulates_failures(self, rig, monkeypatch):
        client, fake, http, di = rig
        monkeypatch.setattr(fake, "search",
                            lambda *a, **kw: self_boom(), raising=False)
        for _ in range(5):
            r = http.post("/search", json={"query": "查一下", "user_id": "u_y2b"})
            assert r.status_code == 200
        st = gear.gear_status()
        assert st["mode"] == "full" and st["consecutive_failures"] == 0, st


def self_boom():
    return TestOuterExceptNotCloudSignal._BoomDict({"results": []})


# ══════════════════════════════════════════════════════════════════
# v20.2.2 · LLM 蒸馏腿挡位（传输层快失败 + 写路径接线）
# 起因：实弹取证 2026-08-26——LLM 网关 521 + openai SDK 尊重
# Retry-After:120 的盲重试，把单次 /add 同步挂 4.5 分钟（嵌入活着）。
# ══════════════════════════════════════════════════════════════════

def _mk_llm_error(msg="LLM extraction failed: Error code: 521"):
    """mem0 的 LLMError 按**类型名**识别（不 import mem0 内部路径——
    形态匹配比路径依赖更抗上游重构）。"""
    return type("LLMError", (Exception,), {})(msg)


class TestLLMGearStateMachine:
    def setup_method(self):
        gear.reset_gear_for_tests()

    def test_llm_leg_independent_from_embed_leg(self):
        t = 1000.0
        for i in range(3):
            gear.record_llm_failure("网关 521", now=t + i)
        assert gear.llm_current_mode(now=t + 4) == "lite"
        assert gear.current_mode(now=t + 4) == "full", \
            "LLM 腿降挡牵连了嵌入腿——两腿状态必须独立"
        gear.reset_gear_for_tests()
        for i in range(3):
            gear.record_cloud_failure("embed down", now=t + i)
        assert gear.current_mode(now=t + 4) == "lite"
        assert gear.llm_current_mode(now=t + 4) == "full", \
            "嵌入腿降挡牵连了 LLM 腿"

    def test_llm_leg_three_state_cycle_with_debounce(self):
        t = 1000.0
        gear.record_llm_failure("x", now=t)
        gear.record_llm_failure("x", now=t + 1)
        assert gear.llm_current_mode(now=t + 2) == "full", "两次失败就降挡——过敏"
        gear.record_llm_failure("x", now=t + 2)
        assert gear.should_try_llm(now=t + 10) is False, "open 冷却中还去撞"
        assert gear.should_try_llm(now=t + 64) is True, "半开必须放真实流量（命门）"
        gear.record_llm_success(now=t + 65)
        assert gear.llm_current_mode(now=t + 66) == "lite", \
            "单次侥幸成功就升挡——假恢复骗过了防抖"
        gear.record_llm_success(now=t + 67)
        assert gear.llm_current_mode(now=t + 68) == "full"

    def test_llm_env_falls_back_like_r1(self, monkeypatch):
        monkeypatch.setenv("AIDUMEI_LLM_GEAR_TRIP_FAILURES", "很多")
        assert gear.llm_trip_threshold() == 3
        errs = gear.llm_gear_status()["config_errors"]
        assert errs and "AIDUMEI_LLM_GEAR_TRIP_FAILURES" in errs


class TestLLMGearWriteWiring:
    """写路径接线：LLMError 上报降挡、挡内直写秒回且 infer=False、
    非 LLM 故障不污染信号（Y2 写侧版）、半开真实写入升挡。"""

    def _spy_infer(self, fake, monkeypatch, infer_seen):
        orig_add = fake.add
        def spy_add(messages, user_id=None, metadata=None, **kw):
            infer_seen.append(kw.get("infer"))
            return orig_add(messages, user_id=user_id, metadata=metadata, **kw)
        monkeypatch.setattr(fake, "add", spy_add)

    def test_llm_error_trips_gear_then_gear_open_skips_layer1(self, rig, monkeypatch):
        import ducky.hot.add as hot_add
        client, fake, http, di = rig
        calls = {"layer1": 0}

        def broken_layer1():
            def _w(mem, msgs, uid, meta, bank_id="default", infer=True):
                calls["layer1"] += 1
                raise _mk_llm_error()
            return _w
        monkeypatch.setattr(hot_add, "lazy_import_layer1", broken_layer1)
        infer_seen: list = []
        self._spy_infer(fake, monkeypatch, infer_seen)

        for i in range(3):
            r = http.post("/add", json={"messages": f"LLM 断供期写入 {i}",
                                        "user_id": "u_llm"})
            assert r.status_code == 200, "LLM 死了 add 不许 500"
            body = r.json()
            assert body.get("action") == "direct"
            assert body.get("distillation") == "skipped_llm_error", body
        assert all(v is False for v in infer_seen), \
            f"LLM 死了 fallback 还想走 LLM 抽取（洞③未闭合）: {infer_seen}"
        assert gear.llm_current_mode() == "lite", "3 次 LLMError 没降挡"
        assert gear.current_mode() == "full", "LLM 故障污染了嵌入腿"

        # 挡位 open：后续 add 一下 layer1 都不碰（不再逐请求付超时）
        n = calls["layer1"]
        r = http.post("/add", json={"messages": "挡内秒回写入",
                                    "user_id": "u_llm"})
        assert r.status_code == 200
        assert r.json().get("distillation") == "skipped_llm_gear_open"
        assert calls["layer1"] == n, \
            "挡位 open 还在探 layer1——每次 add 都要重新撞一遍超时"
        # 挡内写入照样落库：内容进了 fake mem（嵌入活着，云向量照打）
        assert any("挡内秒回写入" in str(c["messages"]) for c in fake.add_calls)

    def test_non_llm_failure_keeps_old_semantics_and_pure_signal(self, rig, monkeypatch):
        import ducky.hot.add as hot_add
        client, fake, http, di = rig

        def crashy_layer1():
            def _w(mem, msgs, uid, meta, bank_id="default", infer=True):
                raise ValueError("FTS 崩了（非 LLM 故障演练）")
            return _w
        monkeypatch.setattr(hot_add, "lazy_import_layer1", crashy_layer1)
        infer_seen: list = []
        self._spy_infer(fake, monkeypatch, infer_seen)

        for i in range(4):
            r = http.post("/add", json={"messages": f"非 LLM 故障写入 {i}",
                                        "user_id": "u_nl"})
            assert r.status_code == 200
            assert r.json().get("distillation") is None, \
                "非 LLM 故障不该打蒸馏跳过注记"
        assert gear.llm_current_mode() == "full", \
            "ValueError 污染了 LLM 腿信号（Y2 写侧版失守）"
        assert all(v is True for v in infer_seen), \
            "非 LLM 故障的降级分支必须透传 infer（v20 纪律）"

    def test_half_open_recovers_via_real_adds(self, rig, monkeypatch):
        import time as _time
        import ducky.hot.add as hot_add
        client, fake, http, di = rig
        t0 = _time.time() - 120
        for i in range(3):
            gear.record_llm_failure("旧断供", now=t0 + i)
        assert gear.llm_gear_status()["breaker"] == "half_open"

        def healthy_layer1():
            def _w(mem, msgs, uid, meta, bank_id="default", infer=True):
                return {"status": "ok", "action": "indexed"}
            return _w
        monkeypatch.setattr(hot_add, "lazy_import_layer1", healthy_layer1)
        http.post("/add", json={"messages": "恢复探测一", "user_id": "u_rec"})
        assert gear.llm_current_mode() == "lite", "单次成功不许升挡"
        http.post("/add", json={"messages": "恢复探测二", "user_id": "u_rec"})
        assert gear.llm_current_mode() == "full", \
            "半开两次真实写入成功仍未升挡——恢复链断了"

    def test_direct_write_inner_llm_error_self_purifies(self, rig, monkeypatch):
        """fallback 自身纯化：非 LLM 故障降级直写（infer 透传 True）时
        内层 mem.add 撞上 LLMError → 就地降 infer=False，不 500。"""
        import ducky.hot.add as hot_add
        client, fake, http, di = rig

        def crashy_layer1():
            def _w(mem, msgs, uid, meta, bank_id="default", infer=True):
                raise ValueError("非 LLM 故障")
            return _w
        monkeypatch.setattr(hot_add, "lazy_import_layer1", crashy_layer1)
        orig_add = fake.add
        infer_seen: list = []

        def add_llm_dead(messages, user_id=None, metadata=None, **kw):
            infer_seen.append(kw.get("infer"))
            if kw.get("infer"):
                raise _mk_llm_error()
            return orig_add(messages, user_id=user_id, metadata=metadata, **kw)
        monkeypatch.setattr(fake, "add", add_llm_dead)

        r = http.post("/add", json={"messages": "双重故障写入", "user_id": "u_dj"})
        assert r.status_code == 200, "fallback 自己 500 了——洞③还在"
        assert r.json().get("distillation") == "skipped_llm_error"
        assert infer_seen == [True, False], infer_seen
        assert gear.llm_gear_status()["consecutive_failures"] == 1, \
            "直写内层的 LLMError 没有上报挡位"


class TestLLMTransportPolicy:
    """mem0 内部 openai 客户端的传输策略补丁（盲重试上移给挡位）。"""

    def test_patch_clips_blind_retries(self):
        from ducky.mem0_patches import _patch_llm_transport_policy, patch_status
        seen = {}

        class _C:
            def with_options(self, **kw):
                seen.update(kw)
                return ("patched", kw)

        class _NS:  # noqa: N801
            pass
        m = _NS(); m.llm = _NS(); m.llm.client = _C()
        _patch_llm_transport_policy(m)
        assert seen.get("max_retries") == 0, "盲重试没掐"
        assert seen.get("timeout") is not None, "没设有限超时"
        assert m.llm.client[0] == "patched", "新客户端没挂回去"
        st = patch_status()["patches"].get("llm_transport_policy", {})
        assert st.get("status") == "applied", st

    def test_patch_reports_drift_on_shape_change(self):
        from ducky.mem0_patches import _patch_llm_transport_policy, patch_status

        class _NS:  # noqa: N801
            llm = None
        _patch_llm_transport_policy(_NS())
        assert patch_status()["patches"]["llm_transport_policy"]["status"] == "drift", \
            "挂载点消失必须报 drift，不许静默空转（§5 usage_tracking 的死法）"
        # 还原账本态，避免污染后续断言 patch_status().ok 的测试
        seen = {}

        class _C:
            def with_options(self, **kw):
                seen.update(kw)
                return self
        class _M:  # noqa: N801
            pass
        m = _M(); m.llm = _M(); m.llm.client = _C()
        _patch_llm_transport_policy(m)
        assert patch_status()["patches"]["llm_transport_policy"]["status"] == "applied"
