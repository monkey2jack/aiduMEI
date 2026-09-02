"""tests/test_v20_1_1_source_guards.py — v20.1.1 源码守卫三连

外审复核采纳项的守卫化：把「当前安全靠人工纪律」升级为「结构性安全」。
三条守卫共用同一方法论（与跳过轴普查同源）：**机制盲扫描 + 显式登记**，
新增位点不登记即红，登记位点消失也红（死文案同样是账）。

  N-3 前端 innerHTML 拼接守卫 —— 外审指控 XSS 经 58 处全量审计驳回，
      但「安全靠每个作者记得包 esc()」是真的；守卫让漏包结构性变红。
  N-4 f-string SQL 插值登记 —— 外审自认「当前安全（表列名皆内部常量）」；
      守卫钉住「新插值必须过登记」，登记时人眼审一次来源。
  N-5 迁移点登记 —— ALTER/CREATE 散落各模块（additive-only 纪律下的
      有意形态）；登记清单让「谁在改 schema」有一张总账。
"""
from __future__ import annotations

import ast
import os
import pathlib
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_REPO = pathlib.Path(__file__).resolve().parent.parent


# ══════════════════════════════════════════════════════════════════
# N-3 · 前端 innerHTML 拼接守卫
# ══════════════════════════════════════════════════════════════════
# 判据：innerHTML 赋值语句中以 `+` 拼进 HTML 的每个表达式，必须是
#   ① 字符串/数字字面量或纯数字运算；
#   ② 安全包装调用（esc/格式化函数——只产数字或已转义文本）；
#   ③ 已审计的内部渲染函数（其实现内部逐字段 esc，2026-08-26 全量
#      人工审计在案，见《v20.1 公开后外审复核》）。
# 新增直拼变量不在名单 → 红。误报的出路是审计后进名单，不是删守卫。

_FRONTEND_FILES = ("frontend/js/panels.js", "frontend/js/main.js", "frontend/js/api.js")

_SAFE_CALL_PREFIXES = (
    "esc(", "fmtInt(", "fmtCompact(", "fmtWhen(", "String(", "Number(",
    "secHead(", "layerRow(", "failure(", "loading(", "barChart(",
    "renderModelConfig(", "renderReasoning(", "renderParams(",
)
# 已审计的 .map(渲染函数) 白名单：这些行渲染函数内部逐字段 esc。
_AUDITED_MAP_RENDERERS = {"recordRow", "factRow", "tombRow", "candRow"}
# 已审计的特例表达式（逐条人工核过来源为安全值；表达式原文精确匹配）：
_AUDITED_EXPRESSIONS = {
    "kTree.domains",            # /api/knowledge-tree 的域计数，数字
    "k.domains",                # 同上
    "modOn", "modAll",          # 模块计数，数字
    "list.slice(0, 24)",        # 后接 .map(fn)，fn 内部 esc(c.category)
    "list.slice(0, 24).map",    # 同上（解析近似的另一截断形态）
    "recs.map(recordRow)",      # 审计过的渲染函数
    "rows.map(tombRow)",
    "rows.map(candRow)",
    "facts.map(factRow)",
    "adj.map",                  # 内联函数体内逐字段 esc（L1000 段人工审计）
    "cats.map",                 # 同上
    "Object.entries(modules)",  # 后接 .map，值为内部模块注册表
    "Object.entries(modules).map",  # 同上（解析截断的另一形态）
    "palette", "stroke",        # main.js 六边形装饰，内部常量色值
    "png",                      # main.js 图标，硬编码 pngMap 的值
}


def _innerhtml_statements(src: str):
    """把跨行的 innerHTML 赋值语句拼成整句（到分号）。"""
    lines = src.split("\n")
    i = 0
    while i < len(lines):
        if "innerHTML" in lines[i] and "=" in lines[i]:
            stmt, j = lines[i], i
            while ";" not in stmt and j + 1 < len(lines):
                j += 1
                stmt += " " + lines[j].strip()
            yield i + 1, stmt
            i = j + 1
        else:
            i += 1


def _concat_expressions(stmt: str):
    rhs = stmt.split("innerHTML", 1)[1]
    return re.findall(r"\+\s*([A-Za-z_$][\w$.]*(?:\([^()]*\))?(?:\.[\w$]+(?:\([^()]*\))?)*)", rhs)


def _is_safe(expr: str) -> bool:
    e = expr.strip()
    if e in _AUDITED_EXPRESSIONS:
        return True
    # 嵌套括号会让 _concat_expressions 把 `secHead(a, fn(b))` 截成裸名
    # `secHead`——安全判据对带括号与裸名两种形态一视同仁。
    if any(e.startswith(p) or e == p.rstrip("(") for p in _SAFE_CALL_PREFIXES):
        return True
    if re.match(r"^\d", e):
        return True
    if ".toFixed(" in e or ".length" in e:
        return True
    m = re.match(r"^[\w$.]+\.map\((\w+)\)", e)
    if m and m.group(1) in _AUDITED_MAP_RENDERERS:
        return True
    return False


def test_innerhtml_concats_are_escaped_or_audited():
    violations = []
    for rel in _FRONTEND_FILES:
        src = (_REPO / rel).read_text(encoding="utf-8")
        for lineno, stmt in _innerhtml_statements(src):
            if "guard:allow-innerHTML" in stmt:
                continue  # 行内豁免必须带理由注释，代码评审可见
            for expr in _concat_expressions(stmt):
                if not _is_safe(expr):
                    violations.append(f"{rel}:{lineno} 拼接了未审计表达式 {expr!r}")
    assert not violations, (
        "innerHTML 直拼了不在安全名单的表达式 —— 用户可控内容必须过 esc()，"
        "确为安全值的请人工审计后登记进 _AUDITED_EXPRESSIONS（附来源）：\n  "
        + "\n  ".join(violations)
    )


def test_audited_renderers_still_exist_and_escape():
    """名单上的渲染函数必须还在、且函数体内确实调用 esc ——
    防止有人重写函数丢了转义而名单还在白名单里点头。"""
    src = (_REPO / "frontend/js/panels.js").read_text(encoding="utf-8")
    for fn in sorted(_AUDITED_MAP_RENDERERS):
        m = re.search(rf"function {fn}\((\w*)\)\s*{{(.*?)\n}}", src, re.S)
        assert m, f"已审计渲染函数 {fn} 消失了 —— 白名单成死文案"
        assert "esc(" in m.group(2), f"{fn} 函数体里不再调用 esc —— 审计结论失效"


# ══════════════════════════════════════════════════════════════════
# N-4 · f-string SQL 插值登记
# ══════════════════════════════════════════════════════════════════

_SQL_PHRASE_RE = re.compile(
    r"\bSELECT\b.*\bFROM\b|\bINSERT\s+(?:OR\s+\w+\s+)?INTO\b|\bUPDATE\s+\w+\s+SET\b|"
    r"\bDELETE\s+FROM\b|\bCREATE\s+(?:TABLE|INDEX|UNIQUE\s+INDEX|VIRTUAL\s+TABLE|TRIGGER)\b|"
    r"\bALTER\s+TABLE\b|\bPRAGMA\s+\w+|\bDROP\s+(?:TABLE|INDEX|TRIGGER)\b",
    re.I | re.S)

# 登记基线（2026-08-26 全量扫描 + 人工核对：全部为内部常量/DDL 片段/
# 占位符拼接，无一来自请求参数——与外审「当前安全」的判断一致）。
# 新插值出现在这张表之外 → 红。值请用 ? 参数化；表列名等结构性拼接
# 请人工核对来源后在此登记。
_EXPECTED_SQL_INTERPOLATIONS = {
    ("ducky/bank_contract.py", "column"), ("ducky/bank_contract.py", "ddl"),
    ("ducky/bank_contract.py", "table"),
    ("ducky/checkpoint.py", "placeholders"),
    ("ducky/core_memory.py", "_owner_first_order()"), ("ducky/core_memory.py", "column"),
    ("ducky/core_memory.py", "ddl"), ("ducky/core_memory.py", "guard"),
    ("ducky/core_memory.py", "table"), ("ducky/core_memory.py", "where"),
    ("ducky/event_ledger.py", "placeholders"), ("ducky/event_ledger.py", "scope_sql"),
    ("ducky/evolve_mem.py", "placeholders"), ("ducky/evolve_mem.py", "table"),
    ("ducky/extended/routes.py", "clauses"), ("ducky/extended/routes.py", "placeholders"),
    # v20.2.4（外审 F-05/F-07 scope 化）：tenant_clause 返回的**结构性**片段
    # （" AND bank_id=? AND user_id=?" 之类），值一律走 ? 参数化随 params 传。
    # 人工核对：来源是 ducky/facts_recall.tenant_clause，不含任何请求数据。
    ("ducky/extended/routes.py", "fclause"),
    ("ducky/hot/legacy_routes.py", "fclause"),
    ("ducky/facts_recall.py", "placeholders"),
    ("ducky/federation/broadcast.py", "peer_frag"), ("ducky/federation/broadcast.py", "shared_frag"),
    ("ducky/federation/dedup.py", "agent_frag"),
    ("ducky/federation/recall.py", "' AND '.join(where)"), ("ducky/federation/recall.py", "scope_frag"),
    ("ducky/federation/schema.py", "FACTS_UNIQUE_COLUMNS"), ("ducky/federation/schema.py", "column"),
    ("ducky/federation/schema.py", "ddl"), ("ducky/federation/schema.py", "table"),
    ("ducky/governance.py", "column"), ("ducky/governance.py", "ddl"),
    ("ducky/hot/legacy_helpers.py", "_SCENES_DDL_V20"), ("ducky/hot/legacy_helpers.py", "col"),
    ("ducky/hot/legacy_routes.py", "','.join('?' * len(all_ids))"),
    ("ducky/hot/legacy_routes.py", "','.join('?' * len(ids))"),
    ("ducky/hot/legacy_routes.py", "placeholders"), ("ducky/hot/legacy_routes.py", "scope_sql"),
    ("ducky/hot/legacy_routes.py", "t_clause"), ("ducky/hot/legacy_routes.py", "where"),
    ("ducky/memory_types.py", "DEFAULT_BANK_ID"), ("ducky/memory_types.py", "LEGACY_PLACEHOLDER_USER_ID"),
    ("ducky/memory_types.py", "column"), ("ducky/memory_types.py", "ddl"),
    # v20.3.2-beta（外审 P1-A）：_table_columns() 的 PRAGMA table_info({table})。
    # table 只来自本模块内的字面量 "memory_types"（唯一调用点），不接受外部输入；
    # PRAGMA 也不接受 ? 参数化。登记而非放行 —— 新增调用点会让本条守卫再红一次。
    ("ducky/memory_types.py", "table"),
    ("ducky/memory_types.py", "owner_sql"), ("ducky/memory_types.py", "placeholders"),
    ("ducky/recall_funnel.py", "placeholders"),
    ("ducky/reflect.py", "ph"),
    # v20.2.5（外审 F-03 真修）：refine 候选的 bank 收窄子句。
    # 来源核对：`_bank_clause()` 只返回两种字面量 —— "" 或 " AND bank_id=?"，
    # bank 值本身走 ? 参数（_bparams），**没有任何调用方输入进入 SQL 文本**。
    # 这正是本登记制要的那种「结构性拼接、值仍参数化」。
    ("ducky/refine_memory.py", "_bclause"),
    ("ducky/routes_p1.py", "f_owner_sql"), ("ducky/routes_p1.py", "mt_owner_sql"),
    ("ducky/salience/core.py", "placeholders"),
    ("ducky/schema_bootstrap.py", "DEFAULT_AGENT_ID"), ("ducky/schema_bootstrap.py", "DEFAULT_USER_ID"),
    ("ducky/skill_crystallizer.py", "','.join('?' * len(_EXCLUDED_CATEGORIES))"),
    ("ducky/skill_crystallizer.py", "_LOW_UTILITY_SUCCESS_RATE"),
    ("ducky/skill_crystallizer.py", "_MIN_USES_FOR_UTILITY"),
    ("ducky/skill_crystallizer.py", "col"), ("ducky/skill_crystallizer.py", "ddl"),
    ("ducky/text_fts.py", "' OR '.join(clauses)"), ("ducky/text_fts.py", "_MEMORIES_DDL"),
    ("ducky/tombstone.py", "','.join(cols)"), ("ducky/tombstone.py", "placeholders"),
    ("ducky/vector_backend.py", "marks"),
    ("ducky/verbatim_vault.py", "' OR '.join(clauses)"), ("ducky/verbatim_vault.py", "col"),
    ("ducky/verbatim_vault.py", "ddl"), ("ducky/verbatim_vault.py", "placeholders"),
    ("ducky/wal_engine.py", "ref_ph"), ("ducky/wal_engine.py", "scope_sql"),
}


def _scan_sql_interpolations():
    found = set()
    for f in sorted((_REPO / "ducky").rglob("*.py")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        rel = str(f.relative_to(_REPO))
        for node in ast.walk(tree):
            if not isinstance(node, ast.JoinedStr):
                continue
            text = "".join(c.value for c in node.values
                           if isinstance(c, ast.Constant) and isinstance(c.value, str))
            if not _SQL_PHRASE_RE.search(text):
                continue
            for c in node.values:
                if isinstance(c, ast.FormattedValue):
                    found.add((rel, ast.unparse(c.value)))
    return found


def test_fstring_sql_interpolations_are_registered():
    found = _scan_sql_interpolations()
    new = found - _EXPECTED_SQL_INTERPOLATIONS
    assert not new, (
        "出现未登记的 f-string SQL 插值 —— 值请改用 ? 参数化；确属表列名等"
        f"结构性拼接的，请人工核对来源后登记：{sorted(new)}"
    )
    vanished = _EXPECTED_SQL_INTERPOLATIONS - found
    assert not vanished, f"登记的插值位点已消失（清账，别留死文案）：{sorted(vanished)}"


# ══════════════════════════════════════════════════════════════════
# N-5 · 迁移点登记
# ══════════════════════════════════════════════════════════════════

_MIG_RE = re.compile(
    r"\b(ALTER\s+TABLE|CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?)\s+([A-Za-z_{][\w{}]*)", re.I)
# 正则回溯赝品：docstring 里「CREATE TABLE IF NOT EXISTS，对存量库是
# no-op」这类**叙述**会回溯出组2="IF"。表名命中 SQL 关键词 = 不是真
# 迁移点，跳过；f"ALTER TABLE {t} ADD…" 的 ADD 归一为 <dynamic>。
_SQL_KEYWORDS = {"IF", "NOT", "EXISTS", "SET", "OR", "COLUMN"}
_SQL_KEYWORD_TABLES = {"ADD"}

# schema 变更总账（2026-08-26 全量扫描）。additive-only 是本仓迁移纪律，
# 迁移逻辑留在各模块（幂等 ensure_*），这张表补的是「谁在改 schema」的
# 全景账——新迁移点不登记即红。
_MIGRATION_LEDGER = {
    ("ducky/autodream.py", "CREATE", "autodream_log"),
    ("ducky/bank_contract.py", "ALTER", "<dynamic>"),
    ("ducky/bank_contract.py", "CREATE", "memory_banks"),
    ("ducky/checkpoint.py", "CREATE", "checkpoints"),
    ("ducky/core_memory.py", "ALTER", "core_memory"),
    ("ducky/core_memory.py", "ALTER", "core_memory__scoped"),
    ("ducky/core_memory.py", "CREATE", "<dynamic>"),
    ("ducky/dual_index.py", "CREATE", "pending_embeddings"),
    ("ducky/event_ledger.py", "ALTER", "memory_events"),
    ("ducky/event_ledger.py", "CREATE", "memory_events"),
    ("ducky/evolve_mem.py", "CREATE", "evolve_adjustments"),
    ("ducky/evolve_mem.py", "CREATE", "evolve_feedback"),
    ("ducky/evolve_mem.py", "CREATE", "evolve_meta"),
    ("ducky/evolve_mem.py", "CREATE", "evolve_queries"),
    ("ducky/federation/schema.py", "ALTER", "facts"),
    ("ducky/federation/schema.py", "CREATE", "agents"),
    ("ducky/federation/schema.py", "CREATE", "federation_broadcast"),
    ("ducky/governance.py", "ALTER", "candidate_facts"),
    ("ducky/governance.py", "CREATE", "candidate_facts"),
    ("ducky/hot/legacy_helpers.py", "ALTER", "observations"),    ("ducky/idempotency.py", "CREATE", "idempotency_keys"),

    ("ducky/hot/legacy_helpers.py", "ALTER", "scenes"),
    ("ducky/hot/legacy_helpers.py", "CREATE", "observations"),
    ("ducky/hot/legacy_helpers.py", "CREATE", "scenes"),
    ("ducky/memory_types.py", "ALTER", "memory_types"),
    ("ducky/memory_types.py", "CREATE", "memory_types"),
    ("ducky/opinion.py", "ALTER", "opinions"),
    ("ducky/opinion.py", "CREATE", "opinions"),
    ("ducky/persona_memory.py", "CREATE", "persona_banks"),
    ("ducky/persona_memory.py", "CREATE", "persona_memories"),
    ("ducky/pipeline/memory_workspace.py", "ALTER", "workspace"),
    ("ducky/pipeline/memory_workspace.py", "ALTER", "workspace_v20"),
    ("ducky/pipeline/memory_workspace.py", "CREATE", "workspace"),
    ("ducky/pipeline/memory_workspace.py", "CREATE", "workspace_v20"),
    ("ducky/refine_memory.py", "ALTER", "refined_memories"),
    ("ducky/refine_memory.py", "CREATE", "refined_memories"),
    ("ducky/reflect.py", "ALTER", "reflections"),
    ("ducky/reflect.py", "CREATE", "reflections"),
    ("ducky/salience/db.py", "ALTER", "salience"),
    ("ducky/salience/db.py", "CREATE", "daily_metrics"),
    ("ducky/salience/db.py", "CREATE", "salience"),
    ("ducky/schema_bootstrap.py", "ALTER", "facts"),
    ("ducky/schema_bootstrap.py", "ALTER", "<dynamic>"),
    ("ducky/schema_bootstrap.py", "CREATE", "entities"),
    ("ducky/schema_bootstrap.py", "CREATE", "fact_entities"),
    ("ducky/schema_bootstrap.py", "CREATE", "fact_events"),
    ("ducky/schema_bootstrap.py", "CREATE", "facts"),
    ("ducky/self_edit.py", "CREATE", "memory_edits"),
    ("ducky/skill_crystallizer.py", "ALTER", "skill_crystals"),
    ("ducky/skill_crystallizer.py", "CREATE", "skill_crystals"),
    ("ducky/text_fts.py", "ALTER", "memories"),
    ("ducky/text_fts.py", "ALTER", "memories_v20"),
    ("ducky/text_fts.py", "CREATE", "memories"),
    ("ducky/text_fts.py", "CREATE", "memories_v20"),
    ("ducky/tombstone.py", "ALTER", "tombstones"),
    ("ducky/tombstone.py", "CREATE", "tombstones"),
    ("ducky/tree_memory.py", "CREATE", "memory_nodes"),
    ("ducky/utils.py", "CREATE", "knowledge_evolution"),
    ("ducky/utils.py", "CREATE", "memory_states"),
    ("ducky/vector_backend.py", "CREATE", "vectors"),
    ("ducky/verbatim_vault.py", "ALTER", "verbatim_fts_map"),
    ("ducky/verbatim_vault.py", "ALTER", "verbatim_turns"),
    ("ducky/verbatim_vault.py", "CREATE", "verbatim_fts_map"),
    ("ducky/verbatim_vault.py", "CREATE", "verbatim_turns"),
}


def _scan_migration_points():
    pts = set()
    for f in sorted((_REPO / "ducky").rglob("*.py")):
        src = f.read_text(encoding="utf-8")
        rel = str(f.relative_to(_REPO))
        for m in _MIG_RE.finditer(src):
            kind = "ALTER" if m.group(1).upper().startswith("ALTER") else "CREATE"
            table = m.group(2)
            if table.upper() in _SQL_KEYWORDS:
                continue  # 文档叙述的回溯赝品，非迁移点
            if table.startswith("{") or table.upper() in _SQL_KEYWORD_TABLES:
                table = "<dynamic>"
            pts.add((rel, kind, table))
    return pts


def test_migration_points_are_registered():
    found = _scan_migration_points()
    new = found - _MIGRATION_LEDGER
    assert not new, (
        f"出现未登记的 schema 迁移点：{sorted(new)} —— additive-only 纪律"
        "照旧，但每个迁移点必须在总账登记（本文件 _MIGRATION_LEDGER）"
    )
    vanished = _MIGRATION_LEDGER - found
    assert not vanished, f"登记的迁移点已消失（清账）：{sorted(vanished)}"


def test_ledgers_are_not_vacuous():
    """三张登记表都必须真的抓着东西——空射程守卫是白护栏。"""
    assert len(_scan_sql_interpolations()) >= 50
    assert len(_scan_migration_points()) >= 50
    src = (_REPO / "frontend/js/panels.js").read_text(encoding="utf-8")
    assert sum(1 for _ in _innerhtml_statements(src)) >= 30
