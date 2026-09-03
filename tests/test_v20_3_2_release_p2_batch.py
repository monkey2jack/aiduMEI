"""v20.3.2 正式版 · P2 批次守卫：E1 / E7 / E10 / P2-18 / P2-19 / P2-20 / P2-31 / P2-32 / P2-34 / 文档面。

每条都小，但都是「定义了不接线」「说的与跑的不一致」「世界模型落后」的具体形态。
"""
import ast
import json
import logging
import pathlib
import sqlite3
import sys
import threading
import time

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _fn_source(path: pathlib.Path, name: str) -> str:
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return ast.get_source_segment(src, n) or ""
    raise AssertionError(f"{path.name} 里找不到 {name}")


# ── E1 · 自定义互斥规则从文件装载并接线 ─────────────────────────────

def test_custom_exclusion_rules_load_from_file_skipping_bad_regex(tmp_path, caplog):
    from ducky import conflict_resolver as cr
    before = list(cr.MUTUAL_EXCLUSION_PATTERNS)
    f = tmp_path / "conflict_rules.json"
    f.write_text(json.dumps({"mutual_exclusion": [
        {"attr": "(域名|url)", "old": "old\\.example", "new": "new\\.example"},
        {"attr": "(", "old": "x", "new": "y"},
    ]}), encoding="utf-8")
    try:
        with caplog.at_level(logging.WARNING):
            n = cr.load_custom_exclusion_patterns_from_file(str(f))
        assert n == 1, "坏正则那条应被跳过，好的那条应装入"
        assert ("(域名|url)", "old\\.example", "new\\.example") in cr.MUTUAL_EXCLUSION_PATTERNS
        assert any("无效" in r.getMessage() for r in caplog.records), "坏条目没出声"
    finally:
        cr.MUTUAL_EXCLUSION_PATTERNS = before


def test_missing_rules_file_is_zero_not_error(tmp_path):
    from ducky import conflict_resolver as cr
    assert cr.load_custom_exclusion_patterns_from_file(str(tmp_path / "nope.json")) == 0


def test_corrupt_rules_file_warns_and_loads_nothing(tmp_path, caplog):
    from ducky import conflict_resolver as cr
    before = list(cr.MUTUAL_EXCLUSION_PATTERNS)
    f = tmp_path / "conflict_rules.json"
    f.write_text("{ not json", encoding="utf-8")
    try:
        with caplog.at_level(logging.WARNING):
            assert cr.load_custom_exclusion_patterns_from_file(str(f)) == 0
        assert any("不可读" in r.getMessage() for r in caplog.records)
        assert cr.MUTUAL_EXCLUSION_PATTERNS == before
    finally:
        cr.MUTUAL_EXCLUSION_PATTERNS = before


def test_lifespan_wires_rule_loading():
    src = _fn_source(_ROOT / "api_server.py", "_lifespan")
    assert "load_custom_exclusion_patterns_from_file" in src, "逃生阀又没接线"


# ── E7 / E10 · 文案如实 ─────────────────────────────────────────────

def test_watermark_warning_tells_the_truth_about_refine_memory():
    src = (_ROOT / "ducky" / "hot" / "health.py").read_text(encoding="utf-8")
    assert "refine_memory 在无 LLM 挡位下" in src and "有损" in src, (
        "水位告警仍在无条件推荐 refine_memory —— 它在无 LLM 时会把 20 条压成 1 句")


def test_injection_rejection_tells_the_caller_what_to_do():
    for rel in ("ducky/hot/add.py", "ducky/hot/raw_drawer.py"):
        src = (_ROOT / rel).read_text(encoding="utf-8")
        assert "AIDUMEM_INJECTION_GUARD_MODE=log_only" in src, f"{rel} 的拒绝文案没给出路"
        assert "bypass" not in src.split("Memory content rejected")[1][:400], (
            f"{rel} 暗示 /add/raw 可以绕过守卫 —— 不实")


# ── P2-18 · 召回日志不落 query 原文 ────────────────────────────────

def test_recall_info_log_uses_fingerprint_not_raw_query():
    src = (_ROOT / "ducky" / "hot" / "search.py").read_text(encoding="utf-8")
    assert "query='{req.query}'" not in src, "INFO 日志仍落用户查询原文"
    from ducky.hot.search import _query_fingerprint
    a, b = _query_fingerprint("我的密码是什么"), _query_fingerprint("我的密码是啥")
    assert len(a) == 10 and a != b and _query_fingerprint("我的密码是什么") == a


# ── P2-19 · 口令哈希文件创建即 0600 ─────────────────────────────────

def test_password_hash_file_is_created_with_0600(tmp_path, monkeypatch):
    import ducky.security.auth as auth
    target = tmp_path / "sub" / "ui_password_hash"
    monkeypatch.setattr(auth, "password_hash_path", lambda: str(target))
    assert auth.write_password_hash("not-a-real-hash", "user") is True
    if not sys.platform.startswith("win"):        # 权限位是 POSIX 语义；不 skip（不给普查添轴）
        assert target.stat().st_mode & 0o777 == 0o600
    src = _fn_source(_ROOT / "ducky" / "security" / "auth.py", "write_password_hash")
    assert "os.open(" in src and "0o600" in src.split("os.open(")[1][:120], "仍是 open(w) 再 chmod 的两步（有 0644 窗口）"


# ── P2-20 · 七循环可被叫停 ───────────────────────────────────────────

_LOOPS = (
    ("ducky/autodream.py", "autodream_background_loop"),
    ("ducky/evolve_mem.py", "evolve_background_loop"),
    ("ducky/reflect.py", "reflect_background_loop"),
    ("ducky/extended/auto_memory.py", "auto_memory_background_loop"),
    ("ducky/extended/auto_memory.py", "_auto_expire_loop"),
    ("ducky/hot/legacy_helpers.py", "_background_consolidation_loop"),
    ("ducky/hot/legacy_helpers.py", "_background_scene_cluster_loop"),
    ("ducky/speed/coalesce.py", "_coalesce_worker_loop"),   # 懒启动的第八个：pytest 会话末它还活着
)


def test_every_background_loop_sleeps_interruptibly():
    bad = []
    for rel, name in _LOOPS:
        src = _fn_source(_ROOT / rel, name)
        tree = ast.parse(src)
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "sleep" \
                    and getattr(n.func.value, "id", "") == "time":
                bad.append(f"{rel}:{name}")
        assert "_shutdown_sleep(" in src or "time.sleep" not in src, f"{rel}:{name} 没用可中断睡眠"
    assert not bad, f"这些循环仍用裸 time.sleep（停机时硬切）：{bad}"


def test_shutdown_sleep_wakes_immediately_on_request():
    from ducky import shutdown
    shutdown.SHUTDOWN.clear()
    try:
        assert shutdown.sleep(0.01) is True
        threading.Timer(0.05, shutdown.request_shutdown).start()
        t0 = time.monotonic()
        assert shutdown.sleep(5) is False
        assert time.monotonic() - t0 < 1.0, "停机请求没有立刻唤醒睡眠"
        assert shutdown.stopping() is True
    finally:
        shutdown.SHUTDOWN.clear()


def test_lifespan_requests_shutdown_after_yield_and_joins():
    src = (_ROOT / "api_server.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    ls = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_lifespan")
    yield_line = next(n.lineno for n in ast.walk(ls) if isinstance(n, ast.Yield))
    calls = {getattr(c.func, "id", getattr(c.func, "attr", "")): c.lineno for c in ast.walk(ls) if isinstance(c, ast.Call)}
    assert "request_shutdown" in calls and calls["request_shutdown"] > yield_line, "yield 之后没有叫停后台循环"
    assert "join" in calls and calls["join"] > yield_line, "没有等待循环收尾（join）"
    assert "_BACKGROUND_THREADS.append(thread)" in src, "线程没登记，join 无从下手"


# ── P2-31 · 两条生产红的用例改为自造世界（元守卫）────────────────────

def test_prod_red_cases_now_build_their_own_world():
    e2e = _fn_source(_ROOT / "tests" / "test_v20_3_e2e_smoke.py", "test_missing_config_is_warning_not_silent_pass")
    assert 'delenv("AIDUMEM_CONFIG_FILE"' in e2e, "e2e 用例仍会读到宿主 .env 的配置路径"
    cas = _fn_source(_ROOT / "tests" / "test_v19_2_security_and_consistency.py", "test_cascade_delete_all_guards")
    assert "Mem0NotConfiguredError" in cas and "get_memory" in cas, "cascade 用例仍依赖宿主的 mem0 后端状态"


# ── P2-32 · 沙箱缺席轴登记表 vs 特征识别 ─────────────────────────────

def test_sandbox_absent_axes_registry_matches_feature_detection():
    """登记表描述的是「生产沙箱」这台机器；在那台机器上跑时，特征探测必须给出同一集合。

    非沙箱形态只校验登记表 ⊆ 全轴（不 skip：不给普查添新轴）。沙箱形态识别：
    解释器前缀下有 pyvenv.cfg 且目录名为 venv（生产 venv 不带点）。
    """
    import test_v19_4_1_audit_fixes as A
    import test_v20_skip_axis_census as C
    all_axes = {a["key"] for a in C._AXES}
    registry = set(A._SANDBOX_ABSENT_AXES)
    assert registry <= all_axes, f"登记了不存在的轴：{registry - all_axes}"
    present = set(C._present_axes())
    absent_here = all_axes - present
    prefix = pathlib.Path(sys.prefix)
    in_sandbox_form = (prefix / "pyvenv.cfg").exists() and prefix.name == "venv"
    if in_sandbox_form:
        assert absent_here == registry, (
            f"生产沙箱实测缺席轴 {sorted(absent_here)} ≠ 登记表 {sorted(registry)} —— 世界模型又落后了")
    else:
        assert registry <= all_axes  # 开发机：特征识别结论仅供人读


# ── P2-34 · 搜索错误信封 ─────────────────────────────────────────────

def test_error_envelope_marks_retryable_honestly():
    from ducky.api_errors import error_envelope, is_retryable
    env = error_envelope(TimeoutError("read timed out"))
    assert set(env) == {"detail", "error_code", "retryable"}
    assert env["error_code"] == "TimeoutError" and env["retryable"] is True
    assert is_retryable(sqlite3.OperationalError("database is locked")) is True
    bad = error_envelope(ValueError("bad param"))
    assert bad["retryable"] is False and bad["error_code"] == "ValueError"
    assert "/health" in bad["detail"]


def test_search_error_paths_use_the_envelope():
    src = (_ROOT / "ducky" / "hot" / "search.py").read_text(encoding="utf-8")
    assert src.count("**error_envelope(e)") == 2, "搜索的两条 200+error 返回没都走信封"


# ── 文档面 ────────────────────────────────────────────────────────────

def test_troubleshooting_covers_the_new_refusal_codes():
    t = (_ROOT / "TROUBLESHOOTING.md").read_text(encoding="utf-8")
    for code in ("no_credential_public_access_denied", "host_not_allowed", "cross_site_write_refused", "AIDUMEI_TRUSTED_HOSTS"):
        assert code in t, f"TROUBLESHOOTING 没有 {code} 的条目"


def test_operations_has_base_upgrade_ritual_and_env_example_documents_new_vars():
    assert "Base upgrade ritual" in (_ROOT / "docs" / "OPERATIONS.md").read_text(encoding="utf-8")
    env = (_ROOT / ".env.example").read_text(encoding="utf-8")
    for k in ("AIDUMEI_TRUSTED_HOSTS", "AIDUMEI_SALIENCE_HALF_LIFE_DAYS", "AIDUMEI_SALIENCE_FLOOR"):
        assert k in env, f".env.example 没有 {k}"
