"""v20.4.0-alpha · P1-7：LLM/Embedder/Reranker 密钥环境变量覆盖。

背景（六方外审 GLM M-1 残余实锤）：三类密钥此前只能明文落在
mem0_config_local.json 里 —— 数据目录被共享卷/备份带走时密钥跟着扩散。
本轮给三条 env 覆盖通道：env 非空 → 优先于 JSON；未设 → 维持 JSON 与
密钥文件回退链原样（老部署零变化）。example 文件的 api_key 改空串，
密钥不再有任何「样例值」形态可抄。

前缀按 env_registry 既定学说：新变量一律 `AIDUMEI_`（当前前缀），
`AIDUMEM_` 是冻结兼容旧前缀 —— 台账见 tests/test_v20_4_env_prefix.py。
"""
import json

import pytest


@pytest.fixture()
def cfg_with_keys():
    return {
        "llm": {"config": {"model": "m", "api_key": "json-llm-key"}},
        "embedder": {"config": {"model": "e", "api_key": "json-emb-key"}},
        "rerank": {"config": {"model": "r", "api_key": "json-rerank-key"}},
    }


class TestEnvOverridesMem0Config:
    def test_env_beats_json_llm(self, cfg_with_keys, monkeypatch):
        from ducky.mem0_runtime import _resolve_api_keys
        monkeypatch.setenv("AIDUMEI_LLM_API_KEY", "env-llm-key")
        out = _resolve_api_keys(cfg_with_keys)
        assert out["llm"]["config"]["api_key"] == "env-llm-key"

    def test_env_beats_json_embedder(self, cfg_with_keys, monkeypatch):
        from ducky.mem0_runtime import _resolve_api_keys
        monkeypatch.setenv("AIDUMEI_EMBEDDER_API_KEY", "env-emb-key")
        out = _resolve_api_keys(cfg_with_keys)
        assert out["embedder"]["config"]["api_key"] == "env-emb-key"

    def test_env_beats_json_reranker(self, cfg_with_keys, monkeypatch):
        from ducky.mem0_runtime import _resolve_api_keys
        monkeypatch.setenv("AIDUMEI_RERANKER_API_KEY", "env-rerank-key")
        out = _resolve_api_keys(cfg_with_keys)
        assert out["rerank"]["config"]["api_key"] == "env-rerank-key"

    def test_no_env_keeps_json(self, cfg_with_keys, monkeypatch):
        from ducky.mem0_runtime import _resolve_api_keys
        for v in ("AIDUMEI_LLM_API_KEY", "AIDUMEI_EMBEDDER_API_KEY", "AIDUMEI_RERANKER_API_KEY"):
            monkeypatch.delenv(v, raising=False)
        out = _resolve_api_keys(cfg_with_keys)
        assert out["llm"]["config"]["api_key"] == "json-llm-key"
        assert out["embedder"]["config"]["api_key"] == "json-emb-key"
        assert out["rerank"]["config"]["api_key"] == "json-rerank-key"

    def test_env_beats_placeholder(self, monkeypatch):
        """JSON 还是占位符时 env 也必须生效（首跑场景：先配 env 后补 JSON）。"""
        from ducky.mem0_runtime import _resolve_api_keys
        cfg = {"llm": {"config": {"api_key": "__LLM_KEY__"}}}
        monkeypatch.setenv("AIDUMEI_LLM_API_KEY", "env-llm-key")
        out = _resolve_api_keys(cfg)
        assert out["llm"]["config"]["api_key"] == "env-llm-key"


class TestEnvOverridesLlmClient:
    """llm_client 是另一条独立读取链（get_llm_config），必须同权覆盖。"""

    def test_env_beats_json_in_llm_client(self, tmp_path, monkeypatch):
        import ducky.llm_client as lc
        cfg_file = tmp_path / "mem0_config_local.json"
        cfg_file.write_text(json.dumps(
            {"llm": {"config": {"model": "m", "api_key": "json-llm-key"}}}),
            encoding="utf-8")
        monkeypatch.setattr(lc, "MEM0_CONFIG", str(cfg_file))
        monkeypatch.setattr(lc, "_config_cache", None)
        monkeypatch.setenv("AIDUMEI_LLM_API_KEY", "env-llm-key")
        assert lc.get_llm_config()["api_key"] == "env-llm-key"

    def test_no_env_keeps_json_in_llm_client(self, tmp_path, monkeypatch):
        import ducky.llm_client as lc
        cfg_file = tmp_path / "mem0_config_local.json"
        cfg_file.write_text(json.dumps(
            {"llm": {"config": {"model": "m", "api_key": "json-llm-key"}}}),
            encoding="utf-8")
        monkeypatch.setattr(lc, "MEM0_CONFIG", str(cfg_file))
        monkeypatch.setattr(lc, "_config_cache", None)
        monkeypatch.delenv("AIDUMEI_LLM_API_KEY", raising=False)
        assert lc.get_llm_config()["api_key"] == "json-llm-key"


class TestExampleAndRegistry:
    def test_example_file_api_keys_are_empty(self):
        """example 的 api_key 必须空串：占位样例值本身就是诱导误提交的形态。"""
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "mem0_config_local.json.example"), encoding="utf-8") as f:
            example = json.load(f)
        for section in ("llm", "embedder"):
            assert example[section]["config"]["api_key"] == "", \
                f"{section}.config.api_key 应为空串（密钥走 AIDUMEI_*_API_KEY 环境变量）"

    def test_new_env_vars_are_registered(self):
        from ducky.env_registry import KNOWN_ENV_VARS
        for v in ("AIDUMEI_LLM_API_KEY", "AIDUMEI_EMBEDDER_API_KEY", "AIDUMEI_RERANKER_API_KEY"):
            assert v in KNOWN_ENV_VARS, f"{v} 未登记进 KNOWN_ENV_VARS"
