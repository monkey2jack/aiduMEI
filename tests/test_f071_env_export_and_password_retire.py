"""f0.7.1 — 两条「以为保护生效、其实没生效」的回归。

S1：服务端 .env 引导解析器不认 `export KEY=VAL`，门禁静默关闭（钩子/脚本却照常带 token）。
S10：设了 AIDUMEM_UI_PASSWORD 后，自动生成的旧口令仍可登录，明文文件仍在。
"""
import importlib
import os

import pytest

import ducky.security.auth as auth_mod


def _api():
    return importlib.import_module("api_server")


@pytest.mark.parametrize("line", [
    "export AIDUMEM_F071_PROBE=s3cret",
    "export\tAIDUMEM_F071_PROBE=s3cret",
    "export AIDUMEM_F071_PROBE='s3cret'",
    "AIDUMEM_F071_PROBE=s3cret",
])
def test_bootstrap_env_file_accepts_export_prefix(tmp_path, monkeypatch, line):
    env = tmp_path / ".env"
    env.write_text(f"# comment\n{line}\n", encoding="utf-8")
    monkeypatch.setenv("AIDUMEM_ENV_FILE", str(env))
    monkeypatch.delenv("AIDUMEM_F071_PROBE", raising=False)
    try:
        _api()._bootstrap_env_file()
        assert os.environ.get("AIDUMEM_F071_PROBE") == "s3cret"
        assert "export AIDUMEM_F071_PROBE" not in os.environ
    finally:
        os.environ.pop("AIDUMEM_F071_PROBE", None)


def test_bootstrap_and_parse_env_file_agree(tmp_path, monkeypatch):
    """两个解析器承诺一致：同一份文件得到同一组键值。"""
    from ducky.utils import parse_env_file
    env = tmp_path / ".env"
    env.write_text("export AIDUMEM_F071_A=1\nAIDUMEM_F071_B=\"two\"\n", encoding="utf-8")
    monkeypatch.setenv("AIDUMEM_ENV_FILE", str(env))
    for k in ("AIDUMEM_F071_A", "AIDUMEM_F071_B"):
        monkeypatch.delenv(k, raising=False)
    try:
        _api()._bootstrap_env_file()
        parsed = parse_env_file(str(env))
        for k in ("AIDUMEM_F071_A", "AIDUMEM_F071_B"):
            assert os.environ.get(k) == parsed[k]
    finally:
        for k in ("AIDUMEM_F071_A", "AIDUMEM_F071_B"):
            os.environ.pop(k, None)


@pytest.fixture
def _pwfiles(tmp_path, monkeypatch):
    hash_file = tmp_path / ".ui_password_hash"
    plain_file = tmp_path / ".ui_initial_password"
    monkeypatch.setattr(auth_mod, "password_hash_path", lambda: str(hash_file))
    monkeypatch.setattr(auth_mod, "initial_password_path", lambda: str(plain_file))
    monkeypatch.delenv("AIDUMEM_UI_PASSWORD", raising=False)
    return hash_file, plain_file


def test_env_password_retires_auto_password(_pwfiles, monkeypatch):
    hash_file, plain_file = _pwfiles
    _api()._ensure_ui_password()                       # 首启：自动口令
    auto_pwd = plain_file.read_text(encoding="utf-8").strip()
    assert auth_mod.check_ui_password(auto_pwd)

    monkeypatch.setenv("AIDUMEM_UI_PASSWORD", "env-password-123")
    # 即便还没重启（文件还在），校验时也不再接受自动口令
    assert not auth_mod.check_ui_password(auto_pwd)
    assert auth_mod.check_ui_password("env-password-123")

    _api()._ensure_ui_password()                       # 重启：作废并删文件
    assert not plain_file.exists()
    assert not hash_file.exists()
    assert not auth_mod.check_ui_password(auto_pwd)
    assert auth_mod.check_ui_password("env-password-123")


def test_env_password_keeps_user_set_hash(_pwfiles, monkeypatch):
    """控制台改过的口令（source=user）不被删除，仍然有效。"""
    hash_file, plain_file = _pwfiles
    auth_mod.write_password_hash(auth_mod.hash_password("console-pass-1"), source="user")
    monkeypatch.setenv("AIDUMEM_UI_PASSWORD", "env-password-123")
    _api()._ensure_ui_password()
    assert hash_file.exists()
    assert auth_mod.check_ui_password("console-pass-1")
    assert auth_mod.check_ui_password("env-password-123")
