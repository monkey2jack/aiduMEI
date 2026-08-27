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

    # v20.2.3（自查 S-2）：**解析 import 别名**。首版只认字面量 `os.`，
    # 于是 `import os as _os` + `int(_os.getenv(...))` 直接溜过 —— 仓里
    # 恰好就有两处别名导入。守卫自己的盲区比它守的缺陷更危险：它会让
    # 「扫过了」和「扫得到」看起来一模一样。
    os_names = {"os"}
    for nd in ast.walk(tree):
        if isinstance(nd, ast.Import):
            for a in nd.names:
                if a.name == "os" and a.asname:
                    os_names.add(a.asname)

    def _is_env_read(node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        f = node.func
        if isinstance(f, ast.Attribute):
            if f.attr == "getenv" and isinstance(f.value, ast.Name) and f.value.id in os_names:
                return True
            if (f.attr == "get" and isinstance(f.value, ast.Attribute)
                    and f.value.attr == "environ"
                    and isinstance(f.value.value, ast.Name)
                    and f.value.value.id in os_names):
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
    # 别名导入不许溜过（S-2：首版只认字面量 os.，仓里恰好有两处别名导入）
    assert _raw_env_casts('import os as _os\nx = int(_os.getenv("A", "1"))') == [2]
    assert _raw_env_casts('import os as _o\ny = float(_o.environ.get("B", "2"))') == [2]


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


# ══════════════════════════════════════════════════════════════════
# 引擎三档可选（v20.2.3 · 用户可自行选择云端/本地/自动）
# 立案由来：自动挡的本地备胎实测常驻 +151MB RSS / +169MB 磁盘，而旋钮级
# 调优（线程数/arena/malloc_trim）实测全部无效、模型也已是最小的中文可用
# 款 —— **唯一有效的优化就是「不加载它」**。于是「省内存」与「让用户自己
# 选档」是同一件事，一并落成产品能力。
# ══════════════════════════════════════════════════════════════════

class TestEngineModeSelection:
    def setup_method(self):
        from ducky.engine_mode import reset_mode_warnings_for_tests
        reset_mode_warnings_for_tests()

    def test_default_is_auto_and_both_legs_on(self, monkeypatch):
        import ducky.engine_mode as em
        monkeypatch.delenv("AIDUMEI_ENGINE_MODE", raising=False)
        assert em.configured_mode() == "auto"
        assert em.cloud_leg_enabled() and em.local_leg_enabled()

    @pytest.mark.parametrize("mode,cloud,local", [
        ("auto", True, True), ("cloud", True, False), ("local", False, True),
        ("AUTO", True, True), ("  Local  ", False, True),
    ])
    def test_modes_map_to_leg_switches(self, monkeypatch, mode, cloud, local):
        import ducky.engine_mode as em
        monkeypatch.setenv("AIDUMEI_ENGINE_MODE", mode)
        assert em.cloud_leg_enabled() is cloud
        assert em.local_leg_enabled() is local

    def test_illegal_mode_falls_back_to_auto_not_crash(self, monkeypatch):
        """档位配错不该让服务起不来（配置雷纪律一视同仁）。"""
        import ducky.engine_mode as em
        monkeypatch.setenv("AIDUMEI_ENGINE_MODE", "涡轮增压")
        assert em.configured_mode() == "auto"
        assert em.cloud_leg_enabled() and em.local_leg_enabled()

    def test_cloud_mode_never_loads_the_local_model(self, monkeypatch):
        """**省 151MB 的闸门**：云端档下探测直接报不可用，绝不触碰加载。"""
        import ducky.local_embed as le
        monkeypatch.setenv("AIDUMEI_ENGINE_MODE", "cloud")
        called = {"n": 0}

        def boom():
            called["n"] += 1
            raise AssertionError("云端档下不该加载本地模型")
        monkeypatch.setattr(le, "_load_model", boom)
        assert le.is_local_embed_available() is False
        assert called["n"] == 0, "云端档仍然摸了模型加载路径"

    def test_auto_mode_still_probes_the_model(self, monkeypatch):
        """区分力对照：自动挡下探测**必须**照常走加载路径 ——
        否则上面那条会因为「谁都不加载」而假绿。"""
        import ducky.local_embed as le
        monkeypatch.setenv("AIDUMEI_ENGINE_MODE", "auto")
        monkeypatch.setattr(le, "_FASTEMBED_IMPORTABLE", True)
        monkeypatch.setattr(le, "_model", None)
        called = {"n": 0}

        def fake_load():
            called["n"] += 1
            return object()
        monkeypatch.setattr(le, "_load_model", fake_load)
        assert le.is_local_embed_available() is True
        assert called["n"] == 1, "自动挡下没走加载路径——对照失去区分力"

    def test_health_exposes_the_configured_mode(self, monkeypatch):
        from ducky.engine_mode import mode_status
        monkeypatch.setenv("AIDUMEI_ENGINE_MODE", "cloud")
        st = mode_status()
        assert st["configured"] == "cloud"
        assert st["cloud_leg"] is True and st["local_leg"] is False
        assert st["note"], "档位探针必须自带人话说明，运维面不该只有一个枚举值"


# ══════════════════════════════════════════════════════════════════
# 自查项 S-1 / S-3（S-2 的验收在元守卫的区分力用例里）
# ══════════════════════════════════════════════════════════════════

class TestWindowTableDoesNotGrowUnbounded:
    """S-1：/login 是免鉴权公开端点，而计数表原先**从不清理** ——
    自查实测 5 万个源 IP = 5 万条常驻条目（约 12MB），死条目永不回收。"""

    def setup_method(self):
        from ducky.rate_guard import reset_rate_windows
        reset_rate_windows()

    def test_stale_entries_are_swept(self):
        from ducky.rate_guard import (_SWEEP_THRESHOLD, record_login_failure,
                                      window_count)
        old_t = 1_000_000.0
        for i in range(_SWEEP_THRESHOLD + 200):
            record_login_failure(f"2001:db8::{i:x}", now=old_t)
        grew = window_count()
        assert grew > _SWEEP_THRESHOLD
        # 下一个窗口来一个新请求：死条目必须被清掉
        record_login_failure("2001:db8::live", now=old_t + 600)
        after = window_count()
        assert after < 10, (
            f"上一窗口的 {grew} 条死条目没被清理（剩 {after}）—— "
            "免鉴权端点的无界增长仍在")

    def test_sweep_is_semantically_lossless(self):
        """区分力对照：清理不许影响判定。当前窗口的计数一条都不许丢。"""
        from ducky.rate_guard import (_SWEEP_THRESHOLD, login_failure_limit,
                                      login_locked, record_login_failure)
        t = 2_000_000.0
        victim = "203.0.113.99"
        for _ in range(login_failure_limit()):
            record_login_failure(victim, now=t)
        assert login_locked(victim, now=t) is not None
        # 触发清理：同窗口塞满阈值
        for i in range(_SWEEP_THRESHOLD + 50):
            record_login_failure(f"198.51.100.{i % 256}::{i}", now=t)
        assert login_locked(victim, now=t) is not None, (
            "清理把当前窗口的计数也扫掉了——护栏被自己的清理机制解除")

    def test_normal_deployment_never_triggers_sweep(self):
        """正常规模不该触发扫描（清理是保险丝，不是常态开销）。"""
        from ducky.rate_guard import _SWEEP_THRESHOLD, record_login_failure, window_count
        for i in range(100):
            record_login_failure(f"10.0.0.{i}")
        assert window_count() == 100 <= _SWEEP_THRESHOLD


class TestPendingVerdict:
    """S-3：欠账水位此前只有裸数字、没有判据。"""

    def test_low_water_is_ok(self):
        from ducky.dual_index import pending_verdict
        assert pending_verdict({"cloud": 3, "local": 1})["level"] == "ok"

    def test_high_water_with_progress_is_elevated_not_stuck(self, monkeypatch):
        """区分力：刚断供完正在排队补算 ≠ 卡死。只看数字会把前者误报成后者。"""
        import ducky.dual_index as di
        monkeypatch.setenv("AIDUMEI_PENDING_WARN_LEVEL", "10")
        monkeypatch.setattr(di, "last_replay_status",
                            lambda: {"report": {"replayed": 7}})
        assert di.pending_verdict({"cloud": 50, "local": 0})["level"] == "elevated"

    def test_high_water_without_progress_is_stuck_and_says_why(self, monkeypatch):
        import ducky.dual_index as di
        monkeypatch.setenv("AIDUMEI_PENDING_WARN_LEVEL", "10")
        monkeypatch.setattr(di, "last_replay_status",
                            lambda: {"report": {"replayed": 0}})
        v = di.pending_verdict({"cloud": 50, "local": 0})
        assert v["level"] == "stuck"
        assert "local_embed" in v["hint"], "判语必须指向下一步去哪看，不能只喊卡住了"

    def test_zero_threshold_disables_verdict(self, monkeypatch):
        import ducky.dual_index as di
        monkeypatch.setenv("AIDUMEI_PENDING_WARN_LEVEL", "0")
        assert di.pending_verdict({"cloud": 99999, "local": 0})["level"] == "ok"
