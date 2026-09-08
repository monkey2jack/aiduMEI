"""
tests/test_v20_4_health_split.py — v20.4 P2-17：/health 拆分为 livez/readyz/diagnostics
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
上轮 P3 登记继承（Codex F-12）：/health 一个端点身兼「负载均衡探活」
与「640 行深度诊断」两职 —— 高频探活被迫为深度探针付成本，深度诊断
又被迫为匿名调用方做留键脱敏。本轮按探针成本分级拆开：

  · /livez       —— O(1) 探活：进程能应答即活，不碰磁盘/数据库/单例；
  · /readyz      —— 廉价就绪检查（文件存在/目录可写/schema 对齐），
                    失败 503 + failed 名单，绝不外呼、不 COUNT、不读大表；
  · /diagnostics —— 完整深度探针，门禁启用时匿名 → 401（不做公开视图）；
  · /health      —— 对外契约不变（e2e_smoke / drill / mcp / hermes 全在读它）。

覆盖：
  1. /livez 匿名可达、O(1) 形状（无 probes）
  2. /readyz 绿灯形状与四项廉价检查
  3. /readyz 负向：facts_db 缺失 → 503 + failed 记名 + 不外泄路径
  4. /diagnostics 门禁启用：匿名 401，凭据 200 且是完整探针
  5. /diagnostics 门禁未启用：匿名也放行完整探针
  6. /health 兼容：匿名仍拿留键公开视图
  7. api_server 中间件层契约：livez/readyz 在公开路径集，diagnostics 不在
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

_TOKEN = "test-split-token"


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ducky.hot.health import register_health_routes

    app = FastAPI()
    register_health_routes(app)
    return TestClient(app)


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setenv("AIDUMEM_API_TOKEN", _TOKEN)
    return _TOKEN


# ──────────────────────────────────────────────
# 1. /livez：O(1) 探活
# ──────────────────────────────────────────────
def test_livez_anonymous_o1(gate_on):
    """探活是负载均衡器的事，高频打、不带凭据；能应答即活。"""
    from ducky.version import SERVICE_VERSION

    r = _client().get("/livez")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"] == SERVICE_VERSION
    assert body["uptime_seconds"] >= 0
    # O(1) 的形状证明：不跑深度探针，响应里没有 probes
    assert "probes" not in body


# ──────────────────────────────────────────────
# 2. /readyz：廉价就绪检查
# ──────────────────────────────────────────────
def test_readyz_green_shape(gate_on, monkeypatch, tmp_path):
    """四项廉价检查全过 → 200 + ready=true。匿名可达（不泄路径/凭据）。

    前置：先按运行时的真实路径完成首跑引导（facts schema 迁移 +
    memories 建表）——「就绪」的语义是「已引导的实例能服务」，不是
    「裸目录也算就绪」（裸目录就绪正是外审 F-01 抓过的 bind-mount
    事故形态，必须由 facts_db 检查挡住）。

    隔离：全量语境下多个测试模块在**模块顶层**把 ``utils.FACTS_DB`` 永久
    改指到自己的临时库（不还原），而 ``ducky.hot.health`` 模块顶的
    ``FACTS_DB`` 又是 import 那一刻冻结的第三处 —— 同一进程里最多同时
    存在三个 facts.db 路径。所以本用例把两套引用显式钉进同一个
    tmp_path（monkeypatch 收尾还原，不新增污染），就绪判据才有确定性。
    """
    import ducky.hot.health as health_mod
    import ducky.utils as utils
    from ducky.schema_bootstrap import ensure_core_schema
    from ducky.text_fts import _ensure_trigram_fts
    from ducky.utils import get_text_conn

    facts = str(tmp_path / "facts.db")
    text = str(tmp_path / "text_fts.db")
    monkeypatch.setattr(utils, "FACTS_DB", facts)
    monkeypatch.setattr(utils, "TEXT_FTS_DB", text)
    monkeypatch.setattr(health_mod, "FACTS_DB", facts)
    monkeypatch.setattr(health_mod, "TEXT_FTS_DB", text)

    ensure_core_schema(force=True)
    conn = get_text_conn()
    _ensure_trigram_fts(conn)
    conn.commit()
    conn.close()

    r = _client().get("/readyz")
    assert r.status_code == 200, f"readyz 应全绿，实得 {r.status_code}: {r.json()}"
    body = r.json()
    assert body["ready"] is True
    assert body["failed"] == []
    for check in ("facts_db", "text_fts_db", "data_dir_writable", "schema_version"):
        assert check in body["checks"], f"readyz 缺廉价检查项 {check}"
        assert body["checks"][check] is True


def test_readyz_503_names_failed_check_without_leaking_paths(gate_on, monkeypatch):
    """负向对照：facts_db 指到不存在的路径 → 503，failed 记名，
    但响应体绝不外泄任何文件系统路径（匿名调用方拿不到侦察素材）。"""
    import ducky.hot.health as health_mod

    bogus = "/nonexistent-v20_4-split/facts.db"
    monkeypatch.setattr(health_mod, "FACTS_DB", bogus)
    r = _client().get("/readyz")
    assert r.status_code == 503
    body = r.json()
    assert body["ready"] is False
    assert "facts_db" in body["failed"]
    assert body["checks"]["facts_db"] is False
    assert bogus not in r.text
    assert "/nonexistent-v20_4-split" not in r.text


# ──────────────────────────────────────────────
# 3. /diagnostics：完整探针，门禁启用时匿名 401
# ──────────────────────────────────────────────
def test_diagnostics_anonymous_401_when_gate_enabled(gate_on):
    r = _client().get("/diagnostics")
    assert r.status_code == 401


def test_diagnostics_full_probe_with_token(gate_on):
    r = _client().get("/diagnostics", headers={"Authorization": f"Bearer {gate_on}"})
    assert r.status_code == 200
    body = r.json()
    # 完整探针的深度字段：匿名 /health 公开视图里绝不会有 base_dir
    assert "probes" in body
    assert body["probes"]["runtime_paths"]["base_dir"]
    assert "health_status" in body


def test_diagnostics_open_when_gate_disabled(monkeypatch):
    """门禁未启用 = 不存在需要防的侦察者，藏字段只伤自己人（与 /health 同一原则）。"""
    import ducky.hot.health as health_mod

    monkeypatch.delenv("AIDUMEM_API_TOKEN", raising=False)
    monkeypatch.setattr(health_mod, "_auth_gate_enabled", lambda: False)
    r = _client().get("/diagnostics")
    assert r.status_code == 200
    assert "probes" in r.json()


# ──────────────────────────────────────────────
# 4. /health 兼容：拆分不许动既有契约
# ──────────────────────────────────────────────
def test_health_anonymous_still_gets_redacted_public_view(gate_on):
    r = _client().get("/health")
    assert r.status_code == 200
    body = r.json()
    assert "_redacted" in body["probes"]
    assert "data_dir_writable" in body["probes"]["runtime_paths"]
    assert "base_dir" not in body["probes"]["runtime_paths"]


# ──────────────────────────────────────────────
# 5. api_server 中间件层契约
# ──────────────────────────────────────────────
def test_middleware_public_path_contract():
    """探活/就绪要对负载均衡器可达（公开路径集）；深度诊断必须留在门禁后。"""
    from api_server import _ALWAYS_PUBLIC_PATHS

    assert "/livez" in _ALWAYS_PUBLIC_PATHS
    assert "/readyz" in _ALWAYS_PUBLIC_PATHS
    assert "/diagnostics" not in _ALWAYS_PUBLIC_PATHS
