"""
tests/test_v20_5_1_path_consistency.py — T-10：mem0 向量/历史库路径 vs DATA_DIR 一致性探针
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
立案背景（docs/DEPLOY_DOCKHOLD.md 记录在案的坑）：改 AIDUMEM_DATA_DIR 只移
SQLite 面，mem0 配置里 Qdrant 的 path 与 history_db_path 不被重写；两者脱钩时
向量库写进错误位置且无症状。runtime_paths 探针此前只查 data_dir_writable
（能写），本组钉「写对地方」——ducky.mem0_runtime.vector_path_consistency
及 /health 的 probes.runtime_paths.path_consistency 挂载。

判据（先红后绿：探针函数不存在时本组全部 import 即红）：
  1. 两项都在 DATA_DIR 之下            → status=ok，无 warning
  2. qdrant path 在 DATA_DIR 之外      → warning，点名 vector_store.config.path
  3. 模板相对路径 ./data/... 而 DATA_DIR 被搬走（Dockhold 坑原形）→ 两项同警
  4. 仅 history_db_path 在外           → warning，只点名 history_db_path
  5. 配置文件缺失                      → status=skipped（零配置首跑不炸、不误警）
  6. 远端 Qdrant（host、无本地 path）  → 不在射程，不报警
  7. 字段缺省                          → 按 mem0 内建默认判定（默认在 DATA_DIR 外 → 警）
  8. /health 集成                      → 授权面见 path_consistency；匿名面只留
     data_dir_writable 布尔，新字段与绝对路径不进匿名载荷（_public_view 白名单）
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import ducky.utils as utils  # noqa: E402


def _base_cfg(qdrant_cfg: dict, history_db_path: str | None = None) -> dict:
    cfg = {
        "llm": {"provider": "openai", "config": {"model": "m", "api_key": "k"}},
        "embedder": {"provider": "openai",
                     "config": {"model": "e", "api_key": "k", "embedding_dims": 8}},
        "vector_store": {"provider": "qdrant", "config": qdrant_cfg},
    }
    if history_db_path is not None:
        cfg["history_db_path"] = history_db_path
    return cfg


def _write_config(tmp_path, monkeypatch, cfg: dict) -> None:
    path = tmp_path / "mem0_config_local.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("AIDUMEM_CONFIG_FILE", str(path))


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """把 ducky.utils.DATA_DIR 钉到本用例的临时目录（探针读的是生效值，不是 env 原文）。"""
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(utils, "DATA_DIR", str(d))
    return d


def _probe():
    from ducky.mem0_runtime import vector_path_consistency
    return vector_path_consistency()


# ── 1. 一致 → 不报警 ────────────────────────────────────────────────
def test_consistent_paths_no_warning(tmp_path, monkeypatch, data_dir):
    _write_config(tmp_path, monkeypatch, _base_cfg(
        {"collection_name": "t", "path": str(data_dir / "qdrant"), "embedding_model_dims": 8},
        history_db_path=str(data_dir / "history.db"),
    ))
    out = _probe()
    assert out["status"] == "ok"
    assert out["mismatched"] == []
    assert out["warning"] is None
    assert out["checks"]["vector_store.config.path"]["under_data_dir"] is True
    assert out["checks"]["history_db_path"]["under_data_dir"] is True


# ── 2. qdrant path 在外 → 报警并点名 ────────────────────────────────
def test_qdrant_path_outside_data_dir_warns(tmp_path, monkeypatch, data_dir):
    stray = tmp_path / "stray"
    _write_config(tmp_path, monkeypatch, _base_cfg(
        {"collection_name": "t", "path": str(stray / "qdrant"), "embedding_model_dims": 8},
        history_db_path=str(data_dir / "history.db"),
    ))
    out = _probe()
    assert out["status"] == "warning"
    assert out["mismatched"] == ["vector_store.config.path"]
    assert "vector_store.config.path" in out["warning"]
    assert "不在 AIDUMEM_DATA_DIR" in out["warning"]


# ── 3. Dockhold 坑原形：模板 ./data/... + DATA_DIR 被搬走 → 两项同警 ──
def test_relative_template_paths_flagged_when_data_dir_moved(tmp_path, monkeypatch, data_dir):
    """相对路径按进程 CWD 解析（运行时真实语义），不按 DATA_DIR——
    套件 CWD 恒为仓库根，而 tmp_path 在仓库外，故此判定与机器无关。"""
    _write_config(tmp_path, monkeypatch, _base_cfg(
        {"collection_name": "t", "path": "./data/qdrant", "embedding_model_dims": 8},
        history_db_path="./data/history.db",
    ))
    out = _probe()
    assert out["status"] == "warning"
    assert set(out["mismatched"]) == {"vector_store.config.path", "history_db_path"}


# ── 4. 仅 history_db_path 在外 ─────────────────────────────────────
def test_history_db_path_outside_warns(tmp_path, monkeypatch, data_dir):
    _write_config(tmp_path, monkeypatch, _base_cfg(
        {"collection_name": "t", "path": str(data_dir / "qdrant"), "embedding_model_dims": 8},
        history_db_path=str(tmp_path / "elsewhere" / "history.db"),
    ))
    out = _probe()
    assert out["status"] == "warning"
    assert out["mismatched"] == ["history_db_path"]


# ── 5. 配置文件缺失 → skipped，不炸不误警 ──────────────────────────
def test_missing_config_file_is_skipped(tmp_path, monkeypatch, data_dir):
    monkeypatch.setenv("AIDUMEM_CONFIG_FILE", str(tmp_path / "no_such_config.json"))
    out = _probe()
    assert out["status"] == "skipped"
    assert out["reason"] == "config_file_missing"
    assert out["warning"] is None


# ── 6. 远端 Qdrant 不在射程 ─────────────────────────────────────────
def test_remote_qdrant_not_flagged(tmp_path, monkeypatch, data_dir):
    _write_config(tmp_path, monkeypatch, _base_cfg(
        {"collection_name": "t", "host": "qdrant.internal", "port": 6333,
         "embedding_model_dims": 8},
        history_db_path=str(data_dir / "history.db"),
    ))
    out = _probe()
    assert out["status"] == "ok"
    assert out["warning"] is None
    check = out["checks"]["vector_store.config.path"]
    assert check["origin"] == "remote" and check["under_data_dir"] is None


# ── 7. 字段缺省 → 按 mem0 内建默认判定（~/.mem0 与 /tmp 都在 DATA_DIR 外）──
def test_unset_keys_use_mem0_builtin_defaults_and_warn(tmp_path, monkeypatch, data_dir):
    _write_config(tmp_path, monkeypatch, _base_cfg(
        {"collection_name": "t", "embedding_model_dims": 8},  # 无 path 也无 host/url
    ))  # 无 history_db_path
    out = _probe()
    assert out["status"] == "warning"
    assert set(out["mismatched"]) == {"vector_store.config.path", "history_db_path"}
    origins = {k: v["origin"] for k, v in out["checks"].items()}
    assert origins == {"vector_store.config.path": "mem0_builtin_default",
                       "history_db_path": "mem0_builtin_default"}


# ── 8. /health 集成：授权面可见，匿名面不泄 ────────────────────────
def test_health_probe_field_and_anonymous_redaction(tmp_path, monkeypatch, data_dir):
    token = "test-path-consistency-token"
    monkeypatch.setenv("AIDUMEM_API_TOKEN", token)
    stray = tmp_path / "stray"
    _write_config(tmp_path, monkeypatch, _base_cfg(
        {"collection_name": "t", "path": str(stray / "qdrant"), "embedding_model_dims": 8},
        history_db_path=str(stray / "history.db"),
    ))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ducky.hot.health import register_health_routes

    app = FastAPI()
    register_health_routes(app)
    client = TestClient(app)

    r = client.get("/health", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    pc = body["probes"]["runtime_paths"]["path_consistency"]
    assert pc["status"] == "warning"
    assert set(pc["mismatched"]) == {"vector_store.config.path", "history_db_path"}
    assert any("不在 AIDUMEM_DATA_DIR" in w for w in body["warnings"])

    anon = client.get("/health")  # 门禁启用、无凭据 → 留键公开视图
    assert anon.status_code == 200
    rp = anon.json()["probes"]["runtime_paths"]
    assert set(rp.keys()) == {"data_dir_writable", "_redacted"}
    assert "path_consistency" not in rp
    assert str(tmp_path) not in anon.text  # 绝对路径一点不泄
