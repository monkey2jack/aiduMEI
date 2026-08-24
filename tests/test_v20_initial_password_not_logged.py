"""tests/test_v20_initial_password_not_logged.py — v20 P1-7：初始口令不许进日志

fable5 H5：`api_server.py` 首启时若未配置 `AIDUMEM_UI_PASSWORD`，会
`secrets.token_urlsafe(12)` 生成一个随机口令，然后**把明文打进 warning 日志**。

这条缺陷不需要推演，它在本套件自己的一次运行里就现形了 —— 当时某条无关用例的
`Captured log call` 段里躺着一行：

    🔐 [安全加固] 未配置 AIDUMEM_UI_PASSWORD，已自动生成随机控制台初始口令: y-2BDHvYZAD1aLkn …

也就是说：一条可直接登录控制台的凭据，随着启动日志进了 journald、进了 logrotate
归档、也会进任何一次「把启动日志贴给我看」。**日志的读者范围永远大于口令的读者
范围**，这条路一旦走了就收不回来。

整改：明文只写进 `<DATA_DIR>/.ui_initial_password`（0600），日志只留路径。

跑法：cd <仓库根> && .venv/bin/pytest tests/test_v20_initial_password_not_logged.py -v
"""
from __future__ import annotations

import ast
import importlib
import logging
import os
import pathlib
import stat
import sys

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import ducky.security.auth as auth_mod  # noqa: E402

_ROOT = pathlib.Path(_REPO_ROOT)


@pytest.fixture
def _fresh_datadir(tmp_path, monkeypatch):
    """把两个口令文件都指进一个空的临时目录，模拟「首次启动」。"""
    hash_file = tmp_path / ".ui_password_hash"
    plain_file = tmp_path / ".ui_initial_password"
    monkeypatch.setattr(auth_mod, "password_hash_path", lambda: str(hash_file))
    monkeypatch.setattr(auth_mod, "initial_password_path", lambda: str(plain_file))
    monkeypatch.delenv("AIDUMEM_UI_PASSWORD", raising=False)
    return hash_file, plain_file


def _ensure():
    """拿到 api_server._ensure_ui_password（导入放在用例内，避免污染收集期）。"""
    api_server = importlib.import_module("api_server")
    return api_server._ensure_ui_password


# ═══════════════ ① 正向：口令能用，但日志里没有它 ═══════════════

def test_generated_password_lands_in_a_0600_file_and_never_in_the_log(
        _fresh_datadir, caplog):
    """★ 核心断言：明文进文件、不进日志，且那份明文**真的能登录**。

    「不进日志」单独立不住 —— 如果口令压根没生成、或者生成了但没落盘，日志里
    自然也不会有它，一条空转的绿灯。所以这里同时咬住三件事：
      · 文件存在且权限恰好 0600；
      · 文件里那串东西过得了 `check_ui_password`（证明它是**真的那个口令**）；
      · 所有日志记录里都不含它。
    """
    hash_file, plain_file = _fresh_datadir

    with caplog.at_level(logging.DEBUG):
        _ensure()()

    assert plain_file.exists(), "初始口令没有落盘 —— 部署方无从取用"
    mode = stat.S_IMODE(plain_file.stat().st_mode)
    assert mode == 0o600, f"明文口令文件权限是 {oct(mode)}，必须是 0o600"

    pwd = plain_file.read_text(encoding="utf-8").strip()
    assert pwd, "口令文件是空的"
    assert auth_mod.check_ui_password(pwd), (
        "文件里那串东西登录不了 —— 那这条用例守的就不是真口令，绿得没有意义"
    )

    leaked = [r.getMessage() for r in caplog.records if pwd in r.getMessage()]
    assert not leaked, (
        f"口令明文出现在 {len(leaked)} 条日志里 —— 这正是 fable5 H5 的形态：\n  "
        + "\n  ".join(leaked)
    )
    assert any(str(plain_file) in r.getMessage() for r in caplog.records), (
        "日志里连文件路径都没留 —— 部署方不知道去哪儿取口令，等于换了个方式失联"
    )


# ═══════════════ ② 负向对照：落盘失败不许锁死控制台 ═══════════════

def test_plaintext_write_failure_leaves_no_active_password(_fresh_datadir, caplog):
    """★ 顺序的理由：明文落不下去时，**哈希也不许落**。

    反过来写（先哈希后明文）的后果是：口令已经生效，但没有任何人知道它是什么 ——
    控制台当场锁死，且下次启动因为「哈希文件已存在」不会再重试。
    这条断言把这个顺序焊住。
    """
    hash_file, plain_file = _fresh_datadir
    monkeypatch_target = auth_mod

    def refuse(_pwd):
        return False

    orig = monkeypatch_target.write_initial_password
    monkeypatch_target.write_initial_password = refuse
    try:
        with caplog.at_level(logging.DEBUG):
            _ensure()()
    finally:
        monkeypatch_target.write_initial_password = orig

    assert not hash_file.exists(), (
        "明文落盘失败了却把哈希写了下去 —— 口令已生效但无人知晓，控制台锁死，"
        "而且下次启动会因为哈希文件已存在而不再重试"
    )
    errs = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert errs, "落盘失败一声不响 —— 首启失去唯一一次自愈机会却没人知道"
    assert any("AIDUMEM_UI_PASSWORD" in m for m in errs), (
        f"报错没告诉运维下一步该干什么（显式设置环境变量）：{errs}"
    )


def test_failure_path_still_refuses_to_log_the_plaintext(_fresh_datadir, caplog):
    """负向对照的负向对照：落盘失败时也不许「兜底打一下明文」。

    这是最容易被下一个人「顺手改善」掉的一处：既然文件写不成，那就打日志让人
    看得到嘛 —— 那正好把 P1-7 原样恢复。判据是 AST 级的（见下一条用例），
    这里再补一条运行时的：失败路径的日志里不许出现任何长得像 token_urlsafe(12)
    的串。
    """
    import re

    hash_file, plain_file = _fresh_datadir
    orig = auth_mod.write_initial_password
    auth_mod.write_initial_password = lambda _p: False
    try:
        with caplog.at_level(logging.DEBUG):
            _ensure()()
    finally:
        auth_mod.write_initial_password = orig

    # token_urlsafe(12) → 16 个 [A-Za-z0-9_-] 字符
    suspicious = []
    for r in caplog.records:
        msg = r.getMessage()
        for m in re.finditer(r"(?<![A-Za-z0-9_\-/.])[A-Za-z0-9_-]{16}(?![A-Za-z0-9_\-/.])", msg):
            suspicious.append((m.group(0), msg))
    assert not suspicious, (
        "失败路径的日志里出现了长得像自动生成口令的串：\n  "
        + "\n  ".join(f"{tok} ← {msg}" for tok, msg in suspicious)
    )


# ═══════════════ ③ 射程守卫：新增的日志点也不许泄 ═══════════════

def test_no_logging_call_in_api_server_takes_the_generated_password():
    """★ 元测试：语法树里不许有任何 `logger.*(… gen_pwd …)`。

    上面两条是运行时判据，只覆盖**被执行到的**那几条日志。这条覆盖**写下来的
    全部**日志点：以后谁在别的分支里再加一句「顺便打一下方便排查」，这里当场红。

    判据用 AST 不用字符串：注释里正当地写着「原先是 `logger.warning(… gen_pwd …)`」，
    字符串级判据分不清「代码在做这件事」和「注释在说这件事已经不做了」。
    """
    src = (_ROOT / "api_server.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    LOG_METHODS = {"debug", "info", "warning", "error", "critical", "exception", "log"}
    leaks = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in LOG_METHODS:
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            for inner in ast.walk(arg):
                if isinstance(inner, ast.Name) and inner.id == "gen_pwd":
                    leaks.append(node.lineno)

    assert not leaks, (
        f"api_server.py 有日志调用把生成的口令当实参传出，行号 {leaks} —— "
        "明文口令只许落 0600 文件，日志里只许出现路径（fable5 H5）"
    )

    # 正向对照：这条守卫必须确实在看一棵有日志调用的树，否则它守的是空气
    log_calls = [
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr in LOG_METHODS
    ]
    assert len(log_calls) > 20, (
        f"api_server.py 里只解析出 {len(log_calls)} 处日志调用 —— 太少了，"
        "八成是 AST 判据没匹配上真实写法，这条守卫正在守空气"
    )


def test_plaintext_file_is_created_with_0600_from_the_start(tmp_path, monkeypatch):
    """★ 权限窗口：文件必须**建出来就是** 0600，不是先 0644 再 chmod。

    `open()` 之后 `os.chmod` 之间有一个「内容已写、权限还是默认」的窗口，同机
    任何用户都能在那一瞬间读到明文。这条用例把 umask 故意放到最松（0o000，
    默认权限就是 0666），如果实现走的是「先建再 chmod」，创建瞬间的模式就会是
    0666 —— 用一个 `os.open` 的替身把创建时的 mode 抓出来验。
    """
    target = tmp_path / ".ui_initial_password"
    monkeypatch.setattr(auth_mod, "initial_password_path", lambda: str(target))

    seen = {}
    real_open = os.open

    def spy_open(path, flags, mode=0o777, *a, **kw):
        if str(path) == str(target):
            seen["flags"] = flags
            seen["mode"] = mode
        return real_open(path, flags, mode, *a, **kw)

    monkeypatch.setattr(os, "open", spy_open)
    old_umask = os.umask(0o000)
    try:
        assert auth_mod.write_initial_password("pw-under-loose-umask") is True
    finally:
        os.umask(old_umask)

    assert seen, (
        "没有观察到对目标文件的 os.open —— 实现可能走的是内建 open()，"
        "那就存在「先 0644 再 chmod」的明文可读窗口"
    )
    assert seen["mode"] == 0o600, (
        f"创建时传的 mode 是 {oct(seen['mode'])}，必须是 0o600"
    )
    assert seen["flags"] & os.O_CREAT, "没带 O_CREAT，抓到的不是创建那一次"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600, (
        "最松 umask 下最终权限不是 0600 —— 补的那一刀 chmod 也没兜住"
    )
