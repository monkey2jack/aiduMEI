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


# ══════════════════════════════════════════════════════════════════
# 第二轮外审（2026-08-27）A-1 ~ A-4 整改验收
# ══════════════════════════════════════════════════════════════════

class TestNonFiniteEnvValues:
    """A-1：拆雷模块自己埋的雷 —— NaN 与任何数比较恒为 False，于是
    `not (v < min or v > max)` 对 NaN 恒真，NaN 被判「合法」静默通过。
    这是本模块头注自己定义的「假绿灯」形态，讽刺地长在拆雷模块里。"""

    def setup_method(self):
        from ducky.env_config import clear_config_errors_for_tests
        clear_config_errors_for_tests()

    # 上一版的非法值词表只有乱串/负数/超界 —— inf 拦得住、**nan 漏网**，
    # 负向对照恰好缺了这条区分力（重演自家「负向对照要有区分力」的老教训）。
    NON_FINITE = ["nan", "NaN", "NAN", "inf", "-inf", "Infinity", "-Infinity",
                  "1e999", "-1e999", "1E999"]

    @pytest.mark.parametrize("bad", NON_FINITE)
    def test_non_finite_rejected_on_unbounded_float(self, monkeypatch, bad):
        """**无上限**的参数最危险：inf 没有上限可撞，nan 谁都撞不上。"""
        from ducky.env_config import config_errors, float_env
        monkeypatch.setenv("AIDUMEI_TEST_F", bad)
        assert float_env("AIDUMEI_TEST_F", 0.05, minimum=0.0) == 0.05, (
            f"{bad!r} 旁路了下限判据 —— 静默变成非有限值")
        assert "AIDUMEI_TEST_F" in config_errors(), (
            f"{bad!r} 被拦下了却没进 config_errors —— 探针依旧失明")

    @pytest.mark.parametrize("bad", NON_FINITE)
    def test_non_finite_rejected_on_bounded_float(self, monkeypatch, bad):
        from ducky.env_config import config_errors, float_env
        monkeypatch.setenv("AIDUMEI_TEST_F", bad)
        assert float_env("AIDUMEI_TEST_F", 0.4, minimum=0.0, maximum=1.0) == 0.4
        assert "AIDUMEI_TEST_F" in config_errors()

    def test_real_scoring_params_are_finite_under_attack(self):
        """端到端：打分参数是本雷下游杀伤最大的地方（融合分整体 NaN →
        排序彻底失效而全系统报健康）。子进程验，因为常量在 import 期求值。"""
        import math
        for env, attr, default in (
            ("AIDUMEM_RERANK_WEIGHT", "RERANK_WEIGHT", 0.4),
            ("AIDUMEM_RECENCY_LAMBDA", "RECENCY_LAMBDA", 0.05),
            ("AIDUMEM_SIGMOIDAL_TEMP", "SIGMOIDAL_TEMPERATURE", 10.0),
        ):
            for bad in ("nan", "1e999"):
                code = (f"import math, ducky.scoring as s; v = s.{attr}; "
                        f"assert math.isfinite(v), f'非有限值漏网: {{v}}'; "
                        f"assert v == {default!r}, f'未回退默认: {{v}}'; print('OK')")
                r = subprocess.run([sys.executable, "-c", code],
                                   env={**os.environ, env: bad},
                                   capture_output=True, text=True,
                                   cwd=_ROOT, timeout=120)
                assert r.returncode == 0 and "OK" in r.stdout, (
                    f"{env}={bad} → {attr} 未被拦下：\n{r.stderr[-300:]}")

    def test_finite_values_still_pass(self):
        """区分力对照：拦非有限值不许误伤正常值（否则守卫退化成「全拒」）。"""
        from ducky.env_config import config_errors, float_env
        import os as _o
        for good, expect in (("0.0", 0.0), ("1e-7", 1e-7), ("0.999", 0.999),
                             ("1e300", 1e300)):
            _o.environ["AIDUMEI_TEST_F"] = good
            assert float_env("AIDUMEI_TEST_F", 9.9, minimum=0.0) == expect
            assert "AIDUMEI_TEST_F" not in config_errors()
        _o.environ.pop("AIDUMEI_TEST_F", None)


class TestExclusiveMinimumRestoresByteIdenticalBehaviour:
    """A-2：v20.2.1 的判据是 `v > 0`，收编时用 minimum=1e-6 近似 ——
    区间 (0, 1e-6) 的合法旧值被拒，「公开行为逐字不变」就不逐字了。"""

    def test_sub_microsecond_cooldown_is_accepted_again(self, monkeypatch):
        import ducky.gear as gear
        monkeypatch.setenv("AIDUMEI_GEAR_COOLDOWN_SEC", "0.0000005")
        assert gear.cooldown_sec() == 5e-07, (
            "亚微秒冷却被拒 —— 与 v20.2.1 的 `v > 0` 判据仍不逐字一致")

    @pytest.mark.parametrize("bad", ["0", "0.0", "-1", "-0.5"])
    def test_zero_and_negative_still_rejected(self, monkeypatch, bad):
        """区分力对照：恢复 (0,1e-6) 不许把 0 和负数一起放进来 ——
        旧判据是**严格**大于零。"""
        import ducky.gear as gear
        monkeypatch.setenv("AIDUMEI_GEAR_COOLDOWN_SEC", bad)
        assert gear.cooldown_sec() == 60.0, f"{bad} 不该被接受"

    def test_exclusive_and_inclusive_minimum_differ(self):
        """把两种下限的语义差钉死，防止将来又被 epsilon 近似糊过去。"""
        from ducky.env_config import float_env
        import os as _o
        _o.environ["AIDUMEI_TEST_F"] = "0.0"
        assert float_env("AIDUMEI_TEST_F", 7.0, minimum=0.0) == 0.0
        assert float_env("AIDUMEI_TEST_F", 7.0, exclusive_minimum=0.0) == 7.0
        _o.environ.pop("AIDUMEI_TEST_F", None)


class TestGearProbeIsHonestUnderPolicy:
    """A-4：本地档下云腿一次都不会被尝试，探针却报 full/closed ——
    只看 engine_gear 的值班人会以为云端腿正在服役且健康。"""

    def test_local_mode_probe_says_disabled(self, monkeypatch):
        from ducky.gear import gear_status, llm_gear_status, should_try_cloud
        monkeypatch.setenv("AIDUMEI_ENGINE_MODE", "local")
        assert should_try_cloud() is False
        for st in (gear_status(), llm_gear_status()):
            assert st["mode"] == "disabled_by_policy", st
            assert st["policy_disabled"] is True
            assert st["breaker_mode_if_serving"] == "full", (
                "熔断器真实内态被抹掉了 —— 它只是没在服役，不是不存在")

    @pytest.mark.parametrize("mode", ["auto", "cloud"])
    def test_other_modes_probe_unchanged(self, monkeypatch, mode):
        """区分力对照：云端档下云腿照常服役，探针**不许**报 disabled。"""
        from ducky.gear import gear_status
        monkeypatch.setenv("AIDUMEI_ENGINE_MODE", mode)
        st = gear_status()
        assert st["mode"] in ("full", "lite") and st["policy_disabled"] is False

    def test_decision_surface_stays_two_valued(self, monkeypatch):
        """**判定面不许被污染**：current_mode() 被 ducky/hot/add.py 拿去分流
        lite 分支，混进第三个值会走错路。探针诚实 ≠ 判定改语义。"""
        from ducky.gear import current_mode, llm_current_mode
        monkeypatch.setenv("AIDUMEI_ENGINE_MODE", "local")
        assert current_mode() in ("full", "lite")
        assert llm_current_mode() in ("full", "lite")


# ══════════════════════════════════════════════════════════════════
# A-3 治本：宣称用例数三面对账（version.py / CHANGELOG / pytest 实数）
#
# 立案由来是一次很难堪的自打脸：同一次发布里，S-4 自查项刚写下
# 「数字过期就是假话」，version.py 里的用例总数就过期了 —— 第二个提交
# 新增 20 条用例，没回头改第一个提交写下的数字。而且根因是**替换悄悄
# 没生效**（锚文本凭记忆写，实际文本不同），我在这一轮里踩了四次。
#
# README 那三行早有守卫（test_v19_4_1_audit_fixes），CHANGELOG 与
# version.py 一直没有 —— 「谁在看着这两份文件的数字」的答案是没人。
# 本条把答案变成：pytest。靠记性的东西，迟早都要焊成结构。
# ══════════════════════════════════════════════════════════════════

def _collected_total() -> int:
    """从 pytest 自身取真值（同 README 守卫的方法论：不硬编码期望值）。"""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q",
         "-p", "no:cacheprovider"],
        cwd=_ROOT, capture_output=True, text=True, timeout=600)
    m = re.search(r"(\d+)\s+tests?\s+collected", proc.stdout)
    assert m, f"无法解析实际用例数：\n{proc.stdout[-400:]}"
    return int(m.group(1))


def _service_version() -> str:
    src = open(os.path.join(_ROOT, "ducky", "version.py"), encoding="utf-8").read()
    m = re.search(r'^SERVICE_VERSION\s*=\s*"([^"]+)"', src, re.M)
    assert m, "读不到 SERVICE_VERSION —— 对账失去锚点"
    return m.group(1)


def _current_release_blocks() -> dict[str, str]:
    """当前版本在 version.py 与 CHANGELOG.md 里的那一段。"""
    ver = _service_version()
    out = {}
    src = open(os.path.join(_ROOT, "ducky", "version.py"), encoding="utf-8").read()
    m = re.search(r"^v%s \(.*?(?=^v\d+\.\d+(?:\.\d+)? \()" % re.escape(ver),
                  src, re.M | re.S)
    assert m, f"version.py 找不到 v{ver} 说明块"
    out["ducky/version.py"] = m.group(0)
    log = open(os.path.join(_ROOT, "CHANGELOG.md"), encoding="utf-8").read()
    m = re.search(r"^## v%s\b(.*?)(?=^## v\d)" % re.escape(ver), log, re.M | re.S)
    assert m, f"CHANGELOG.md 找不到 v{ver} 小节"
    out["CHANGELOG.md"] = m.group(1)
    return out


_CLAIM_RE = re.compile(r"用例总数\s*(\d+)\s*(?:→|->)\s*(\d+)")


def test_claimed_case_count_matches_reality_in_every_release_doc():
    """version.py 与 CHANGELOG 当前版本段里宣称的用例总数 = pytest 实数。"""
    actual = _collected_total()
    problems = []
    for fname, block in _current_release_blocks().items():
        hits = _CLAIM_RE.findall(block)
        if not hits:
            problems.append(f"{fname}：当前版本段里没有「用例总数 X → Y」的宣称 —— "
                            "格式变了，本守卫失去着力点，请同步改判据")
            continue
        for _before, after in hits:
            if int(after) != actual:
                problems.append(f"{fname} 宣称用例总数 {after}，pytest 实测 {actual}")
    assert not problems, (
        "宣称与实况脱节（宣称即承诺铁律）：\n  " + "\n  ".join(problems)
        + "\n\n这条守卫的由来：v20.2.3 第二个提交新增用例后，第一个提交写下的"
        "\n数字没人回头改，而同一次发布里的自查项刚写过「数字过期就是假话」。")


def test_claimed_counts_agree_across_all_release_docs():
    """三面互相之间也要一致 —— 只对实数不够：两份文档各自对了实数、
    却在别的数字上互相打架，读者照样无从分辨（v19.4.2 就这么翻过车）。"""
    claims = {}
    for fname, block in _current_release_blocks().items():
        for before, after in _CLAIM_RE.findall(block):
            claims.setdefault(fname, set()).add((before, after))
    values = {v for s in claims.values() for v in s}
    assert len(values) <= 1, (
        f"各文档对同一版本的用例数宣称不一致：{claims}")


def test_this_guard_can_actually_see_a_stale_number():
    """守卫自证区分力：拿一段**故意写错**的文本喂给同一套判据，必须认出来。
    （不改真文件——守卫的自证不该依赖破坏被守护的东西。）"""
    fake = "用例总数 1290 → 999"
    hits = _CLAIM_RE.findall(fake)
    assert hits == [("1290", "999")], "判据认不出宣称形态，等于永远绿"
    assert int(hits[0][1]) != _collected_total(), "自证样本恰好等于实数，失去区分力"
