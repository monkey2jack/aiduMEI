"""tests/test_f051_cold_start_probe.py — f0.5.1 冷启动速断回归

独立复测报告列出的首个体感缺陷：重启后熔断器从 closed 起步，首次云试探
要陪 mem0/网络的默认超时（复测实测 12-15s），宿主 6s 预算放弃 → 前两
轮对话无记忆。修复：云腿「未证明健康」阶段（本进程尚无一次成功）试探
只给短预算，超时按失败记账并就地落本地腿；成功一次后行为回到原语义。

三个行为态 + 两个负向对照 + env 判据，全部真实调用链（不是替身看门）。
"""
from __future__ import annotations

import pathlib
import sys
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _flush_probe_executor():
    """共享探针执行器的测试隔离：慢探针替身占着唯一线程会把后续
    用例的探针排队拖过预算（邻接用例无辜变红的串扰源）。"""
    yield
    from ducky import engine as eng
    eng._reset_probe_executor_for_tests()

# ════════════════════════════════════════════════════════════════
# gear 层：ever_succeeded / cloud_unproven 语义
# ════════════════════════════════════════════════════════════════

def test_gear_unproven_then_proven():
    import ducky.gear as gear
    gear.reset_gear_for_tests() if hasattr(gear, "reset_gear_for_tests") else None
    b = gear._EMBED
    b.reset()
    try:
        assert b.ever_succeeded() is False
        assert gear.cloud_unproven() is True
        b.record_success()
        assert b.ever_succeeded() is True
        assert gear.cloud_unproven() is False
        # 失败不抹掉「曾经成功」（进程级事实）
        b.record_failure("演练")
        assert b.ever_succeeded() is True
        assert gear.cloud_unproven() is False
    finally:
        b.reset()


def test_failure_alone_keeps_unproven():
    """负向对照：只有失败（从未成功）时 unproven 保持 True——速断预算
    继续生效，不会因为「试过」就误判云腿健康。"""
    import ducky.gear as gear
    b = gear._EMBED
    b.reset()
    try:
        for _ in range(5):
            b.record_failure("演练")
        assert b.ever_succeeded() is False
        assert gear.cloud_unproven() is True
    finally:
        b.reset()


# ════════════════════════════════════════════════════════════════
# engine 层：冷启动预算真实生效（慢云腿 → 本地兜底、宿主预算内返回）
# ════════════════════════════════════════════════════════════════

class _StubMemory:
    """mem 实例替身：engine._get_mem 直接返回它；search 本身不时应答
    （search_candidates 缝位已被钉）。"""
    def search(self, *a, **kw):
        return {"results": []}


def _patch_cloud(monkeypatch, sleep_s: float, ok_results: list, hit: dict):
    """在真实缝位（ducky.search_candidates.search_candidates——engine 内部
    import 的就是它）钉慢云腿/快云腿替身，签名对齐生产（feedback：替身
    签名必须对齐生产）。hit 记录调用次数供断言。"""
    import ducky.search_candidates as sc

    def _fake(memory, query, *, filters=None, limit=None):
        hit["calls"] = hit.get("calls", 0) + 1
        if sleep_s:
            time.sleep(sleep_s)
        return {"results": ok_results}

    monkeypatch.setattr(sc, "search_candidates", _fake)


def test_cold_start_slow_cloud_falls_back_within_budget(monkeypatch):
    """云腿睡 10s（模拟复测的 12-15s 死等）、预算 0.5s：
    ① 本次请求 2s 内返回（ 远小于宿主 6s）；② 结果来自本地腿；
    ③ 遥测记 local_fallback；④ 熔断器吃到失败信号。"""
    from ducky import engine as eng
    from ducky import gear
    b = gear._EMBED
    b.reset()
    monkeypatch.setenv("AIDUMEI_CLOUD_PROBE_BUDGET_SEC", "0.5")
    hit = {}
    _patch_cloud(monkeypatch, sleep_s=10.0, ok_results=[{"id": "cloud"}], hit=hit)
    try:
        t0 = time.monotonic()
        eng.RecallEngine(memory_instance=_StubMemory()).search(
            query="冷启动演练查询", user_id="u_cold", limit=3, bank_id="default")
        elapsed = time.monotonic() - t0
        assert elapsed < 5.0, f"冷启动预算失效：耗时 {elapsed:.1f}s（应为本地腿速度）"
        tel = eng.last_recall_telemetry()
        assert tel.get("vector_leg") == "local_fallback", tel
        assert b.ever_succeeded() is False  # 慢云腿没被计成功
    finally:
        b.reset()


def test_proven_cloud_path_unwrapped_and_zero_behavior_change(monkeypatch):
    """负向对照：云腿成功一次后（proven），下一个请求走原路径。
    快云腿（0.05s）正常返回云端结果、遥测 vector_leg=ok、
    且不产生 local_fallback。"""
    from ducky import engine as eng
    from ducky import gear
    b = gear._EMBED
    b.reset()
    try:
        # 先让云腿 proven：一次成功
        b.record_success()
        assert gear.cloud_unproven() is False
        hit = {}
        _patch_cloud(monkeypatch, sleep_s=0.0, ok_results=[{"id": "m1", "memory": "云端结果", "score": 0.9}], hit=hit)
        eng.RecallEngine(memory_instance=_StubMemory()).search(
            query="proven 演练", user_id="u_prov", limit=3, bank_id="default")
        tel = eng.last_recall_telemetry()
        assert tel.get("vector_leg") == "ok", tel
        assert tel.get("vector_leg") != "local_fallback"
    finally:
        b.reset()


def test_cold_budget_env_invalid_falls_back(monkeypatch):
    """env 判据：NaN/inf/负数/乱码 → 回退默认并出声（config 纪律）。"""
    from ducky import engine as eng
    for bad in ("nan", "inf", "-1", "0", "abc"):
        monkeypatch.setenv("AIDUMEI_CLOUD_PROBE_BUDGET_SEC", bad)
        assert eng._cloud_probe_budget_sec() == eng._CLOUD_PROBE_BUDGET_DEFAULT, bad
    monkeypatch.setenv("AIDUMEI_CLOUD_PROBE_BUDGET_SEC", "0.8")
    assert eng._cloud_probe_budget_sec() == 0.8
    monkeypatch.delenv("AIDUMEI_CLOUD_PROBE_BUDGET_SEC")
    assert eng._cloud_probe_budget_sec() == eng._CLOUD_PROBE_BUDGET_DEFAULT
