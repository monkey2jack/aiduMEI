"""f0.8 ③-5 认证与环境变量：改密后 env 口令失效、空环境变量不挡 .env、空 HOST 不绑全网卡。

外审 deepseek F-12 / F-21 / F-22 / F-25、muse M-11 的反向断言（修复前全部复现）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def pwd_env(monkeypatch, tmp_path):
    from ducky.security import auth
    monkeypatch.setattr(auth, "password_hash_path", lambda: str(tmp_path / ".ui_password_hash"))
    monkeypatch.setenv("AIDUMEM_UI_PASSWORD", "EnvInitialPass123")
    return auth


def test_env_password_stops_working_after_console_change(pwd_env):
    auth = pwd_env
    assert auth.check_ui_password("EnvInitialPass123") is True        # 引导口令可用
    assert auth.write_password_hash(auth.hash_password("BrandNewPassword456"))
    assert auth.check_ui_password("BrandNewPassword456") is True
    assert auth.check_ui_password("EnvInitialPass123") is False       # 修复前 True
    assert auth.env_password_shadowed() is True


def test_env_password_still_retires_auto_password(pwd_env):
    auth = pwd_env
    auth.write_password_hash(auth.hash_password("auto-generated-xyz"), source="auto")
    assert auth.check_ui_password("auto-generated-xyz") is False
    assert auth.check_ui_password("EnvInitialPass123") is True
    assert auth.env_password_shadowed() is False


def test_no_hash_env_only_works(pwd_env):
    assert pwd_env.check_ui_password("EnvInitialPass123") is True
    assert pwd_env.check_ui_password("wrong") is False


def test_load_env_file_fills_empty_env_vars(monkeypatch, tmp_path):
    from ducky.utils import load_env_file
    env = tmp_path / ".env"
    env.write_text('AIDUMEM_API_TOKEN="real-token-xyz"\nF08_PROBE_SET=from-file\nF08_PROBE_BLANK=x\n')
    monkeypatch.setenv("AIDUMEM_API_TOKEN", "")
    monkeypatch.setenv("F08_PROBE_SET", "explicit")
    monkeypatch.setenv("F08_PROBE_BLANK", "   ")
    load_env_file(str(env))
    assert os.environ["AIDUMEM_API_TOKEN"] == "real-token-xyz"         # 修复前 ''
    assert os.environ["F08_PROBE_SET"] == "explicit"                   # 显式非空值仍优先
    assert os.environ["F08_PROBE_BLANK"] == "x"


def _probe(env_extra: dict, code: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AIDUMEM_", "AIDUMEI_"))}
    env.update(env_extra)
    p = subprocess.run([sys.executable, "-c", code], cwd=_ROOT, env=env,
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr[-2000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


def test_api_server_bootstrap_empty_env_does_not_shadow_dotenv(tmp_path):
    """外审 F-21：`AIDUMEM_API_TOKEN=` 会把 .env 的真 token 顶掉，门禁静默关闭。"""
    envf = tmp_path / ".env"
    envf.write_text('AIDUMEM_API_TOKEN="real-token-xyz"\n')
    data = tmp_path / "data"
    data.mkdir()
    out = _probe({"AIDUMEM_ENV_FILE": str(envf), "AIDUMEM_API_TOKEN": "", "AIDUMEM_DATA_DIR": str(data)},
                 "import json, api_server as a; print(json.dumps({'tok': a._api_token(), 'gate': a._auth_enabled()}))")
    assert out == {"tok": "real-token-xyz", "gate": True}


@pytest.mark.parametrize("raw", ["", "   "])
def test_empty_host_resolves_to_loopback_everywhere(monkeypatch, raw):
    """外审 F-22：门禁判回环、uvicorn 实绑 0.0.0.0。现在两处读同一个解析函数。"""
    import api_server
    monkeypatch.setenv("AIDUMEM_HOST", raw)
    monkeypatch.setattr(sys, "argv", ["api_server.py"])
    assert api_server._env_bind_host() == "127.0.0.1"
    assert api_server._detect_bind_host() == "127.0.0.1"


def test_main_passes_resolved_host_to_uvicorn(monkeypatch):
    import api_server
    seen = {}
    monkeypatch.setenv("AIDUMEM_HOST", "")
    monkeypatch.setattr(sys, "argv", ["api_server.py"])
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: seen.update(k))
    try:
        api_server.main()
    except SystemExit:
        pass
    assert seen.get("host") == "127.0.0.1", seen


def test_zero_address_is_not_a_trusted_loopback_host_name():
    import api_server
    assert "0.0.0.0" not in api_server._LOOPBACK_HOST_NAMES
