"""tests/test_v20_1_recall_verdict.py — v20.1 WP-C 召回弃答信号

三态矩阵（预案验收门槛原文）：
  · 库中有   → found，confidence 与最高分一致；
  · 库中确无 → not_found（负向：有结果的查询绝不误报 not_found）；
  · 掐断嵌入 → degraded，**不许**伪装成 not_found —— 判定顺序即契约，
    故障先于缺失；「没搜到」和「搜挂了」是这个项目反复付过学费的一对同形词。

阈值纪律：默认 0.0（只有空结果才判 not_found），显式配置才启用低分判
not_found，且**结果照常返回** —— verdict 是随行判语，不越权丢数据。
生效值从 /health 可读（配置生效三查：问解析者，不问文件）。
"""
from __future__ import annotations

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ducky.hot.search as hot_search  # noqa: E402
from ducky.hot.search import (  # noqa: E402
    _VERDICT_THRESHOLD_ENV,
    _verdict_threshold,
    compute_recall_verdict,
)

# ══════════════════════════════════════════════════════════════════
# 1. 纯函数层：判定顺序即契约
# ══════════════════════════════════════════════════════════════════

R1 = [{"id": "m1", "memory": "内容", "score": 0.72}]


class TestComputeVerdict:
    def test_nonempty_scored_is_found(self):
        assert compute_recall_verdict(R1, 0.72, 0.0) == ("found", "scored")

    def test_empty_clean_is_not_found(self):
        assert compute_recall_verdict([], None, 0.0) == ("not_found", "empty_results")

    def test_empty_with_vector_leg_failure_is_degraded_not_not_found(self):
        """核心负向对照：腿断产生的空，判 degraded —— 「没有」是不可知，不是不存在。"""
        verdict, basis = compute_recall_verdict([], None, 0.0, vector_leg_failed=True)
        assert verdict == "degraded", "搜挂了被伪装成了查无此忆"
        assert basis == "empty_after_leg_failure"

    def test_empty_on_mem0_degraded_path_is_degraded(self):
        verdict, _ = compute_recall_verdict([], None, 0.0, recall_path="mem0_degraded")
        assert verdict == "degraded"

    def test_failure_with_results_still_judged_by_score(self):
        """腿断但兜底有结果：按分数照常判 —— degraded 只保护「空」的歧义。"""
        verdict, _ = compute_recall_verdict(R1, 0.72, 0.0, vector_leg_failed=True)
        assert verdict == "found"

    def test_below_threshold_is_not_found(self):
        assert compute_recall_verdict(R1, 0.72, 0.9) == ("not_found", "below_threshold")

    def test_at_threshold_is_found(self):
        assert compute_recall_verdict(R1, 0.9, 0.9)[0] == "found"

    def test_zero_threshold_never_judges_by_score(self):
        assert compute_recall_verdict(R1, 0.01, 0.0)[0] == "found"


class TestThresholdParsing:
    def test_unset_is_zero(self, monkeypatch):
        monkeypatch.delenv(_VERDICT_THRESHOLD_ENV, raising=False)
        assert _verdict_threshold() == 0.0

    def test_valid_value_is_honored(self, monkeypatch):
        monkeypatch.setenv(_VERDICT_THRESHOLD_ENV, "0.35")
        assert _verdict_threshold() == 0.35

    def test_invalid_value_warns_and_falls_back(self, monkeypatch, caplog):
        """铁律 13：「设了打错的阈值」和「没设」行为一样、意图不同 —— 必须出声。"""
        monkeypatch.setenv(_VERDICT_THRESHOLD_ENV, "很高")
        with caplog.at_level(logging.WARNING, logger="aiduMEM.hot"):
            assert _verdict_threshold() == 0.0
        assert any(_VERDICT_THRESHOLD_ENV in r.message for r in caplog.records)

    def test_out_of_range_warns_and_falls_back(self, monkeypatch, caplog):
        monkeypatch.setenv(_VERDICT_THRESHOLD_ENV, "1.5")
        with caplog.at_level(logging.WARNING, logger="aiduMEM.hot"):
            assert _verdict_threshold() == 0.0
        assert any("超出" in r.message for r in caplog.records)


# ══════════════════════════════════════════════════════════════════
# 2. 路由层：真实 /search 响应携带三态
# ══════════════════════════════════════════════════════════════════

class _RaisingMemory:
    """嵌入服务挂掉的形态：mem0.search 直接抛。"""

    def search(self, *a, **kw):
        raise RuntimeError("embedding provider down (模拟)")


@pytest.fixture()
def search_client(monkeypatch):
    """最小 /search 路由。默认腿全通；单个用例再按需换腿。

    workspace 与 verbatim 旁路（monkeypatch 成不命中）：它们各有自己的
    用例文件，这里只测三态判定这一层。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import ducky.memory_workspace as ws

    monkeypatch.setattr(ws, "ws_lookup", lambda *a, **kw: [])
    monkeypatch.setattr(ws, "ws_feed_from_results", lambda *a, **kw: None)
    monkeypatch.setattr(hot_search, "boost_salience_for_results", lambda *a, **kw: None)
    monkeypatch.setattr(hot_search, "get_memory", lambda: _RaisingMemory())

    app = FastAPI()
    hot_search.register_search_routes(app)
    return TestClient(app), monkeypatch


def _post(client, query="测试查询", **extra):
    return client.post("/search", json={"query": query, "user_id": "wpc_user", **extra})


def test_route_found_with_confidence(search_client):
    client, mp = search_client
    hits = [{"id": "m1", "memory": "用户偏好简洁接口", "score": 0.83},
            {"id": "m2", "memory": "次要结果", "score": 0.41}]
    mp.setattr(hot_search, "lazy_import_hybrid",
               lambda: (lambda mem, q, uid, limit, **kw: list(hits)))
    body = _post(client).json()
    assert body["status"] == "ok"
    assert body["recall_verdict"] == "found"
    assert body["verdict_basis"] == "scored"
    assert body["recall_confidence"] == pytest.approx(0.83)


def test_route_empty_clean_is_not_found(search_client):
    client, mp = search_client
    mp.setattr(hot_search, "lazy_import_hybrid",
               lambda: (lambda mem, q, uid, limit, **kw: []))
    body = _post(client, query="库里根本没有的东西").json()
    assert body["recall_verdict"] == "not_found"
    assert body["verdict_basis"] == "empty_results"
    assert body["results"] == []


def test_route_embedding_down_is_degraded_not_not_found(search_client):
    """整个 WP-C 的靶心：嵌入挂了 → 空结果必须判 degraded。

    这里不 mock 判定函数，走**真实链条**：hybrid_search → RecallEngine →
    mem.search 抛 → 引擎腿遥测记 failed → 路由读遥测 → degraded。
    对照（区分力）由上一条用例担任：同样的空结果、腿没断 → not_found。
    两条并立，才证明 degraded 不是「空结果」的别名。
    """
    client, _ = search_client  # get_memory 已是 _RaisingMemory，真引擎真遥测
    body = _post(client, query="任何查询").json()
    assert body["status"] == "ok"
    assert body["recall_verdict"] == "degraded", \
        f"搜挂了被伪装成 {body.get('recall_verdict')} —— 反静默降级铁律"
    assert body["verdict_basis"] == "empty_after_leg_failure"
    # v20.2 自动挡：云腿失败后本请求就地落本地腿（无感 fallback），
    # 但备胎空手（本环境无本地索引）时判语必须仍是 degraded ——
    # 「备胎接住了」与「云断且备胎也空手」是两回事，后者不许装 not_found。
    assert body["_recall_legs"].get("vector_leg") == "local_fallback"
    assert "embedding" in body["_recall_legs"].get("error", "")


def test_route_below_threshold_drops_the_results_it_distrusts(search_client, monkeypatch):
    """**契约在 2026-08-29 反转了（社区 Issue #5）。**

    这条用例原来叫 `..._marks_not_found_but_keeps_results`，断言的是
    「verdict 是判语不是过滤器：低分判 not_found，结果一条不许丢」。
    那个设计的本意是「判语不越权丢数据」，本身讲得通 —— 但它造出了一个
    自相矛盾的输出：部署方已经用 `AIDUMEI_RECALL_VERDICT_THRESHOLD` 声明
    「低于这个分不可信」，系统据此判了 `not_found`，**却把同一批结果原样返回**。
    「我知道这批不靠谱」和「我照样给你」同时成立。

    社区网友的 Agent 提的正是这个病（弱命中凑分填满结果集），
    而实机实测坐实了它：生产库上问一个毫不相干的问题，三条全无关的记忆
    （0.2862 / 0.2819 / 0.2362）照样被返回，verdict 同时写着 not_found。

    所以现在：**低于已标定下限的结果不再返回**（`_score_floor` 未显式设置时
    回落到该阈值）。要回到旧行为，显式写 `AIDUMEM_RECALL_SCORE_FLOOR=0`
    —— 下面第二段就是在验这个逃生门，没有它这条反转就是不可回退的。
    """
    client, mp = search_client
    hits = [{"id": "m1", "memory": "弱相关", "score": 0.2}]
    mp.setattr(hot_search, "lazy_import_hybrid",
               lambda: (lambda mem, q, uid, limit, **kw: list(hits)))
    monkeypatch.delenv("AIDUMEM_RECALL_SCORE_FLOOR", raising=False)
    monkeypatch.setenv(_VERDICT_THRESHOLD_ENV, "0.6")
    body = _post(client).json()
    assert body["recall_verdict"] == "not_found"
    assert body["results"] == [], "低于已标定下限的结果不该再返回（Issue #5）"

    # 逃生门：显式关掉下限，旧行为完整回来（判语照旧、结果照旧给）
    monkeypatch.setenv("AIDUMEM_RECALL_SCORE_FLOOR", "0")
    body2 = _post(client).json()
    assert body2["recall_verdict"] == "not_found"
    assert body2["verdict_basis"] == "below_threshold"
    assert len(body2["results"]) == 1, "关掉下限后必须回到「判语不丢数据」的旧行为"


def test_route_workspace_hit_is_found(search_client, monkeypatch):
    client, _ = search_client
    import ducky.memory_workspace as ws
    monkeypatch.setattr(
        ws, "ws_lookup",
        lambda *a, **kw: [{"id": "w1", "memory": "热缓存命中", "score": 0.9}],
    )
    body = _post(client).json()
    assert body["recall_verdict"] == "found"
    assert body["verdict_basis"] == "workspace_hit"


def test_telemetry_reset_between_requests(search_client, monkeypatch):
    """腿断残留不许串请求：先打一发 degraded，再换好腿 —— 必须回 found。"""
    client, mp = search_client
    body1 = _post(client).json()
    assert body1["recall_verdict"] == "degraded"

    mp.setattr(hot_search, "lazy_import_hybrid",
               lambda: (lambda mem, q, uid, limit, **kw: [
                   {"id": "m1", "memory": "好结果", "score": 0.7}]))
    body2 = _post(client).json()
    assert body2["recall_verdict"] == "found", \
        "上一请求的腿断遥测泄漏到了本请求 —— reset 没生效"


# ══════════════════════════════════════════════════════════════════
# 3. /health 观测面
# ══════════════════════════════════════════════════════════════════

def test_health_exposes_verdict_threshold_effective(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ducky.hot.health import register_health_routes

    monkeypatch.setenv(_VERDICT_THRESHOLD_ENV, "0.35")
    app = FastAPI()
    register_health_routes(app)
    probes = TestClient(app).get("/health").json()["probes"]
    assert probes.get("recall_verdict_threshold_effective") == 0.35, \
        "生效阈值问不到 —— 配置生效三查缺了问解析者这一查"
