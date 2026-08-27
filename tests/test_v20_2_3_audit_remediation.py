"""tests/test_v20_2_3_audit_remediation.py — 外审 M 组整改验收（v20.2.3）

对应外部审计（2026-08-27）的 H-1 / M-1 / M-2 三项：
  · H-1 运行时依赖双清单 → tests/test_v20_runtime_deps_declaration.py（另立）
  · M-1 /login 爆破护栏 → 本文件 TestLoginBruteForceGuard
  · M-2 配置雷普查与拆除 → 本文件 TestConfigMines（含**元守卫**防回归）
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ══════════════════════════════════════════════════════════════════
# M-2 · 配置雷：非法 env 一律回退默认 + 出声，绝不 raise
# ══════════════════════════════════════════════════════════════════

# 「非法 env 必须能被安全解析」的模块与其 env 名。这几处在 v20.2.3 之前
# 都是裸 int()/float()，**多数炸在 import 期**：一个配置笔误让整个服务
# 起不来（比 v20.2.1 外审 R1 原案更狠——R1 那两处至少还能起得来）。
_MINED_SITES = [
    ("ducky.security.auth", "AIDUMEM_SESSION_TTL_SECONDS", "SESSION_TTL_SECONDS", 43200),
    ("ducky.security.injection_guard", "AIDUMEM_MAX_MEMORY_CHARS", "MAX_CONTENT_LENGTH", 100000),
    ("ducky.scoring", "AIDUMEM_RECENCY_LAMBDA", "RECENCY_LAMBDA", 0.05),
    ("ducky.scoring", "AIDUMEM_RERANK_WEIGHT", "RERANK_WEIGHT", 0.4),
    ("ducky.scoring", "AIDUMEM_SIGMOIDAL_TEMP", "SIGMOIDAL_TEMPERATURE", 10.0),
]


class TestConfigMines:
    @pytest.mark.parametrize("mod,env,attr,default", _MINED_SITES)
    def test_invalid_env_falls_back_instead_of_crashing_import(self, mod, env, attr, default):
        """非法值：模块照常 import，取值回退默认。

        **子进程跑**是刻意的：这些常量在 import 期求值，父进程里模块早已
        加载，monkeypatch env 根本影响不到它们——那样的「测试」会稳过，
        且证明不了任何事（假绿灯）。
        """
        code = (f"import {mod} as m; "
                f"v = m.{attr}; "
                f"assert v == {default!r}, f'回退值不对: {{v}}'; "
                f"print('OK')")
        r = subprocess.run([sys.executable, "-c", code],
                           env={**os.environ, env: "garbage_不是数字"},
                           capture_output=True, text=True, cwd=_ROOT, timeout=120)
        assert r.returncode == 0, (
            f"{mod} 在 {env}=非法值 时 import 失败——配置雷还埋着：\n"
            f"{r.stderr[-400:]}"
        )
        assert "OK" in r.stdout

    @pytest.mark.parametrize("mod,env,attr,default", _MINED_SITES)
    def test_valid_env_still_takes_effect(self, mod, env, attr, default):
        """区分力对照：回退 ≠ 忽略配置。合法值必须照常生效。"""
        good = "7200" if isinstance(default, int) else "0.25"
        expect = float(good) if isinstance(default, float) else int(good)
        code = (f"import {mod} as m; "
                f"assert m.{attr} == {expect!r}, f'合法值没生效: {{m.{attr}}}'; "
                f"print('OK')")
        r = subprocess.run([sys.executable, "-c", code],
                           env={**os.environ, env: good},
                           capture_output=True, text=True, cwd=_ROOT, timeout=120)
        assert r.returncode == 0 and "OK" in r.stdout, (
            f"{mod} 的 {env} 合法值未生效（守卫退化成「永远用默认」）：\n{r.stderr[-300:]}"
        )

    def test_invalid_env_is_visible_not_silent(self, monkeypatch):
        """不静默：非法值必须留在 config_errors 里可查（进 /health 探针）。"""
        from ducky.env_config import clear_config_errors_for_tests, config_errors, int_env
        clear_config_errors_for_tests()
        monkeypatch.setenv("AIDUMEI_TEST_KNOB", "很多")
        assert int_env("AIDUMEI_TEST_KNOB", 5) == 5
        assert "AIDUMEI_TEST_KNOB" in config_errors()
        monkeypatch.setenv("AIDUMEI_TEST_KNOB", "9")
        assert int_env("AIDUMEI_TEST_KNOB", 5) == 9
        assert "AIDUMEI_TEST_KNOB" not in config_errors(), "合法值未清除旧错误记录"

    def test_out_of_range_also_falls_back(self, monkeypatch):
        """越界与不可解析同等处理——负的 TTL 不比 'garbage' 更该被接受。"""
        from ducky.env_config import clear_config_errors_for_tests, config_errors, int_env
        clear_config_errors_for_tests()
        monkeypatch.setenv("AIDUMEI_TEST_KNOB", "-3")
        assert int_env("AIDUMEI_TEST_KNOB", 5, minimum=1) == 5
        assert "AIDUMEI_TEST_KNOB" in config_errors()


# ── 元守卫：裸转换形态不许回归 ────────────────────────────────────────

# 判据走 **AST 不走字符串**：本仓刚被自家文档字符串绊过一次 ——
# ducky/env_config.py 的头注里写着 `int(os.environ.get(...))` 当反面例子，
# 正则分不清「代码」和「讲代码的话」，于是把单一真相源自己判成了违规。
# （同款教训另见 test_v20_1_1_source_guards 的 SQL 关键字回溯赝品。）

# 允许保留裸形态的位点，必须写明理由。
_RAW_CAST_EXEMPT: dict[str, str] = {}


def _raw_env_casts(src: str) -> list[int]:
    """返回裸 int(os.environ.get(...)) / float(os.getenv(...)) 的行号。

    只认真正的**调用节点**：int/float 直接包住 os.environ.get 或
    os.getenv。注释与字符串字面量天然不进 AST，误报为零。
    """
    import ast
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []

    def _is_env_read(node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        f = node.func
        if isinstance(f, ast.Attribute):
            if f.attr == "getenv" and isinstance(f.value, ast.Name) and f.value.id == "os":
                return True
            if (f.attr == "get" and isinstance(f.value, ast.Attribute)
                    and f.value.attr == "environ"
                    and isinstance(f.value.value, ast.Name) and f.value.value.id == "os"):
                return True
        return False

    def _wraps_env(node: ast.AST) -> bool:
        """int(env) 直接包，或 int(env or 默认) 这种 or 链包 —— 都算。"""
        if _is_env_read(node):
            return True
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            return any(_wraps_env(v) for v in node.values)
        return False

    out = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in {"int", "float"} and node.args
                and _wraps_env(node.args[0])):
            out.append(node.lineno)
    return out


def _scanned_files() -> list[str]:
    out = []
    for base, dirs, files in os.walk(_ROOT):
        dirs[:] = [d for d in dirs
                   if d not in {".git", ".venv", "venv", "node_modules",
                                "__pycache__", "tests", "data", "logs",
                                "benchmarks", "docs"}]
        for f in files:
            if f.endswith(".py"):
                out.append(os.path.relpath(os.path.join(base, f), _ROOT))
    return out


def test_no_raw_env_numeric_cast_in_production_code():
    """产品代码里不许再有 `int(os.environ.get(...))` 这种裸转换。

    外审 M-2 点名了两处，自查普查出六处，元守卫上岗后又抓出两处
    （frontend/dev_server.py、integrations 的 hook）——**普查比点名更值钱，
    而普查若不焊成守卫，下一处照样会长出来**（v19.4.2 的 inotify_simple
    与 v20.2.3 的 python-multipart 就是同一个道理）。

    正确写法：`ducky.env_config` 的 int_env / float_env。
    """
    offenders = {}
    for rel in _scanned_files():
        if rel in _RAW_CAST_EXEMPT:
            continue
        src = open(os.path.join(_ROOT, rel), encoding="utf-8").read()
        lines = _raw_env_casts(src)
        if lines:
            offenders[rel] = lines
    assert not offenders, (
        "以下文件仍在用裸 int()/float() 解析 env —— 非法值会 raise，"
        "多数还炸在 import 期（服务直接起不来）：\n  "
        + "\n  ".join(f"{k}:{v}" for k, v in sorted(offenders.items()))
        + "\n请改用 ducky.env_config 的 int_env / float_env；"
        "\n确有理由保留的，加进 _RAW_CAST_EXEMPT 并写明为什么它炸了也没关系。"
    )


def test_meta_guard_has_discriminating_power():
    """守卫自证：认得出坏形态、认不错好形态、**不被文档字符串骗到**。"""
    assert _raw_env_casts('x = int(os.environ.get("A", "1"))') == [1]
    assert _raw_env_casts("y = float(os.getenv('B', '2'))") == [1]
    assert _raw_env_casts('z = int(os.environ.get("C") or 8)') == [1]
    # 正确写法不许被误伤
    assert _raw_env_casts('v = int_env("A", 1)') == []
    assert _raw_env_casts('s = os.environ.get("A", "1")') == []
    # 讲坏形态的话不是坏形态（正则版在这里翻过车）
    assert _raw_env_casts('"""别写 int(os.environ.get(...)) 这种。"""') == []
    assert _raw_env_casts('# 反面例子：int(os.environ.get("A"))') == []


# ══════════════════════════════════════════════════════════════════
# M-1 · /login 爆破护栏
# ══════════════════════════════════════════════════════════════════

class TestLoginBruteForceGuard:
    def setup_method(self):
        from ducky.rate_guard import reset_rate_windows
        reset_rate_windows()

    def test_failures_lock_out_but_successes_do_not_count(self):
        from ducky.rate_guard import (login_failure_limit, login_locked,
                                      record_login_failure)
        limit = login_failure_limit()
        assert limit > 0
        ip = "203.0.113.7"
        for _ in range(limit):
            assert login_locked(ip) is None, "未到上限就锁——正常用户会被误伤"
            record_login_failure(ip)
        retry = login_locked(ip)
        assert isinstance(retry, int) and retry >= 1, "到上限仍不锁——护栏空转"

    def test_lockout_is_per_ip(self):
        from ducky.rate_guard import (login_failure_limit, login_locked,
                                      record_login_failure)
        a, b = "203.0.113.7", "203.0.113.8"
        for _ in range(login_failure_limit()):
            record_login_failure(a)
        assert login_locked(a) is not None
        assert login_locked(b) is None, "一个 IP 超限牵连了别人"

    def test_window_rolls_over(self):
        from ducky.rate_guard import (login_failure_limit, login_locked,
                                      record_login_failure)
        ip, t = "203.0.113.9", 1_000_000.0
        for _ in range(login_failure_limit()):
            record_login_failure(ip, now=t)
        assert login_locked(ip, now=t) is not None
        assert login_locked(ip, now=t + 61) is None, "下一窗口仍锁死——永久封 IP 不是本意"

    def test_zero_limit_disables_guard(self, monkeypatch):
        from ducky.rate_guard import login_locked, record_login_failure
        monkeypatch.setenv("AIDUMEI_LOGIN_FAILURES_PER_MIN", "0")
        ip = "203.0.113.10"
        for _ in range(50):
            record_login_failure(ip)
        assert login_locked(ip) is None, "0 应关闭护栏（与写路径限流同语义）"

    def test_login_route_returns_429_with_retry_after(self, monkeypatch, tmp_path):
        """端到端：超限后 /login 必须 429 + Retry-After，且**不再校验口令**
        （连正确口令都挡在门外——这正是「先查后验」的预期行为）。"""
        from fastapi.testclient import TestClient

        import api_server
        from ducky.rate_guard import record_login_failure, login_failure_limit, reset_rate_windows

        reset_rate_windows()
        checked = {"n": 0}
        real_check = api_server.__dict__.get("check_ui_password")

        import ducky.security.auth as auth

        def counting_check(pw):
            checked["n"] += 1
            return True   # 口令永远正确：若护栏失效，登录会 200
        monkeypatch.setattr(auth, "check_ui_password", counting_check)

        client = TestClient(api_server.app)
        for _ in range(login_failure_limit()):
            record_login_failure("testclient")

        r = client.post("/login", json={"password": "correct-password"})
        assert r.status_code == 429, (
            f"超限后仍返回 {r.status_code} —— 护栏没接到路由上")
        assert r.headers.get("Retry-After"), "429 必须带 Retry-After"
        assert checked["n"] == 0, (
            "超限时仍调用了口令校验——「先查后验」失守（白烧 PBKDF2 且给旁路信号）")
