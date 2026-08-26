"""tests/test_v20_1_1_scope_hardening.py — v20.1.1 行为面点名验收

对应《v20.1 公开后外审复核》净新增行动项：
  N-1 限流护栏（写路径 + delete_all，外审建议采纳·按生产 14 天实测定值）
  N-2 metadata 形态白名单（外审 P1-5）
  N-6 R-18 观察库/场景库作用域删除（两轮外审共同挂账）+ persona 改判断言
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ducky.rate_guard import check_rate, reset_rate_windows  # noqa: E402


# ══════════════════════════════════════════════════════════════════
# N-1 · 限流：窗口语义 / 租户隔离 / 关闭态 / 路由 429
# ══════════════════════════════════════════════════════════════════

class TestRateGuard:
    def setup_method(self):
        reset_rate_windows()

    def test_window_semantics_and_retry_after(self):
        t0 = 1_700_000_040.0  # = 28_333_334 分钟整点（选整点让期望值可精确算）
        assert check_rate("r", "u1", limit=2, now=t0) is None
        assert check_rate("r", "u1", limit=2, now=t0 + 1) is None
        retry = check_rate("r", "u1", limit=2, now=t0 + 2)
        assert retry == 58, \
            f"Retry-After 应为到下一分钟的精确剩余秒 58（实得 {retry}）"
        # 下一自然分钟恢复
        assert check_rate("r", "u1", limit=2, now=t0 + 60) is None

    def test_tenants_do_not_contaminate_each_other(self):
        t0 = 1_700_000_000.0
        for i in range(3):
            check_rate("r", "noisy", limit=2, now=t0 + i * 0.1)
        assert check_rate("r", "quiet", limit=2, now=t0 + 1) is None, \
            "一个租户超限不许牵连别人"

    def test_zero_limit_disables(self):
        t0 = 1_700_000_000.0
        for i in range(50):
            assert check_rate("r", "u", limit=0, now=t0 + i * 0.01) is None

    def test_invalid_env_raises_by_name(self, monkeypatch):
        from ducky.rate_guard import add_rate_limit
        monkeypatch.setenv("AIDUMEI_RATE_ADD_PER_MIN", "很快")
        with pytest.raises(ValueError, match="AIDUMEI_RATE_ADD_PER_MIN"):
            add_rate_limit()

    def test_delete_all_route_returns_429_with_retry_after(self, monkeypatch, tmp_path):
        """超限的 delete_all 必须 429 + Retry-After，绝不静默放行。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        import ducky.rate_guard as rg
        import ducky.utils as utils
        from ducky.hot.crud import register_crud_routes

        monkeypatch.setattr(utils, "FACTS_DB", str(tmp_path / "facts.db"))
        monkeypatch.setattr(rg, "delete_all_rate_limit", lambda: 1)
        reset_rate_windows()

        app = FastAPI()
        register_crud_routes(app)
        client = TestClient(app)
        client.post("/delete_all", json={"user_id": "rl_user"})  # 第 1 次占额度
        r2 = client.post("/delete_all", json={"user_id": "rl_user"})
        assert r2.status_code == 429, r2.text
        assert "Retry-After" in r2.headers
        assert "AIDUMEI_RATE_DELETE_ALL_PER_MIN" in r2.json()["detail"], \
            "429 文案必须教会调用方怎么调上限"

    def test_add_route_returns_429(self, monkeypatch, tmp_path):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        import ducky.rate_guard as rg
        import ducky.utils as utils
        from ducky.hot.add import register_add_routes

        monkeypatch.setattr(utils, "FACTS_DB", str(tmp_path / "facts.db"))
        monkeypatch.setattr(rg, "add_rate_limit", lambda: 1)
        reset_rate_windows()

        app = FastAPI()
        register_add_routes(app)
        client = TestClient(app)
        client.post("/add", json={"messages": "第一条", "user_id": "rl_add"})
        r2 = client.post("/add", json={"messages": "第二条", "user_id": "rl_add"})
        assert r2.status_code == 429, r2.text
        assert "Retry-After" in r2.headers

    def test_health_exposes_effective_limits(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from ducky.hot.health import register_health_routes

        app = FastAPI()
        register_health_routes(app)
        probes = TestClient(app).get("/health").json()["probes"]
        assert probes.get("rate_add_per_min_effective") == 120
        assert probes.get("rate_delete_all_per_min_effective") == 3


# ══════════════════════════════════════════════════════════════════
# N-2 · metadata 形态白名单
# ══════════════════════════════════════════════════════════════════

class TestMetadataShape:
    def _mk(self, md):
        from ducky.api_models import AddRequest
        return AddRequest(messages="x", metadata=md)

    def test_normal_and_cjk_keys_pass(self):
        """存量兼容正查：正常键与中文键必须照旧通过（收紧不许伤正用）。"""
        md = {"source": "hermes", "bank_id": "default", "recorded_at": "2026-08-26",
              "media_url": "https://x/y.png", "会话轮次": 3, "x_custom-tag.v2": "ok"}
        assert self._mk(md).metadata == md

    def test_key_count_cap(self):
        with pytest.raises(ValueError, match="键数"):
            self._mk({f"k{i}": i for i in range(33)})

    def test_key_name_shape(self):
        with pytest.raises(ValueError, match="键名不合法"):
            self._mk({"<script>alert(1)</script>": "x"})
        with pytest.raises(ValueError, match="键名不合法"):
            self._mk({"k" * 65: "x"})

    def test_single_value_cap(self):
        with pytest.raises(ValueError, match="单值上限"):
            self._mk({"blob": "灾" * 4097})

    def test_total_payload_cap(self):
        with pytest.raises(ValueError, match="总载荷"):
            self._mk({f"k{i}": "值" * 800 for i in range(21)})

    def test_nesting_depth_cap(self):
        with pytest.raises(ValueError, match="嵌套深度"):
            self._mk({"a": {"b": {"c": 1}}})
        assert self._mk({"a": {"b": 1}}).metadata  # 深度 2 合法


# ══════════════════════════════════════════════════════════════════
# N-6 · R-18：观察库 / 场景库作用域删除 + persona 改判
# ══════════════════════════════════════════════════════════════════

@pytest.fixture()
def r18_sandbox(monkeypatch, tmp_path):
    import ducky.core_memory as cm
    import ducky.mem0_runtime as runtime
    import ducky.refine_memory as refine_memory
    import ducky.utils as utils
    from ducky.schema_bootstrap import ensure_core_schema

    db_path = str(tmp_path / "facts.db")
    monkeypatch.setattr(utils, "FACTS_DB", db_path)
    monkeypatch.setattr(refine_memory, "_checked", False)
    monkeypatch.setattr(cm, "_initialized", False)
    cm._initialized_scopes.clear()

    class _Vec:
        def insert(self, *a, **kw): pass
        def get(self, *a, **kw): return None
    class _Mem:
        embedding_model = type("E", (), {"embed": staticmethod(lambda t, a: [0.1])})()
        vector_store = _Vec()
        def get_all(self, *a, **kw): return {"results": []}
        def search(self, *a, **kw): return {"results": []}
    monkeypatch.setattr(runtime, "get_memory", lambda: _Mem())

    ensure_core_schema(force=True)
    # 三库建表走各自模块的 ensure（传入独立 conn，避开 legacy_helpers
    # 线程缓存连接绑死 import 时路径的坑——from-import 中毒同族）。
    from ducky.hot.legacy_helpers import _ensure_observations_table, _ensure_scenes_table
    conn = sqlite3.connect(db_path)
    _ensure_observations_table(conn)
    _ensure_scenes_table(conn)
    conn.close()

    def query(sql, params=()):
        c = sqlite3.connect(db_path)
        try:
            return c.execute(sql, params).fetchall()
        finally:
            c.close()
    return query, db_path


def _seed_r18(db_path):
    conn = sqlite3.connect(db_path)
    try:
        for uid, summary in (("r18_victim", "受害者的观察一"), ("r18_victim", "受害者的观察二"),
                             ("r18_bystander", "旁观者的观察"), ("", "v7 存量无主观察")):
            conn.execute(
                "INSERT INTO observations (category, summary, content, user_id) VALUES (?,?,?,?)",
                ("general", summary, summary + "正文", uid))
        for uid in ("r18_victim", "r18_bystander"):
            conn.execute(
                "INSERT INTO scenes (category, summary, member_keys, user_id, bank_id) VALUES (?,?,?,?,?)",
                ("cat", f"{uid} 的场景", f"{uid}_keys", uid, "default"))
        conn.commit()
    finally:
        conn.close()


class TestR18ScopedDeletion:
    def test_delete_all_clears_observations_and_scenes(self, r18_sandbox):
        query, db_path = r18_sandbox
        _seed_r18(db_path)
        from ducky.wal_engine import cascade_delete_all

        out = cascade_delete_all("r18_victim")
        det = out["details"]
        assert det.get("observations_deleted") == 2, det
        assert det.get("scenes_deleted") == 1, det
        assert query("SELECT 1 FROM observations WHERE user_id='r18_victim'") == []
        assert query("SELECT 1 FROM scenes WHERE user_id='r18_victim'") == []

    def test_cross_tenant_and_legacy_rows_survive(self, r18_sandbox):
        """负向对照：旁观者不连坐；v7 无主存量行（user_id=''）不动——
        它们不属于任何租户，删了才是错。"""
        query, db_path = r18_sandbox
        _seed_r18(db_path)
        from ducky.wal_engine import cascade_delete_all

        cascade_delete_all("r18_victim")
        assert query("SELECT 1 FROM observations WHERE user_id='r18_bystander'"), "旁观者观察被连坐"
        assert query("SELECT 1 FROM scenes WHERE user_id='r18_bystander'"), "旁观者场景被连坐"
        assert query("SELECT 1 FROM observations WHERE user_id=''"), "v7 无主存量行被误删"

    def test_matrix_meta_guard_now_covers_both_tables(self, r18_sandbox):
        """两张表实建后，元守卫的射程必须覆盖它们（矩阵沉默即红）。"""
        query, _ = r18_sandbox
        from ducky.wal_engine import DELETE_CHAIN_MATRIX
        tables = {r[0] for r in query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        assert {"observations", "scenes"} <= tables
        undecided = tables - set(DELETE_CHAIN_MATRIX)
        assert not undecided, f"矩阵沉默的表：{sorted(undecided)}"
        assert DELETE_CHAIN_MATRIX["observations"][0] == "clean"
        assert DELETE_CHAIN_MATRIX["scenes"][0] == "clean"

    def test_persona_exemption_is_semantic_not_lazy(self):
        """persona 豁免的理由必须是「租户轴正交」（复核改判），
        不许退回「接口未做」那种懒豁免。"""
        from ducky.wal_engine import DELETE_CHAIN_MATRIX
        action, reason = DELETE_CHAIN_MATRIX["store:persona"]
        assert action == "exempt"
        assert "正交" in reason and "persona_key" in reason
