"""tests/test_f05_posix_import_guard.py — f0.5 Windows 顶层裸 POSIX import 守卫

issue #31 / Z指导复测 6.1 的守卫化：ducky/mutation_journal.py:14 顶层裸
`import fcntl` 曾让 `import ducky` 在 Windows 直接 ModuleNotFoundError，
且它是热路径必经模块（__init__ → layer1_selfcheck → mutation_journal）。
修法是 try/except ImportError 降级（与 llm_client.py / resource_probe.py
同款范式）；本守卫把「不得新增顶层裸 POSIX import」升级为结构性红线。

方法论与 test_v20_1_1_source_guards.py 同源：**AST 机制盲扫描 + 显式登记**
（判据用 AST 不用字符串——注释/docstring 里写着 import 不算）。
- 模块顶层裸 POSIX import（Module.body 直接子级）在登记表之外 → 红
- 登记为 guard 的位点实际没有 try/except ImportError 守卫（死登记）→ 红
- 函数内延迟 import / try 内 / platform 分支内不算顶层，不红
"""
from __future__ import annotations

import ast
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

_REPO = pathlib.Path(__file__).resolve().parent.parent

# 顶层裸 import 即崩的 POSIX-only 模块（Windows 无这些包）
_POSIX_ONLY_MODULES = {"fcntl", "resource", "pwd", "grp", "termios", "syslog", "pty", "daemon", "spwd"}

# 登记表：允许且已按下述形态合规的文件 → 该文件顶层允许出现的 POSIX 模块集合。
# 合规形态由本守卫逐文件 AST 核验（两条断言见下），登记≠免检。
_ALLOWED = {
    "ducky/llm_client.py": {"fcntl"},
    "ducky/resource_probe.py": {"resource"},
    "ducky/mutation_journal.py": {"fcntl"},   # f0.5 修复后形态
    "ducky/wal_engine.py": {"fcntl", "msvcrt"},      # try 内 + Windows 分支
    "ducky/process_lock.py": {"fcntl", "msvcrt"},    # os.name 分支内
}

# 产品代码扫描范围：ducky/ 与 integrations/（scripts/ 是 POSIX-only 运维
# 脚本、函数内延迟 import，另外在 README 平台口径注明，不在本守卫射程）。
_SCAN_DIRS = ("ducky", "integrations")


def _collect_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for d in _SCAN_DIRS:
        files.extend((_REPO / d).rglob("*.py"))
    return sorted(files)


def _top_level_bare_posix_imports(tree: ast.Module) -> dict[str, int]:
    """{module: lineno} — Module.body 直接子级里的 POSIX-only 裸 import。

    try/if 包裹的 import 不是 body 直接子级（那是复合语句的内部），天然不算。
    """
    found: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _POSIX_ONLY_MODULES:
                    found.setdefault(root, node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in _POSIX_ONLY_MODULES:
                found.setdefault(node.module.split(".")[0], node.lineno)
    return found


def _has_import_error_guard(tree: ast.Module, module: str) -> bool:
    """文件里是否存在 `try: import <module> … except ImportError` 的 AST 形态。"""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        tried = {a.name.split(".")[0] for s in node.body if isinstance(s, ast.Import) for a in s.names}
        if module not in tried:
            continue
        for handler in node.handlers:
            if handler.type is None:
                return True  # 裸 except 也算兜住
            if "ImportError" in ast.unparse(handler.type):
                return True
        return False
    return False


# ════════════════════════════════════════════════════════════════
# 断言一：全产品树顶层裸 POSIX import 必须落在登记表内
# ════════════════════════════════════════════════════════════════
def test_no_new_top_level_bare_posix_imports():
    violations = []
    for path in _collect_files():
        rel = path.relative_to(_REPO).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        allowed = _ALLOWED.get(rel, set())
        for module, lineno in _top_level_bare_posix_imports(tree).items():
            if module not in allowed:
                violations.append(f"{rel}:{lineno} 顶层裸 import {module}"
                                  f"（Windows 上 import 即崩；修法见 mutation_journal.py "
                                  f"try/except ImportError 范式，或该位点登记进 _ALLOWED）")
    assert not violations, "新增顶层裸 POSIX import（order #31 复发）:\n" + "\n".join(violations)


# ════════════════════════════════════════════════════════════════
# 断言二：登记为「顶层允许」的 guard 位点必须真有守卫形态（死登记也是账）
# ════════════════════════════════════════════════════════════════
def test_registered_guards_actually_guarded():
    dead = []
    for rel, modules in sorted(_ALLOWED.items()):
        path = _REPO / rel
        if not path.exists():
            dead.append(f"{rel}: 文件不存在，登记已死（请更新 _ALLOWED）")
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        for module in sorted(modules):
            # msvcrt 走平台分支，不走 ImportError 守卫，单独放行
            if module == "msvcrt":
                continue
            top = _top_level_bare_posix_imports(tree)
            if module not in top:
                continue  # 不在顶层（可能在分支/try 内），断言一已覆盖越界检测
            if not _has_import_error_guard(tree, module):
                dead.append(f"{rel}: 顶层 {module} 无 try/except ImportError 守卫，"
                            f"登记形态不符")
    assert not dead, "守卫登记与实际形态不符:\n" + "\n".join(dead)


# ════════════════════════════════════════════════════════════════
# 负向对照：扫描器必须能抓出坏代码（否则守卫白设）
# ════════════════════════════════════════════════════════════════
def test_guard_probe_catches_bare_import():
    # ① 顶层裸 import —— 必须抓到
    bad = ast.parse("import json\nimport fcntl\nx = 1\n")
    assert "fcntl" in _top_level_bare_posix_imports(bad)
    # ② try/except ImportError 守卫 —— 不算裸
    guarded = ast.parse(
        "try:\n    import fcntl\nexcept ImportError:\n    fcntl = None\n")
    assert _top_level_bare_posix_imports(guarded) == {}
    assert _has_import_error_guard(guarded, "fcntl")
    # ③ 函数内的 import —— 不算顶层
    inner = ast.parse("def f():\n    import fcntl\n    return fcntl\n")
    assert _top_level_bare_posix_imports(inner) == {}
    # ④ 注释里写着 import fcntl —— 不算（AST 判据，不是字符串）
    comment = ast.parse("# import fcntl\nx = 1\n")
    assert _top_level_bare_posix_imports(comment) == {}
    # ⑤ try 包裹但 except 不是 ImportError（吞别的异常）—— 不认守卫
    fake = ast.parse(
        "try:\n    import fcntl\nexcept ValueError:\n    pass\n")
    assert not _has_import_error_guard(fake, "fcntl")
    # ⑥ 死登记探测：记录一个不存在的守卫
    empty = ast.parse("import fcntl\n")
    assert not _has_import_error_guard(empty, "fcntl")
