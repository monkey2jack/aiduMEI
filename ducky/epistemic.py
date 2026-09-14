"""
ducky.epistemic — 认知出身标签 (v21 preview · EchoMind 融改 F1)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
每条记忆写入时标注「出身」：用户亲口说的、外部引用的、LLM 推断的、
来源不明的——四档在库里不同地位，检索时按信任等级加权。

借鉴来源: EchoMind Memory v1.2.13（jasonatgit）的 epistemic_mode 设计思想
  - 思路级借鉴，本文件为 aiduMEI 独立实现（对方仓库无 LICENSE，不复制代码）
  - 零 LLM 成本：纯 source 规则映射，写入时一次判定
  - 与本仓 Verbatim Vault 组成「原文保真 + 出身标签」双保险

包含:
1. resolve_epistemic(source, has_external_ref) — 唯一判定纯函数
2. EPISTEMIC_MODES — 合法枚举（供探针/测试校验）
3. DEFAULT_EPISTEMIC_MULTIPLIERS — 检索乘数默认值（manifest 可覆盖）
"""
from __future__ import annotations

import os

logger_name = "aiduMEM.Epistemic"

# ── 合法枚举（探针与测试的唯一真相源）─────────────────────────────────────────
EPISTEMIC_USER_PROVIDED = "user_provided"  # 用户亲口提供的事实
EPISTEMIC_REFERENCED = "referenced"        # 引用外部文档/网页/联邦同步
EPISTEMIC_REASONED = "reasoned"            # LLM 推断产物（未经验证）
EPISTEMIC_FUZZY = "fuzzy"                  # 来源不明（兜底，含全部存量行）

EPISTEMIC_MODES = (
    EPISTEMIC_USER_PROVIDED,
    EPISTEMIC_REFERENCED,
    EPISTEMIC_REASONED,
    EPISTEMIC_FUZZY,
)

# ── 检索乘数默认值（manifest/env 可覆盖，禁止只改代码）────────────────────────
DEFAULT_EPISTEMIC_MULTIPLIERS = {
    EPISTEMIC_USER_PROVIDED: 1.15,
    EPISTEMIC_REFERENCED: 1.05,
    EPISTEMIC_FUZZY: 1.00,
    EPISTEMIC_REASONED: 0.85,
}

# ── source 前缀 → 出身（基于本仓真实 source 值核定）──────────────────────────
# 本仓在册的写入方：pattern_extract / conflict_resolver / reflect / autodream /
# self_edit / refine_rollback / wal_cascade / backfill / gear(upshift) 等。
# 规则按优先级从上到下，先中先得。
_REASONED_SOURCES = (
    "pattern_extract",
    "reflect",
    "autodream",
    "self_edit",
    "refine",
    "cron_lesson",
    "assistant",
    "ai-self",      # persona AI 自我认知写入（extended/routes.py）
    "persona",
    "llm",
    "distill",
    "instinct_graduation",
    "skill_growth",
)

_REFERENCED_SOURCES = (
    "web_extract",
    "web",
    "doc",
    "url",
    "http",
    "federation",   # 联邦同步引入的外部记忆
    "import",
)

# 系统内部记账/迁移来源不算「用户事实」，也不算「推断」——归入 fuzzy 兜底，
# 由调用方显式指定时才给更高档（保持诚实：不知道就是不知道）。
_SYSTEM_SOURCES = (
    "wal_cascade",
    "backfill",
    "schema_v",
    "reconcile",
    "upshift",
    "system",
)


def resolve_epistemic(source: str, *, has_external_ref: bool = False) -> str:
    """按写入来源判定认知出身。纯函数，零副作用，零 LLM 成本。

    判定优先级：
      1. 显式外部引用标记 → referenced
      2. 外部引用类来源 → referenced
      3. 推断类来源 → reasoned
      4. 系统内部来源 → fuzzy（如实兜底）
      5. 其余（用户直述/用户 id 直写/未知）→ user_provided

    注意第 5 条的方向选择：本仓 facts.source 的历史惯例是「默认填用户 id」
    （schema 默认值即 DEFAULT_USER_ID），凡是走到这里的都是用户语境的直接
    写入；推断/系统类写入在本仓均有专属 source 字符串，已在上面拦截。
    """
    s = (source or "").strip().lower()

    if has_external_ref:
        return EPISTEMIC_REFERENCED
    if any(k in s for k in _REFERENCED_SOURCES):
        return EPISTEMIC_REFERENCED
    if any(k in s for k in _REASONED_SOURCES):
        return EPISTEMIC_REASONED
    if any(k in s for k in _SYSTEM_SOURCES):
        return EPISTEMIC_FUZZY
    if not s:
        return EPISTEMIC_FUZZY
    return EPISTEMIC_USER_PROVIDED


def stamp_epistemic(conn, fact_id, source: str, *, has_external_ref: bool = False) -> str:
    """写入后补打认知出身标签（v21 F1 统一入口）。

    列不在（未迁移库 / 旧 DDL 测试夹具）如实跳过返回 ''——不打炸写入
    主链路，也不假装已标注；列在则写入并返回所打档位。
    """
    from ducky.bank_contract import table_columns
    if "epistemic_mode" not in table_columns(conn, "facts"):
        return ""
    mode = resolve_epistemic(source, has_external_ref=has_external_ref)
    conn.execute("UPDATE facts SET epistemic_mode=? WHERE id=?", (mode, fact_id))
    return mode


def stamp_memory_refs(refs, mode: str, *, user_id: str, bank_id: str,
                      source: str = "") -> int:
    """mem0 主链路记忆的出身登记（sidecar 表 memory_epistemic）。

    v21.0 收口（生产用户审计 🔴-1）：mem0.add 蒸馏产物不进 facts 表，
    出身按 memory_ref（UUID）落 sidecar。诚实映射由调用方给出：
    LLM 经手（infer=True）→ reasoned；确定性直写（infer=False）→ user_provided。
    表不在（未迁移库）如实返回 0——不打炸写入主链路。
    """
    if mode not in EPISTEMIC_MODES:
        return 0
    refs = [str(r) for r in refs if r]
    if not refs:
        return 0
    from datetime import datetime, timezone
    from ducky.utils import get_facts_conn
    conn = get_facts_conn()
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "memory_epistemic" not in tables:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        for ref in refs:
            conn.execute(
                "INSERT INTO memory_epistemic (memory_ref, epistemic_mode, user_id, bank_id,"
                " source, created_at, updated_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(memory_ref) DO UPDATE SET epistemic_mode=excluded.epistemic_mode,"
                " updated_at=excluded.updated_at",
                (ref, mode, user_id, bank_id, source, now, now))
        conn.commit()
        return len(refs)
    finally:
        conn.close()


def load_epistemic_multipliers(env: dict[str, str] | None = None) -> dict[str, float]:
    """加载检索乘数。env 可覆盖（AIDUMEI_EPISTEMIC_MULT_<MODE>），
    非法值 fail-closed 回默认——与 env_config 的有限性闸门同一纪律。

    四个变量名以字面量登记在 _ENV_KEYS（env 注册表守卫按源码字面量对账，
    动态拼接的名字对不上账）。"""
    environ = os.environ if env is None else env
    multipliers = dict(DEFAULT_EPISTEMIC_MULTIPLIERS)
    for mode, env_key in _ENV_KEYS.items():
        raw = environ.get(env_key)
        if raw is None:
            continue
        try:
            val = float(raw)
        except (TypeError, ValueError):
            continue  # 非法配置：回默认，不炸服务
        if not (0.0 <= val <= 2.0):  # 有界乘数：超出区间视为非法
            continue
        multipliers[mode] = val
    return multipliers


# env 变量名唯一真相源（字面量，供注册表守卫对账）
_ENV_KEYS = {
    EPISTEMIC_USER_PROVIDED: "AIDUMEI_EPISTEMIC_MULT_USER_PROVIDED",
    EPISTEMIC_REFERENCED: "AIDUMEI_EPISTEMIC_MULT_REFERENCED",
    EPISTEMIC_REASONED: "AIDUMEI_EPISTEMIC_MULT_REASONED",
    EPISTEMIC_FUZZY: "AIDUMEI_EPISTEMIC_MULT_FUZZY",
}
