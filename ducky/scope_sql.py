"""
ducky.scope_sql — 统一作用域 SQL 构建器（v20.5.1 · T-05）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

**为什么存在**：本仓的作用域谓词长期以三种形态散在各处——

1. 正规入口：``bank_contract.scope_predicate``（精确匹配，带守卫的放宽档）、
   ``bank_contract.legacy_fact_scope_predicate``（迁移期混合归属）、
   ``facts_recall.tenant_clause``（facts 表，含 strict/宽松档与 conn 迁移探测）；
2. 手抄片段：各处直接拼 ``user_id=? AND bank_id=?`` 的字符串——v20.2.4 那次
   「子句算出来却没拼进 SQL」的假修复、v20.5.0 的两条 🔴，根因同出一门：
   **关键判据靠每个调用点自觉写对，没有任何东西强制它必须存在**；
3. 请求参数裸传：外部传入的 scope dict 想带什么键就带什么键。

本模块把三种形态收进**一个入口**：调用点只许从这里拿作用域 SQL，
 flavor 用枚举显式选择，未知维度默认拒绝，alias 上 SQL 文本前必须过
标识符校验。配合 ``tests/test_v20_5_1_scope_sql_guard.py`` 的棘轮普查：
**手拼点只许减少，不许新增。**

这不是把旧函数换个名字再出口——三个底层谓词的语义一字未动（它们各自
背着真实的生产教训，见各自 docstring），本模块只做：唯一入口、参数
校验、flavor 路由、调用面收敛。
"""
from __future__ import annotations

from typing import Any, Literal

from ducky.bank_contract import (
    BankScope,
    legacy_fact_scope_predicate,
    make_scope,
    scope_predicate,
)

__all__ = ["KNOWN_SCOPE_KEYS", "scope_clause", "scope_from_mapping"]

# 已知作用域维度全集。请求面传入的 scope 映射里出现任何其它键，
# 一律拒绝 —— 「未知维度静默放行」在 v20.2.4（🟡-4）真实发生过：
# 调用方以为自己收窄了，其实条件被无声丢掉。
KNOWN_SCOPE_KEYS = frozenset({"user_id", "bank_id"})

# flavor 全集：
#   canonical  —— 精确 user_id + bank_id（新代码的默认）
#   transition —— 迁移期混合归属（user_id 为主，未认领行按渠道标记回落）
#   facts      —— facts 表专用（strict/宽松档 + conn 迁移探测）
_FLAVORS = frozenset({"canonical", "transition", "facts"})


def _check_alias(alias: str) -> str:
    """alias 会被插进 SQL 文本（``f"{alias}."``），必须是不可注入的标识符。

    当前全部调用点传字面量，本校验保证「以后谁把外部输入接进来」当场炸在
    这里，而不是变成 SQL 注入。空串合法（不加前缀）。
    """
    if alias and not alias.isidentifier():
        raise ValueError(f"alias 必须是合法标识符，收到: {alias!r}")
    return alias


def scope_clause(
    scope: BankScope | None = None,
    *,
    alias: str = "",
    flavor: Literal["canonical", "transition", "facts"] = "canonical",
    conn: Any | None = None,
) -> tuple[str, list[str]]:
    """构造租户作用域 SQL 片段的唯一入口。返回 (sql_fragment, params)。

    sql_fragment 以 ``AND`` 开头，可直接拼进既有 ``WHERE`` 之后。
    flavor 语义见模块 docstring；未知 flavor 抛 ValueError（fail-closed，
    绝不静默落回某个宽松形态）。
    """
    _check_alias(alias)
    scope = scope or make_scope()
    if flavor == "canonical":
        return scope_predicate(scope, alias=alias)
    if flavor == "transition":
        return legacy_fact_scope_predicate(scope, alias=alias)
    if flavor == "facts":
        from ducky.facts_recall import tenant_clause

        return tenant_clause(
            scope.user_id, alias=alias, bank_id=scope.bank_id, conn=conn
        )
    raise ValueError(
        f"未知 scope flavor: {flavor!r}（合法值：{sorted(_FLAVORS)}）"
    )


def scope_from_mapping(raw: dict[str, Any], *, who: str = "") -> BankScope:
    """把请求面进来的 scope 映射收敛成 BankScope；未知维度默认拒绝。

    用途：HTTP/MCP 边界把 ``{"user_id": ..., "bank_id": ...}`` 变成受信
    scope。多出来的任何键（``tenant``、``org``、拼错的 ``bankId``……）
    都抛 BankScopeError——拼错的维度名本质是「调用方以为收窄了而实际没有」，
    必须当场炸，不许静默忽略。
    """
    from ducky.bank_contract import BankScopeError

    unknown = set(raw) - KNOWN_SCOPE_KEYS
    if unknown:
        raise BankScopeError(
            f"{who or '调用方'}传入了未知 scope 维度: {sorted(unknown)}"
            f"（已知维度：{sorted(KNOWN_SCOPE_KEYS)}）——拒绝放行"
        )
    return make_scope(user_id=raw.get("user_id"), bank_id=raw.get("bank_id"))
