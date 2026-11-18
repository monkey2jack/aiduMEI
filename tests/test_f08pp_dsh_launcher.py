"""DSH launcher isolation and shared MCP defaults with and without opt-in."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "integrations" / "dsh" / "mcp-stdio.py"
SPEC = importlib.util.spec_from_file_location("dsh_mcp_launcher", SOURCE)
LAUNCHER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAUNCHER)


def test_test_endpoint_never_uses_production_token(tmp_path):
    token = tmp_path / "test-token"
    token.write_text("synthetic-isolated-token", encoding="utf-8")
    resolved, repo = LAUNCHER.prepare({
        "AIDUMEI_TEST_URL": "http://127.0.0.1:8878",
        "AIDUMEM_API_TOKEN": "synthetic-production-token",
        "AIDUMEM_USER_ID": "dsh-test",
        "AIDUMEM_ENV_FILE": str(tmp_path / "production.env"),
        "AIDUMEI_TEST_TOKEN_FILE": str(token),
        "XDG_STATE_HOME": str(tmp_path), "LOCALAPPDATA": str(tmp_path),
    })
    assert resolved["AIDUMEM_API_TOKEN"] == "synthetic-isolated-token"
    assert resolved["AIDUMEM_ENV_FILE"] == os.devnull
    assert resolved["AIDUMEM_DEFAULT_USER_ID"] == "dsh-test"
    assert resolved["AIDUMEI_DEFAULT_BANK_ID"] == "default"
    assert resolved["AIDUMEM_HOST_STATE_DB"] == ""
    assert resolved["AIDUMEI_MCP_DANGEROUS_TOOLS"] == "0"
    assert repo == SOURCE.parents[2]


def test_dsh_bank_is_the_mcp_default_only_in_the_opted_in_process(tmp_path):
    code = (
        "import inspect, runpy; ns=runpy.run_path('mcp_server.py', run_name='_dsh_scope_check'); "
        "assert inspect.signature(ns['mem_search']).parameters['bank_id'].default == 'dsh-preview'"
    )
    result = subprocess.run([sys.executable, "-c", code], cwd=SOURCE.parents[2], capture_output=True, text=True,
                            env={**os.environ, "AIDUMEI_DEFAULT_BANK_ID": "dsh-preview",
                                 "AIDUMEM_ENV_FILE": os.devnull, "AIDUMEM_HOST_STATE_DB": "",
                                 "AIDUMEM_DATA_DIR": str(tmp_path / "data"), "AIDUMEM_LOG_DIR": str(tmp_path / "logs")})
    assert result.returncode == 0, result.stderr


def test_shared_mcp_bank_default_is_unchanged_without_opt_in(tmp_path):
    code = (
        "import inspect, runpy; ns=runpy.run_path('mcp_server.py', run_name='_default_scope_check'); "
        "assert inspect.signature(ns['mem_search']).parameters['bank_id'].default == 'default'"
    )
    env = {key: value for key, value in os.environ.items() if key != "AIDUMEI_DEFAULT_BANK_ID"}
    env.update({"AIDUMEM_ENV_FILE": os.devnull, "AIDUMEM_HOST_STATE_DB": "",
                "AIDUMEM_DATA_DIR": str(tmp_path / "data"), "AIDUMEM_LOG_DIR": str(tmp_path / "logs")})
    result = subprocess.run([sys.executable, "-c", code], cwd=SOURCE.parents[2], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr


def test_missing_test_token_does_not_fall_back():
    resolved, _ = LAUNCHER.prepare({"AIDUMEI_TEST_URL": "http://127.0.0.1:8878", "AIDUMEM_API_TOKEN": "synthetic-production-token"})
    assert resolved["AIDUMEM_API_TOKEN"] == ""


def test_production_mcp_token_uses_explicit_env_file_fallback(tmp_path):
    env_file = tmp_path / "production.env"
    env_file.write_text("export AIDUMEM_API_TOKEN=production-scoped-token\n", encoding="utf-8")
    resolved, _ = LAUNCHER.prepare({"AIDUMEM_ENV_FILE": str(env_file)})
    assert resolved["AIDUMEM_API_TOKEN"] == "production-scoped-token"


def test_test_endpoint_never_reads_production_env_file(tmp_path):
    env_file = tmp_path / "production.env"
    env_file.write_text("AIDUMEM_API_TOKEN=production-scoped-token\n", encoding="utf-8")
    resolved, _ = LAUNCHER.prepare({
        "AIDUMEI_TEST_URL": "http://127.0.0.1:8878",
        "AIDUMEM_ENV_FILE": str(env_file),
    })
    assert resolved["AIDUMEM_API_TOKEN"] == ""


@pytest.mark.parametrize("url", ["file:///tmp/api", "https://user:password@example.invalid", "https://example.invalid?token=x", "https://example.invalid#token"])
def test_unsafe_endpoint_is_refused(url):
    with pytest.raises(ValueError, match="invalid_api_base"):
        LAUNCHER.prepare({"AIDUMEI_TEST_URL": url})


def test_local_state_isolated_by_endpoint_user_and_test_mode():
    base = {"AIDUMEM_API_BASE": "http://127.0.0.1:8767", "AIDUMEM_USER_ID": "alpha"}
    cases = [base, {**base, "AIDUMEM_USER_ID": "beta"}, {**base, "AIDUMEM_API_BASE": "http://127.0.0.1:8878"},
             {**base, "AIDUMEI_TEST_URL": base["AIDUMEM_API_BASE"]}]
    assert len({LAUNCHER.prepare(env)[0]["AIDUMEM_DATA_DIR"] for env in cases}) == len(cases)


def test_global_off_exits_before_invalid_configuration(tmp_path):
    result = subprocess.run([sys.executable, str(SOURCE), "--check"], capture_output=True, text=True,
                            env={**os.environ, "AIDUMEI_HOOKS": "off", "AIDUMEI_TEST_URL": "invalid",
                                 "XDG_STATE_HOME": str(tmp_path), "LOCALAPPDATA": str(tmp_path)})
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""
    assert list(tmp_path.iterdir()) == []
