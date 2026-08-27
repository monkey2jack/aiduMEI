"""
tests/test_v20_observability.py — v20 P0-4 可观测性契约测试
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
覆盖：
  1. rerank 三态遥测（not_configured / error / empty / ok / skipped_empty_input）
  2. rerank 非 200 抛错且不回显响应体（凭据错误详情不落日志）
  3. /usage 账本 rerank 分桶（calls / failures / latency / providers）
  4. scoring 把 rerank_applied 回写进遥测
  5. /search 响应携带 _recall_path 与 _rerank（SearchResponse extra="allow" 实链路验证）
  6. /search 错误路径 detail 不被 response_model 剥掉
  7. /reload 语义：reset_memory_singleton 清空 rerank 配置缓存
  8. /health 暴露 rerank_configured 探针；jina/cohere 无 base_url 也算已配置
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import ducky.mem0_runtime as mr  # noqa: E402


# ──────────────────────────────────────────────
# 公共夹具：隔离用量账本 + 每测重置遥测
# ──────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _isolate_usage(monkeypatch):
    """用量账本写进临时文件，绝不污染真实 llm_usage.json。"""
    tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    tmp.close()
    monkeypatch.setattr(mr, "USAGE_FILE", tmp.name)
    monkeypatch.setattr(mr, "_llm_usage", {})
    mr.reset_rerank_telemetry()
    yield
    try:
        os.unlink(tmp.name)
    except OSError:
        pass


def _set_rerank_cfg(monkeypatch, **kw):
    cfg = {"provider": "siliconflow", "model": "test-model",
           "base_url": "", "api_key": ""}
    cfg.update(kw)
    monkeypatch.setattr(mr, "_RERANK_CONFIG_CACHE", cfg)
    return cfg


def _usage_rerank_bucket():
    usage = mr.get_llm_usage()
    if not usage:
        return {}
    today = next(iter(usage))
    return usage[today].get("rerank", {})


# ──────────────────────────────────────────────
# 1. rerank 遥测三态
# ──────────────────────────────────────────────
def test_rerank_telemetry_not_configured(monkeypatch):
    _set_rerank_cfg(monkeypatch, api_key="", base_url="")
    out = mr.rerank("查询", ["文档一"])
    assert out == []
    telem = mr.last_rerank_telemetry()
    assert telem is not None
    assert telem["status"] == "not_configured"
    # 未配置不算一次外呼，账本不该出现 rerank 桶
    assert _usage_rerank_bucket() == {}


def test_rerank_telemetry_skipped_empty_input(monkeypatch):
    _set_rerank_cfg(monkeypatch, api_key="k", base_url="http://127.0.0.1:1")
    out = mr.rerank("查询", [])
    assert out == []
    assert mr.last_rerank_telemetry()["status"] == "skipped_empty_input"


def test_rerank_telemetry_error_and_usage_failure(monkeypatch):
    _set_rerank_cfg(monkeypatch, provider="fakeprov", api_key="k",
                    base_url="http://127.0.0.1:1")

    def _boom(cfg, query, documents, top_n):
        raise RuntimeError("rerank HTTP 401")

    monkeypatch.setitem(mr.RERANK_PROVIDERS, "fakeprov", _boom)
    out = mr.rerank("查询", ["文档一", "文档二"])
    assert out == []
    telem = mr.last_rerank_telemetry()
    assert telem["status"] == "error"
    assert "rerank HTTP 401" in telem["error"]
    assert telem["provider"] == "fakeprov"
    assert telem["latency_ms"] >= 0.0
    bucket = _usage_rerank_bucket()
    assert bucket["calls"] == 1
    assert bucket["failures"] == 1
    assert bucket["providers"]["fakeprov"] == {"calls": 1, "failures": 1}


def test_rerank_telemetry_empty_result(monkeypatch):
    _set_rerank_cfg(monkeypatch, provider="fakeprov", api_key="k",
                    base_url="http://127.0.0.1:1")
    monkeypatch.setitem(mr.RERANK_PROVIDERS, "fakeprov",
                        lambda cfg, q, docs, n: [])
    out = mr.rerank("查询", ["文档一"])
    assert out == []
    telem = mr.last_rerank_telemetry()
    assert telem["status"] == "empty"
    assert telem["returned"] == 0
    bucket = _usage_rerank_bucket()
    assert bucket["calls"] == 1
    assert bucket["failures"] == 0


def test_rerank_telemetry_ok(monkeypatch):
    _set_rerank_cfg(monkeypatch, provider="fakeprov", api_key="k",
                    base_url="http://127.0.0.1:1")
    monkeypatch.setitem(mr.RERANK_PROVIDERS, "fakeprov",
                        lambda cfg, q, docs, n: [{"index": 0, "relevance_score": 0.93}])
    out = mr.rerank("查询", ["文档一"])
    assert out == [{"index": 0, "relevance_score": 0.93}]
    telem = mr.last_rerank_telemetry()
    assert telem["status"] == "ok"
    assert telem["returned"] == 1
    assert telem["provider"] == "fakeprov"
    assert "latency_ms" in telem
    bucket = _usage_rerank_bucket()
    assert bucket["calls"] == 1
    assert bucket["failures"] == 0
    assert bucket["latency_ms_sum"] >= 0.0
    assert bucket["providers"]["fakeprov"]["calls"] == 1


def test_rerank_reset_kills_cross_request_residue(monkeypatch):
    """线程复用时上一请求的遥测残留必须被 reset 清掉。"""
    _set_rerank_cfg(monkeypatch, api_key="", base_url="")
    mr.rerank("查询", ["文档一"])
    assert mr.last_rerank_telemetry() is not None
    mr.reset_rerank_telemetry()
    assert mr.last_rerank_telemetry() is None


# ──────────────────────────────────────────────
# 2. 非 200 抛错且不回显响应体
# ──────────────────────────────────────────────
def test_rerank_http_error_raises_without_body():
    class _FakeResp:
        status_code = 401
        text = 'invalid api key "sk-SHOULD-NEVER-LEAK"'

    with pytest.raises(RuntimeError) as ei:
        mr._rerank_http_error(_FakeResp())
    assert "401" in str(ei.value)
    # 负向对照：响应体可能回显凭据错误详情，绝不许进异常消息
    assert "SHOULD-NEVER-LEAK" not in str(ei.value)


def test_rerank_http_error_passes_on_200():
    class _FakeResp:
        status_code = 200
        text = "{}"

    assert mr._rerank_http_error(_FakeResp()) is None


# ──────────────────────────────────────────────
# 3. scoring 回写 applied 标志
# ──────────────────────────────────────────────
def _scoring_candidates():
    return [
        {"id": "m1", "memory": "用户喜欢喝茶", "score": 0.5},
        {"id": "m2", "memory": "今天天气不错", "score": 0.4},
    ]


def test_scoring_writes_applied_true(monkeypatch):
    import ducky.scoring as scoring
    monkeypatch.setattr(scoring, "get_batch_salience_records", lambda ids: {})
    _set_rerank_cfg(monkeypatch, provider="fakeprov", api_key="k",
                    base_url="http://127.0.0.1:1")
    monkeypatch.setitem(mr.RERANK_PROVIDERS, "fakeprov",
                        lambda cfg, q, docs, n: [{"index": 0, "relevance_score": 0.9}])
    mr.reset_rerank_telemetry()
    out = scoring.score_and_rank_candidates("喜欢什么", _scoring_candidates(), limit=2)
    assert out, "打分结果不该为空"
    telem = mr.last_rerank_telemetry()
    assert telem is not None
    assert telem["applied"] is True
    assert telem["status"] == "ok"


def test_scoring_writes_applied_false_when_not_configured(monkeypatch):
    import ducky.scoring as scoring
    monkeypatch.setattr(scoring, "get_batch_salience_records", lambda ids: {})
    _set_rerank_cfg(monkeypatch, api_key="", base_url="")
    mr.reset_rerank_telemetry()
    out = scoring.score_and_rank_candidates("喜欢什么", _scoring_candidates(), limit=2)
    assert out
    telem = mr.last_rerank_telemetry()
    assert telem is not None
    assert telem["applied"] is False
    assert telem["status"] == "not_configured"


# ──────────────────────────────────────────────
# 4. /search 响应契约（_recall_path / _rerank / detail 透传）
# ──────────────────────────────────────────────
def _build_search_client(monkeypatch, hybrid_fn):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import ducky.hot.search as hs
    import ducky.memory_workspace as mw

    class _FakeMem:
        def search(self, *a, **kw):
            raise RuntimeError("mem0 down")

    monkeypatch.setattr(hs, "get_memory", lambda: _FakeMem())
    monkeypatch.setattr(hs, "ensure_bank_registered", lambda scope, **kw: None)
    monkeypatch.setattr(hs, "boost_salience_for_results", lambda results: None)
    monkeypatch.setattr(hs, "lazy_import_hybrid", lambda: hybrid_fn)
    monkeypatch.setattr(mw, "ws_lookup", lambda uid, q, bank_id="default": [])
    monkeypatch.setattr(mw, "ws_feed_from_results",
                        lambda uid, results, bank_id="default": None)

    app = FastAPI()
    hs.register_search_routes(app)
    return TestClient(app)


def test_search_response_carries_recall_path_and_rerank(monkeypatch):
    hits = [{"id": "m1", "memory": "用户喜欢喝茶", "score": 0.8}]

    def _hybrid(mem, query, uid, limit, before="", after="", bank_id="default"):
        return list(hits)

    client = _build_search_client(monkeypatch, _hybrid)
    r = client.post("/search", json={"query": "喜欢什么", "user_id": "u_obs"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    # 这两个下划线字段此前会被严格 response_model 静默剥掉——
    # 本断言同时是 SearchResponse extra="allow" 修复的实链路验证。
    assert body["_recall_path"] == "hybrid"
    assert "_rerank" in body
    assert body["_rerank"]["status"] in (
        "not_invoked", "not_configured", "ok", "empty", "error", "skipped_empty_input",
    )
    assert body["results"][0]["id"] == "m1"


def test_search_degraded_path_marked(monkeypatch):
    def _hybrid(mem, query, uid, limit, before="", after="", bank_id="default"):
        raise RuntimeError("hybrid broken")

    client = _build_search_client(monkeypatch, _hybrid)
    # FakeMem.search 也抛错 → 整体走 error 路径；detail 必须透传
    r = client.post("/search", json={"query": "喜欢什么", "user_id": "u_obs"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "error"
    assert body["results"] == []
    assert "mem0 down" in body.get("detail", "")


def test_search_degraded_recall_path_when_mem0_survives(monkeypatch):
    """hybrid 挂、mem0 裸搜活着 → _recall_path 必须标成 mem0_degraded。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import ducky.hot.search as hs
    import ducky.memory_workspace as mw

    class _FakeMem:
        def search(self, *a, **kw):
            return {"results": [{"id": "m9", "memory": "裸搜结果", "score": 0.5,
                                 "metadata": {}}]}

    def _hybrid(mem, query, uid, limit, before="", after="", bank_id="default"):
        raise RuntimeError("hybrid broken")

    monkeypatch.setattr(hs, "get_memory", lambda: _FakeMem())
    monkeypatch.setattr(hs, "ensure_bank_registered", lambda scope, **kw: None)
    monkeypatch.setattr(hs, "boost_salience_for_results", lambda results: None)
    monkeypatch.setattr(hs, "lazy_import_hybrid", lambda: _hybrid)
    monkeypatch.setattr(hs, "vector_scope_filters", lambda uid, bank, **kw: {"user_id": uid})
    monkeypatch.setattr(hs, "vector_item_in_bank", lambda item, bank, **kw: True)
    monkeypatch.setattr(mw, "ws_lookup", lambda uid, q, bank_id="default": [])
    monkeypatch.setattr(mw, "ws_feed_from_results",
                        lambda uid, results, bank_id="default": None)

    app = FastAPI()
    hs.register_search_routes(app)
    client = TestClient(app)
    r = client.post("/search", json={"query": "喜欢什么", "user_id": "u_obs"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["_recall_path"] == "mem0_degraded"


def test_search_workspace_hit_recall_path(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import ducky.hot.search as hs
    import ducky.memory_workspace as mw

    monkeypatch.setattr(hs, "get_memory", lambda: object())
    monkeypatch.setattr(hs, "ensure_bank_registered", lambda scope, **kw: None)
    monkeypatch.setattr(hs, "boost_salience_for_results", lambda results: None)
    monkeypatch.setattr(
        mw, "ws_lookup",
        lambda uid, q, bank_id="default": [{"id": "w1", "memory": "工作台命中"}],
    )

    app = FastAPI()
    hs.register_search_routes(app)
    client = TestClient(app)
    r = client.post("/search", json={"query": "喜欢什么", "user_id": "u_obs"})
    assert r.status_code == 200
    body = r.json()
    assert body["_workspace_hit"] is True
    assert body["_recall_path"] == "workspace"
    assert body["_rerank"]["status"] == "not_invoked"


# ──────────────────────────────────────────────
# 5. /reload 清空 rerank 配置缓存
# ──────────────────────────────────────────────
def test_reset_memory_singleton_clears_rerank_cache(monkeypatch):
    monkeypatch.setattr(mr, "_RERANK_CONFIG_CACHE",
                        {"provider": "stale", "api_key": "old"})
    mr.reset_memory_singleton()
    assert mr._RERANK_CONFIG_CACHE is None


# ──────────────────────────────────────────────
# 6. rerank_config_status（/health 探针的数据源）
# ──────────────────────────────────────────────
def test_rerank_config_status_reports_provider_without_secrets(monkeypatch):
    _set_rerank_cfg(monkeypatch, provider="siliconflow",
                    api_key="sk-SECRET", base_url="http://internal:9999")
    st = mr.rerank_config_status()
    assert st["configured"] is True
    assert st["provider"] == "siliconflow"
    # 负向对照：状态里绝不许出现 key 或 base_url 内容
    flat = str(st)
    assert "sk-SECRET" not in flat
    assert "internal:9999" not in flat


def test_rerank_config_status_jina_without_base_url(monkeypatch):
    """jina/cohere 官方端点写死，无 base_url 也算已配置——与 rerank() 同一条规则。"""
    _set_rerank_cfg(monkeypatch, provider="jina", api_key="k", base_url="")
    assert mr.rerank_config_status()["configured"] is True
    _set_rerank_cfg(monkeypatch, provider="siliconflow", api_key="k", base_url="")
    assert mr.rerank_config_status()["configured"] is False


def test_health_probes_include_rerank_configured(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ducky.hot.health import register_health_routes

    _set_rerank_cfg(monkeypatch, provider="siliconflow",
                    api_key="k", base_url="http://127.0.0.1:1")
    app = FastAPI()
    register_health_routes(app)
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    probes = r.json()["probes"]
    assert probes["rerank_configured"] is True
    assert probes["rerank_provider"] == "siliconflow"


def _health_probes():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ducky.hot.health import register_health_routes

    app = FastAPI()
    register_health_routes(app)
    r = TestClient(app).get("/health")
    assert r.status_code == 200
    return r.json()["probes"]


def test_health_exposes_schema_version_from_disk_not_from_the_constant(monkeypatch):
    """v20 回归清单要求 schema_version 可读。

    关键在于它必须来自磁盘上的 ``PRAGMA user_version``。只回显代码常量的话，
    库还停在旧版本时 /health 照样报新版本号 —— 那是标准的假绿灯。
    """
    from ducky.schema_bootstrap import CURRENT_SCHEMA_VERSION
    from ducky.utils import get_facts_conn

    conn = get_facts_conn()
    on_disk = int(conn.execute("PRAGMA user_version").fetchone()[0])
    conn.close()

    probes = _health_probes()
    assert "schema_version" in probes, "/health 里读不到 schema_version"
    assert probes["schema_version"] == on_disk, \
        f"schema_version 不是磁盘真值: {probes['schema_version']} != {on_disk}"
    assert probes["schema_version_expected"] == int(CURRENT_SCHEMA_VERSION)
    assert probes["schema_version_ok"] is (on_disk == int(CURRENT_SCHEMA_VERSION))


def test_health_schema_version_mismatch_is_not_reported_green(monkeypatch):
    """负向对照：把代码期望值抬高一档，schema_version_ok 必须翻成 False。

    如果这条还报 True，说明上面那条测试是空的 —— 它只是碰巧两边相等。
    """
    import ducky.schema_bootstrap as sb

    monkeypatch.setattr(sb, "CURRENT_SCHEMA_VERSION", int(sb.CURRENT_SCHEMA_VERSION) + 7)
    probes = _health_probes()
    assert probes["schema_version_ok"] is False, "版本对不上却报绿灯"
    assert probes["schema_version"] != probes["schema_version_expected"]
