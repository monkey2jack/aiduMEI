"""tests/test_f08_env_file_probe.py — f0.8.0：实体词来源探针必须读服务真正用的那份 `.env`

服务启动（api_server._bootstrap_env_file / ducky.utils.load_env_file）认
`AIDUMEM_ENV_FILE`；/health 的实体词来源探针以前只读代码目录下的 `.env`。
`.env` 放在代码目录之外的部署因此被误报 `no_env_file` / `overridden`。
合成数据，不碰真实配置。
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import ducky.pipeline.memory_gate as gate  # noqa: E402

_KEY = "AIDUMEM_ENTITY_KEYWORDS"
_WORDS = "合成词甲|合成词乙"


@pytest.fixture
def _dirs(tmp_path, monkeypatch):
    import ducky.utils as utils
    code = tmp_path / "code"
    elsewhere = tmp_path / "etc"
    code.mkdir()
    elsewhere.mkdir()
    monkeypatch.setattr(utils, "BASE_DIR", str(code))
    monkeypatch.delenv(_KEY, raising=False)
    monkeypatch.delenv("AIDUMEM_ENV_FILE", raising=False)
    monkeypatch.setattr(gate, "_SELF_REF_CACHE", {"key": None, "pattern": None})
    monkeypatch.setattr(gate, "_ENTITY_WARNED", False)
    return code, elsewhere


def test_probe_honors_aidumem_env_file_outside_code_dir(_dirs, monkeypatch):
    """★ 反向用例：`.env` 只在 AIDUMEM_ENV_FILE 指向处；旧代码报 no_env_file。"""
    code, elsewhere = _dirs
    env = elsewhere / "aidumem.env"
    env.write_text(f"{_KEY}={_WORDS}\n", encoding="utf-8")
    monkeypatch.setenv("AIDUMEM_ENV_FILE", str(env))
    monkeypatch.setenv(_KEY, _WORDS)
    assert not (code / ".env").exists()
    st = gate.entity_keywords_status()
    assert st["source"] == "env_file", st


def test_probe_prefers_env_file_var_over_stale_code_dir_copy(_dirs, monkeypatch):
    """代码目录里有一份旧 `.env`，服务读的是 AIDUMEM_ENV_FILE —— 探针必须跟服务一致。"""
    code, elsewhere = _dirs
    (code / ".env").write_text(f"{_KEY}=旧词表\n", encoding="utf-8")
    env = elsewhere / "aidumem.env"
    env.write_text(f"{_KEY}={_WORDS}\n", encoding="utf-8")
    monkeypatch.setenv("AIDUMEM_ENV_FILE", str(env))
    monkeypatch.setenv(_KEY, _WORDS)
    assert gate.entity_keywords_status()["source"] == "env_file"


def test_probe_default_still_reads_code_dir(_dirs, monkeypatch):
    """不设 AIDUMEM_ENV_FILE 时行为不变：读代码目录（BASE_DIR）下的 `.env`。"""
    code, _ = _dirs
    (code / ".env").write_text(f"{_KEY}={_WORDS}\n", encoding="utf-8")
    monkeypatch.setenv(_KEY, _WORDS)
    assert gate.entity_keywords_status()["source"] == "env_file"


def test_probe_and_service_share_one_path_rule(_dirs, monkeypatch, tmp_path):
    """探针与 ducky.utils 的 `.env` 路径规则是同一个函数，不各写一份。"""
    import inspect

    import ducky.utils as utils
    src = inspect.getsource(gate._declared_in_env_file)
    assert "env_file_path" in src
    target = tmp_path / "x.env"
    monkeypatch.setenv("AIDUMEM_ENV_FILE", str(target))
    assert utils.env_file_path("/nowhere") == str(target)
    assert utils._env_file_path() == str(target)
