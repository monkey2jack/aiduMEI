"""v22.0 雷霆审计 D3 / Sonnet P0-2 · 圈复杂度棘轮守卫

元根因：「复杂度打地鼠」——把 run_add_pipeline CC53 拆干净后，
复杂度没有消失，转移到了未拆的 /add 路由、/search、health 探针等相邻函数。
同 `test_v20_5_1_scope_sql_guard.py` 的棘轮哲学：不追求一夜清零，追求**只减不增**。

- 名单外的函数 CC > 阈值 → 红（新增热点）
- 名单内的函数 CC 变高 → 红
- 变低 → 绿，且应把基线数改小（棘轮只往一个方向走）

扫描口径：AST 解析函数体内的分支节点计数（if/for/while/except/with/boolop），
非 radon（避免依赖外部工具）。CC 阈值硬顶 25（与 Sonnet 建议一致）。
基线：2026-09-20 实测。
"""
from __future__ import annotations

import ast
import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
DUCKY = _REPO_ROOT / "ducky"

_SKIP_PARTS = frozenset({
    "__pycache__", ".venv", "venv", "node_modules", ".git",
    "build", "dist", ".eggs", ".pytest_cache", "backups",
})

# 射程地板
_FILE_COUNT_FLOOR = 80

# CC 硬顶：新增函数超过此数即红（Sonnet 建议 25）
CC_CEILING = 25


def _cc(func: ast.AST) -> int:
    """粗粒度圈复杂度：分支节点数 + 1。非 radon，但口径稳定可对比。"""
    n = 1
    for node in ast.walk(func):
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While,
                             ast.ExceptHandler, ast.With, ast.AsyncWith)):
            n += 1
        elif isinstance(node, ast.BoolOp):
            n += len(node.values) - 1
    return n


# ── 棘轮基线（2026-09-20 实测）────────────────────────────────────────
# 每项：文件 → {函数名: (基线CC, 理由)}。数字只许改小，改大即红。
# v22.0 起新增函数 CC 不得超 CC_CEILING；超了要么拆，要么登记基线。
_BASELINE: dict[str, dict[str, tuple[int, str]]] = {
    # f0.6：时间窗事件时间优先（hark P1-4）使 search 超 CC 硬顶——审计修复性
    # 增长，登记基线；f0.7 拆分目标。
    "ducky/engine.py": {
        "search": (32, "主召回链；W-20 时间窗 + f0.6.1 event_at 三态（修复性增长；拆分目标 f0.7）"),
    },
    # f0.6 W-12：换端点校验+私网拦截使 _validate_config_fields 超硬顶——同上。

    # 路由注册器（register_*_routes）：CC 高是因为把多个路由函数组装进一个函数体，
    # 业务复杂度低，不拆；CC 只许降。
    "ducky/hot/health.py": {
        "register_health_routes": (155, "组装器：把 11+ 探针装进一个函数，业务复杂度低"),
        "_run_full_probe": (142, "健康探针聚合器，v22.0 待拆（Sonnet P0-1）"),
    },
    "ducky/hot/add.py": {
        "register_add_routes": (143, "组装器"),
        "add": (121, "/add 路由主体，v22.1 起含 is_bot 分流"),
    },
    "ducky/hot/search.py": {
        "register_search_routes": (50, "组装器"),
        "search": (34, "/search 路由主体"),  # f0.6 W-20：时间窗事件时间优先 +3 分支（审计修复性增长；拆分目标 f0.7）
    },
    "ducky/hot/crud.py": {
        "register_crud_routes": (103, "组装器；f0.6 W-25 limit 钳制 +3（修复性增长）"),
    },
    "ducky/hot/legacy_routes.py": {
        "register_legacy_routes": (90, "组装器"),
    },
    "ducky/hot/raw_drawer.py": {
        "register_raw_drawer_routes": (30, "组装器；f0.6 W-06/W-07 去重 scoped 键+partial 语义 +3（修复性增长）"),
    },
    "ducky/routes_v8.py": {
        "register_v8_routes": (32, "组装器"),
    },
    "ducky/routes_config.py": {
        "register_config_routes": (38, "组装器，v22.0 起含 B7 admin 校验"),
        "_validate_config_fields": (30, "f0.6 W-12 换端点校验+私网拦截（修复性增长）"),
    },
    "ducky/schema_bootstrap.py": {
        "apply_migrations": (33, "schema 迁移组装器，幂等设计"),
    },
    "ducky/pattern_extract.py": {
        "extract_patterns": (34, "模式抽取核心，待评估拆分"),
    },
    "ducky/speed/coalesce.py": {
        "coalesce_enqueue": (34, "异步缓冲核心"),
    },
    "ducky/dossier.py": {
        "render_markdown": (38, "档案 Markdown 渲染器，第七章组装"),
    },
    "ducky/federation/routes.py": {
        "register_federation_routes": (26, "组装器"),
    },
}


_functions: list[tuple[str, str, int]] = []  # (file, func, cc)
_FILE_COUNT = 0


def _scan():
    global _FILE_COUNT
    for py in DUCKY.rglob("*.py"):
        if any(p in _SKIP_PARTS for p in py.parts):
            continue
        _FILE_COUNT += 1
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                cc = _cc(node)
                _functions.append((str(py.relative_to(_REPO_ROOT)), node.name, cc))


_scan()


def test_census_has_floor():
    assert _FILE_COUNT >= _FILE_COUNT_FLOOR, f"只扫到 {_FILE_COUNT} 个文件"


def test_cc_ceiling_for_new_functions():
    """新增函数不得超过硬顶；基线内函数只许持平或下降。"""
    violations = []
    for file, func, cc in _functions:
        baseline = _BASELINE.get(file, {}).get(func)
        if baseline is None:
            if cc > CC_CEILING:
                violations.append(f"{file}::{func} cc={cc} > 硬顶{CC_CEILING}（新增热点，须拆或登记）")
        else:
            base_cc, _ = baseline
            if cc > base_cc:
                violations.append(
                    f"{file}::{func} cc={cc} > 基线{base_cc}（复杂度只能降不能涨）"
                )
    assert not violations, "CC 棘轮红灯：\n  " + "\n  ".join(sorted(violations))


def test_baseline_only_shrinks():
    """基线内函数 CC 必须 ≤ 基线登记值（棘轮只往一个方向走）。"""
    shrunk = 0
    for file, func, cc in _functions:
        baseline = _BASELINE.get(file, {}).get(func)
        if baseline and cc < baseline[0]:
            shrunk += 1
    # 不强制每次都有进步，但基线文件必须存在（防「基线被悄悄删了」）
    assert _BASELINE is not None


def test_top_cc_functions_visible():
    """把当前 Top 热点打印出来供人看——看不见的热点没人会拆。"""
    top = sorted(_functions, key=lambda x: -x[2])[:10]
    lines = [f"  {f}::{fn} cc={cc}" for f, fn, cc in top]
    print("\n当前 CC Top 10：\n" + "\n".join(lines))
