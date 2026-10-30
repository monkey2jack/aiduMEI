"""f0.7 设置页体检:模型配置真能用、测试连接是真测、vLLM 能接上。

覆盖用户实测出来的几处断点:
- UI 首次配 rerank 时 provider 写成 null → 每次检索 rerank 静默失败(AttributeError);
- 「测试连接」只看 /health,模型填错也显示正常;
- 局域网 / tailnet 上的 vLLM 地址一律被拦,没有放行办法。
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ducky.mem0_runtime as runtime
import ducky.routes_config as routes

REPO = Path(__file__).resolve().parents[1]


class _FakeVllm(BaseHTTPRequestHandler):
    models = ("qwen-local", "bge-local", "rerank-local")

    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self._send({"data": [{"id": m} for m in self.models]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if body.get("model") not in self.models:
            return self._send({"error": {"message": f"model {body.get('model')} not found"}}, 404)
        if self.path.endswith("/chat/completions"):
            return self._send({"model": body["model"], "choices": [{"message": {"content": "p"}}]})
        if self.path.endswith("/embeddings"):
            return self._send({"data": [{"embedding": [0.1] * 8}]})
        if self.path.endswith("/rerank"):
            return self._send({"results": [{"index": i, "relevance_score": 0.5}
                                           for i, _ in enumerate(body.get("documents", []))]})
        return self._send({}, 404)


@pytest.fixture
def vllm():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeVllm)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "models.json"
    path.write_text("{}")
    monkeypatch.setenv("AIDUMEM_CONFIG_FILE", str(path))
    monkeypatch.setattr(runtime, "MEM0_CONFIG", str(path))
    monkeypatch.setattr(routes, "_CFG_PATH", str(path))
    monkeypatch.setenv("AIDUMEI_ENGINE_MODE", "auto")
    for k in ("AIDUMEI_RERANKER_API_KEY", "AIDUMEI_LLM_API_KEY", "AIDUMEI_EMBEDDER_API_KEY",
              "AIDUMEI_DECISION_API_KEY", "AIDUMEI_MODEL_ENDPOINT_ALLOW"):
        monkeypatch.delenv(k, raising=False)
    from ducky.federation import routes as federation
    monkeypatch.setattr(federation, "_is_admin_caller", lambda caller: caller == "test-admin")
    app = FastAPI()
    routes.register_config_routes(app)
    return path, TestClient(app)


def _put(client, section, body):
    return client.put(f"/config/{section}?caller=test-admin", json=body)


def test_ui_first_rerank_save_gets_working_provider(cfg):
    path, client = cfg
    # UI 编辑表单早先不带 provider —— 曾写成 null,检索时 None.lower() 静默失败
    r = _put(client, "rerank", {"enabled": True, "config": {
        "model": "rank-a", "openai_base_url": "https://example.invalid/v1", "api_key": "k"}})
    assert r.status_code == 200
    assert json.loads(path.read_text())["rerank"]["provider"] == "openai_compatible"
    # 已经被旧版写成 null 的存量配置,读取时也回落默认
    raw = json.loads(path.read_text())
    raw["rerank"]["provider"] = None
    path.write_text(json.dumps(raw))
    assert runtime._load_rerank_config()["provider"] == runtime.DEFAULT_RERANK_PROVIDER


def test_llm_save_takes_effect_for_call_llm_without_reload(cfg, monkeypatch):
    """PUT /config/llm 之后，call_llm 读到的就是新端点与新 key —— 不靠客户端再打 /reload。

    get_llm_config 是进程级缓存；此前只有 /reload 才清。控制台保存后会补打 /reload，
    只走 API 改配置的调用方则一直打旧 endpoint/key，直到重启。
    """
    from ducky import llm_client
    path, client = cfg
    monkeypatch.setattr(llm_client, "MEM0_CONFIG", str(path))
    path.write_text(json.dumps({"llm": {"provider": "openai", "config": {
        "model": "old-model", "openai_base_url": "https://old.example.invalid/v1", "api_key": "old-key"}}}))
    llm_client.invalidate_llm_config_cache()
    assert llm_client.get_llm_config()["base_url"] == "https://old.example.invalid/v1"

    r = _put(client, "llm", {"provider": "openai", "config": {
        "model": "new-model", "openai_base_url": "https://new.example.invalid/v1", "api_key": "new-key"}})
    assert r.status_code == 200, r.text
    now = llm_client.get_llm_config()
    assert (now["model"], now["base_url"], now["api_key"]) == (
        "new-model", "https://new.example.invalid/v1", "new-key")
    llm_client.invalidate_llm_config_cache()


def test_rerank_provider_choice_is_validated(cfg):
    _, client = cfg
    assert _put(client, "rerank", {"provider": "bogus", "config": {}}).status_code == 400
    assert _put(client, "rerank", {"provider": "vllm", "config": {}}).status_code == 200


def test_lan_vllm_needs_explicit_allow_and_metadata_never(cfg, monkeypatch):
    _, client = cfg
    lan = {"provider": "openai", "config": {"openai_base_url": "http://192.168.1.50:8000/v1", "api_key": "EMPTY"}}
    assert _put(client, "embedder", lan).status_code == 400
    monkeypatch.setenv("AIDUMEI_MODEL_ENDPOINT_ALLOW", "192.168.1.50, 169.254.169.254")
    assert _put(client, "embedder", lan).status_code == 200
    meta = {"provider": "openai", "config": {"openai_base_url": "http://169.254.169.254/v1", "api_key": "EMPTY"}}
    assert _put(client, "embedder", meta).status_code == 400, "云元数据地址不能被名单放行"


def test_config_test_really_calls_each_model(cfg, vllm):
    _, client = cfg
    for sec, model in (("llm", "qwen-local"), ("embedder", "bge-local")):
        assert _put(client, sec, {"provider": "openai", "config": {
            "model": model, "openai_base_url": vllm, "api_key": "EMPTY"}}).status_code == 200
    assert _put(client, "rerank", {"enabled": True, "provider": "vllm", "config": {
        "model": "rerank-local", "openai_base_url": vllm, "api_key": "EMPTY"}}).status_code == 200
    r = client.post("/config/test?caller=test-admin", json={}).json()
    by = {x["section"]: x for x in r["results"]}
    assert by["llm"]["ok"] and by["embedder"]["ok"] and by["rerank"]["ok"]
    assert "8 维" in by["embedder"]["detail"]
    assert by["vision"]["status"] == "not_configured" and "共用" in by["vision"]["detail"]
    assert by["decision"]["ok"] is False and by["decision"]["status"] == "not_configured"
    assert r["all_ok"] is True
    assert "EMPTY" not in json.dumps(r), "测试结果不许回显密钥"


def test_config_test_reports_wrong_model_and_dead_endpoint(cfg, vllm):
    _, client = cfg
    _put(client, "llm", {"provider": "openai", "config": {
        "model": "typo-model", "openai_base_url": vllm, "api_key": "EMPTY"}})
    _put(client, "embedder", {"provider": "openai", "config": {
        "model": "bge-local", "openai_base_url": "http://127.0.0.1:9/v1", "api_key": "EMPTY"}})
    by = {x["section"]: x for x in client.post("/config/test?caller=test-admin", json={}).json()["results"]}
    assert not by["llm"]["ok"] and "可用模型" in by["llm"]["detail"] and "qwen-local" in by["llm"]["detail"]
    assert not by["embedder"]["ok"] and "连不上" in by["embedder"]["detail"]


def test_config_test_respects_local_tier_and_admin(cfg, vllm, monkeypatch):
    _, client = cfg
    _put(client, "llm", {"provider": "openai", "config": {
        "model": "qwen-local", "openai_base_url": vllm, "api_key": "EMPTY"}})
    monkeypatch.setenv("AIDUMEI_ENGINE_MODE", "local")
    by = {x["section"]: x for x in client.post("/config/test?caller=test-admin", json={}).json()["results"]}
    assert by["llm"]["status"] == "blocked_by_engine_mode"
    from ducky.route_policy import POLICIES
    assert POLICIES[("post", "/config/test")].resource == "admin", "测试连接与读写配置同级鉴权"


def test_decision_card_shows_provider_defaults_not_fake_values(cfg):
    _, client = cfg
    view = client.get("/config?caller=test-admin").json()["decision"]
    assert view["config"]["model"] is None, "没写就是没写,不冒充"
    assert view["defaults"]["model"] and view["defaults"]["openai_base_url"].startswith("https://")


def test_settings_panel_wires_real_test_vllm_preset_and_rerank_provider():
    js = (REPO / "frontend" / "js" / "panels.js").read_text(encoding="utf-8")
    assert "API.post('/config/test'" in js, "测试连接必须真测模型,不是只看 /health"
    assert "edRerProvider" in js and "provider: rp" in js
    assert "edVllm" in js and "AIDUMEI_MODEL_ENDPOINT_ALLOW" in js
    from ducky.routes_registry import UI_ROUTES
    assert ("POST", "/config/test") in UI_ROUTES
