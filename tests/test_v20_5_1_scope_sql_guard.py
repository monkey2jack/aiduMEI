r"""v20.5.1 守卫：作用域 SQL 手拼点棘轮普查（T-05 的另一半）。

**守的是什么**：本仓的租户作用域谓词只能出自三处正规入口
（``bank_contract.scope_predicate`` / ``legacy_fact_scope_predicate`` /
``facts_recall.tenant_clause``，统一门面 ``ducky/scope_sql.scope_clause``）。
历史上「子句算出来没拼进 SQL」（v20.2.4 假修复）、「手抄契约层 SQL 连注释
一起抄歪」（wal_engine 注释自承）都出自手拼形态。本守卫不追求一夜清零，
追求**只减不增**：

- 名单外的文件出现作用域片段 → 红（新增手拼点）；
- 名单内的文件片段数**变多** → 红；
- 变少 → 绿，且应顺手把基线数改小（棘轮只往一个方向走）。

扫描口径：AST 取字符串常量，**排除 docstring**（文档里谈论 ``user_id=?``
不是拼 SQL）；命中正则 ``(user_id|bank_id|agent_id|source)\s*=\s*\?`` 计一处。
同一文件里注释掉的历史 SQL 若被删，计数自然下降 —— 这正是鼓励的方向。

判据设计沿用 test_v20_legacy_alias_guard.py 的约定：名单每项必须带
「为什么这里允许手拼 + 何时可删」的理由，射程有地板，理由不许烂掉。
"""

import ast
import pathlib
import re

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

_SKIP_PARTS = frozenset({
    "__pycache__", ".venv", "venv", "node_modules", ".git",
    "build", "dist", ".eggs", ".pytest_cache", "backups", ".upgrade-artifacts",
})

# 射程地板：ducky/ 与两个入口的 .py 总量低于此数 = 扫描范围缩水，守卫假绿。
_FILE_COUNT_FLOOR = 80

_SCOPE_FRAGMENT_RE = re.compile(r"(?:user_id|bank_id|agent_id|source)\s*=\s*\?")

# ── 棘轮基线（2026-09-11 实测，扫描口径见本文件 docstring）────────────
# 每项：相对路径 → (当前片段数, 理由)。数字只许改小，改大即红。
# 通用收敛方向：凡「精确匹配但手拼」的存量，分批迁往 scope_clause()；
# 凡「语义与正规谓词不同」的，必须已在该文件内有注释说明分歧原因。
_BASELINE: dict[str, tuple[int, str]] = {
    "ducky/wal_engine.py": (21, "删除链多为 IN(?) 精确匹配；作用域枚举已委托 legacy_fact_scope_predicate；存量片段随 cascade 重构分批收敛"),
    "ducky/verbatim_vault.py": (18, "全文件一律精确 user_id+bank_id（:665 注释钉死语义）；_delete_turn_ids 已迁 scope_clause（v20.5.1），余量待分批迁移"),
    "ducky/checkpoint.py": (12, "checkpoint 库不在二维租户轴上（台账已公开的设计裁决）；残留片段多为内部台账键，需逐处甄别后决定上轴或注释豁免"),
    "ducky/text_fts.py": (12, "FTS 触发器/镜像表 SQL；作用域由调用侧保证，片段为精确匹配；待评估是否改由 scope_clause 统一出口"),
    "ducky/tombstone.py": (12, "墓碑精确删除，user_id 精确匹配；待迁 canonical"),
    "ducky/refine_memory.py": (11, ":133-149 注释已说明与 tenant_clause 的刻意分歧（source 过滤语义）；_half_scope_error 已堵半作用域；维持豁免"),
    "ducky/tree_memory.py": (10, "知识树内部表，精确匹配；待迁 canonical"),
    "ducky/hot/legacy_routes.py": (9, "读路径已用 tenant_clause；片段为 upsert 精确匹配键；维持"),
    "ducky/conflict_resolver.py": (8, "部分委托 legacy_fact_scope_predicate；剩余手拼为 T-05 首批迁移对象"),
    "ducky/idempotency.py": (8, "幂等键表，精确匹配；待迁 canonical"),
    "ducky/persona_memory.py": (7, "persona 库不在二维租户轴上（台账公开裁决）；需逐处甄别"),
    "ducky/hot/legacy_helpers.py": (6, "legacy 辅助，精确匹配；待迁"),
    "ducky/memory_types.py": (6, "类型账本带作用域列，精确匹配；待迁"),
    "ducky/core_memory.py": (5, "core memory 精确匹配；待迁"),
    "ducky/federation/writer.py": (4, "联邦写入精确匹配键；v20.5.0 刚修过谱系，动它须格外小心"),
    "ducky/pipeline/memory_workspace.py": (4, "工作区装配精确匹配；待迁"),
    "ducky/federation/registry.py": (3, "注册表 agent_id 主键精确匹配，非租户轴；豁免"),
    "ducky/governance.py": (3, "治理表精确匹配；待迁"),
    "ducky/reflect.py": (4, "反思记录精确匹配；待迁"),
    "ducky/dual_index.py": (2, "双索引内部键；待甄别"),
    "ducky/event_ledger.py": (2, "事件账本为系统级全局表（有意不分租户轴），片段为自身主键；豁免并注释"),
    "ducky/federation/dedup.py": (2, "去重精确匹配；待迁"),
    "ducky/hot/crud.py": (2, "crud 精确匹配键；待迁"),
    "ducky/hot/raw_drawer.py": (2, "原文抽屉精确匹配；待迁"),
    "ducky/routes_obsidian.py": (2, "Obsidian 集成路由精确匹配；待迁"),
    "ducky/routes_p1.py": (2, "P1 路由精确匹配；待迁"),
    "ducky/salience/core.py": (2, "显著性表精确匹配；待迁"),
    "ducky/federation/broadcast.py": (1, "广播表自身键；豁免"),
    "ducky/federation/schema.py": (1, "schema 迁移 DDL 内片段；豁免"),
    "ducky/self_edit.py": (1, "自编辑精确匹配；待迁"),
}

# 正规入口自身，不参与普查（片段本来就是它们的职责）。
_CANONICAL_MODULES = frozenset({
    "ducky/bank_contract.py",
    "ducky/facts_recall.py",
    "ducky/scope_sql.py",
})


def _iter_py_files():
    for base in ("ducky",):
        for p in sorted((_REPO_ROOT / base).rglob("*.py")):
            if set(p.parts) & _SKIP_PARTS:
                continue
            yield p
    for name in ("api_server.py", "mcp_server.py", "mem0_sync.py"):
        p = _REPO_ROOT / name
        if p.exists():
            yield p


def _strip_docstrings(tree: ast.AST) -> None:
    """就地摘除 docstring 节点（模块/类/函数的首个字符串表达式）。"""
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                first.value.value = ""


def scope_fragment_census() -> dict[str, int]:
    """{相对路径: 片段数}。只含命中 >0 的文件。"""
    out: dict[str, int] = {}
    for p in _iter_py_files():
        rel = p.relative_to(_REPO_ROOT).as_posix()
        if rel in _CANONICAL_MODULES:
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        _strip_docstrings(tree)
        n = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                n += len(_SCOPE_FRAGMENT_RE.findall(node.value))
        if n:
            out[rel] = n
    return out


class TestScopeFragmentRatchet:
    def test_census_has_floor(self):
        files = list(_iter_py_files())
        assert len(files) >= _FILE_COUNT_FLOOR, (
            f"射程缩水：只扫到 {len(files)} 个文件（地板 {_FILE_COUNT_FLOOR}）"
        )

    def test_no_new_handrolled_files(self):
        new_files = sorted(set(scope_fragment_census()) - set(_BASELINE))
        assert not new_files, (
            "出现新的作用域手拼文件："
            + ", ".join(new_files)
            + "。请改用 ducky/scope_sql.py 的 scope_clause()；"
            "确属特例才把文件加进 _BASELINE 并写明理由。"
        )

    def test_counts_only_shrink(self):
        census = scope_fragment_census()
        grown = {
            rel: (was, now)
            for rel, (was, _why) in _BASELINE.items()
            if (now := census.get(rel, 0)) > was
        }
        assert not grown, (
            "作用域手拼点变多了（棘轮只许向减少方向走）："
            + "; ".join(f"{rel} {was}→{now}" for rel, (was, now) in grown.items())
        )

    def test_baseline_does_not_rot(self):
        for rel, (_n, why) in _BASELINE.items():
            assert why and "TODO" not in why, f"{rel} 的基线理由缺失或含 TODO"
        for rel in _BASELINE:
            assert (_REPO_ROOT / rel).exists(), f"基线名单里的 {rel} 已不存在，请同步清理名单"


class TestScopeClauseBuilder:
    """统一入口自身的契约。"""

    def test_canonical_default(self):
        from ducky.scope_sql import scope_clause

        sql, params = scope_clause()
        assert sql.startswith(" AND ") and params, "canonical 形态必须带 AND 前缀与参数"

    def test_unknown_flavor_rejected(self):
        from ducky.scope_sql import scope_clause

        with pytest.raises(ValueError):
            scope_clause(flavor="everything")  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad", ["a;DROP", "1", "x y", "f--"])
    def test_alias_injection_rejected(self, bad):
        from ducky.scope_sql import scope_clause

        with pytest.raises(ValueError):
            scope_clause(alias=bad)

    def test_unknown_dimension_rejected(self):
        from ducky.bank_contract import BankScopeError
        from ducky.scope_sql import scope_from_mapping

        with pytest.raises(BankScopeError):
            scope_from_mapping({"user_id": "u", "tenant": "t"})

    def test_mapping_roundtrip(self):
        from ducky.scope_sql import scope_from_mapping

        scope = scope_from_mapping({"user_id": "dudu", "bank_id": "vault"})
        assert scope.user_id == "dudu" and scope.bank_id == "vault"
