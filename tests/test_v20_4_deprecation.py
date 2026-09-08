"""
v20.4.0 守卫（六方外审整改 P1-5 / GPT P1-03）：兼容层 Deprecation 台账不得烂掉。

这条守卫防的是什么
──────────────────
GPT P1-03 实锤：本仓兼容层（旧 import 路径门面、legacy 路由）持续累积，
**没有退役政策** —— 每一处都写着「兼容」，没有一处写着「何时退役、替代品是谁」。
于是 docs/DEPRECATION.md 应运而生：五列登记（introduced_at /
last_supported_version / deprecated_at / planned_removal_version / replacement）。

但台账本身是文档，文档会烂：代码重构把模块挪走了、符号改名了，台账还在
原地指着空气 —— 这种「死台账」比没有台账更糟，它发放「已治理」的假凭证。

所以本文件把台账焊进测试：
  1. 台账每一行登记的模块**必须真实存在**（文件消失 → 红）；
  2. 台账「登记符号」列引用的每个函数/类**必须在模块里可解析**
     （AST 层面能查到定义、导入或 __all__ 登记，改名 → 红）；
  3. 外审点名的五处必须全部在册（漏登记 → 红）。

为什么用 AST 而不是 import：部分模块导入即触发重依赖（向量库、配置装载），
守卫只关心「符号在不在」，不关心「能不能跑」，静态解析最诚实也最稳。
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_LEDGER = _REPO_ROOT / "docs" / "DEPRECATION.md"

# 外审 GPT P1-03 实锤点名的五处。少任何一处，本守卫必须红 ——
# 防「修台账的方式是把难写的行删掉」。
_REQUIRED_MODULES = {
    "ducky/add_speed.py",
    "ducky/routes_core.py",
    "ducky/hot/legacy_routes.py",
    "ducky/memory_salience.py",
    "ducky/extended/",
}

# 台账表格的列序。改动表头而忘记改这里 → 红，这也是有意的：
# 表格结构是台账与守卫之间的契约，不许悄悄漂移。
_EXPECTED_HEADER = [
    "模块", "角色", "introduced_at", "last_supported_version",
    "deprecated_at", "planned_removal_version", "replacement", "登记符号",
]


def _parse_ledger() -> list[dict[str, str]]:
    """把 docs/DEPRECATION.md 的登记表格解析成行字典列表。"""
    assert _LEDGER.is_file(), "docs/DEPRECATION.md 不存在 —— P1-5 台账未建立"
    rows: list[dict[str, str]] = []
    header_seen: list[str] | None = None
    for line in _LEDGER.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if header_seen is None:
            if all(h in cells for h in _EXPECTED_HEADER):
                header_seen = cells
            continue
        if set("".join(cells)) <= {"-", " ", ":"}:
            continue  # 分隔行
        if len(cells) != len(header_seen):
            continue
        rows.append(dict(zip(header_seen, cells)))
    assert header_seen is not None, (
        "台账里找不到含全部五列（introduced_at / last_supported_version / "
        "deprecated_at / planned_removal_version / replacement）的表格"
    )
    return rows


def _module_path(cell: str) -> str:
    """模块列单元格 → 仓内相对路径（剥掉反引号）。"""
    return cell.strip("` ").rstrip("/") + ("/" if cell.rstrip("` ").endswith("/") else "")


def _resolve_module_file(dotted: str) -> pathlib.Path | None:
    """点分模块名 → 仓内文件（模块 .py 或包 __init__.py）。"""
    rel = pathlib.Path(*dotted.split("."))
    for cand in (_REPO_ROOT / rel.with_suffix(".py"),
                 _REPO_ROOT / rel / "__init__.py"):
        if cand.is_file():
            return cand
    return None


def _top_level_names(path: pathlib.Path, _seen: frozenset[str] = frozenset()) -> set[str]:
    """AST 抽取模块（或包 __init__）顶层可见名字：def / class / 赋值 / import / __all__。

    `from X import *` 递归解析 X 的顶层名字 —— 兼容门面（如 ducky/add_speed.py）
    整个命名空间就是星号导入撑起来的，不递归的话门面行永远解析不到符号。
    """
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
            # __all__ 的字面量条目也算可解析 —— 门面靠 re-export + __all__ 吃饭
            if any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
                if isinstance(node.value, (ast.List, ast.Tuple)):
                    for elt in node.value.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            names.add(elt.value)
        elif isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.names and any(a.name == "*" for a in node.names) and node.module:
                if node.module not in _seen:
                    star_file = _resolve_module_file(node.module)
                    if star_file is not None:
                        names |= _top_level_names(star_file, _seen | {node.module})
                continue
            for a in node.names:
                names.add(a.asname or a.name)
    return names


def _symbols(cell: str) -> list[str]:
    return [s.strip("` ") for s in re.split(r"[,，、]", cell) if s.strip("` ")]


def test_ledger_registers_all_five_audited_modules():
    rows = _parse_ledger()
    modules = {_module_path(r["模块"]) for r in rows}
    missing = _REQUIRED_MODULES - modules
    assert not missing, f"台账漏登记外审点名的兼容层：{sorted(missing)}"


def test_every_registered_module_exists_on_disk():
    rows = _parse_ledger()
    assert rows, "台账表格解析出 0 行 —— 空台账等于没有台账"
    for r in rows:
        mod = _module_path(r["模块"])
        target = _REPO_ROOT / mod
        assert target.exists(), (
            f"台账登记了 {mod}，但该路径已不存在 —— 台账烂掉了；"
            f"请更新台账（移除或改指新位置），而不是留着一行死登记"
        )


def test_every_registered_symbol_resolves():
    rows = _parse_ledger()
    failures: list[str] = []
    for r in rows:
        mod = _module_path(r["模块"])
        target = _REPO_ROOT / mod
        if not target.exists():
            continue  # 存在性由上一条守卫报告，这里不重复
        src_file = target / "__init__.py" if target.is_dir() else target
        names = _top_level_names(src_file)
        for sym in _symbols(r["登记符号"]):
            if sym not in names:
                failures.append(f"{mod}: 符号 {sym} 不可解析")
    assert not failures, (
        "台账登记的 legacy 符号在代码里解析不到（改名/删除后台账未跟进）：\n  "
        + "\n  ".join(failures)
    )


def test_five_columns_are_never_left_empty():
    """五列是台账的存在理由；任何一列留空（含占位横线）都等于没登记。"""
    rows = _parse_ledger()
    for r in rows:
        for col in ("introduced_at", "last_supported_version", "deprecated_at",
                    "planned_removal_version", "replacement"):
            val = r[col].strip()
            assert val and val not in {"-", "—", "?", "待定"}, (
                f"{r['模块']} 的 {col} 列为空或占位 —— "
                f"找不到依据就诚实写「≥vX（首次出现于…）」，不许留空"
            )
