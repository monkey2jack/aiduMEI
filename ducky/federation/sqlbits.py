"""
ducky.federation.sqlbits — 联邦 SQL 片段与参数化助手
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

联邦查询里反复出现「历史行 agent_id/profile 可能为 NULL」的兼容处理。
把这些片段收在一处，用 **占位符 + 参数** 而非字符串插值——
即使默认身份是常量，也不给 SQL 拼接留任何习惯性口子。

约定
    每个 helper 返回 (sql_fragment, params)，调用方直接 append 到 where/params。
"""
from __future__ import annotations

from typing import Any

from ducky.federation.schema import DEFAULT_AGENT, DEFAULT_PROFILE

# 历史行 agent_id/profile 为 NULL 时视为默认身份
AGENT_EXPR = "COALESCE(agent_id, ?)"
PROFILE_EXPR = "COALESCE(profile, ?)"
TIER_EXPR = "COALESCE(memory_tier, 'semantic')"
SHARED_EXPR = "COALESCE(shared, 1)"


def agent_in(agent_ids: list[str]) -> tuple[str, list[Any]]:
    """限定归属 Agent 集合。空列表返回恒真片段（不加限制）。"""
    if not agent_ids:
        return "1=1", []
    placeholders = ",".join("?" for _ in agent_ids)
    return f"{AGENT_EXPR} IN ({placeholders})", [DEFAULT_AGENT, *agent_ids]


def agent_eq(agent_id: str) -> tuple[str, list[Any]]:
    """限定单个归属 Agent。"""
    return f"{AGENT_EXPR} = ?", [DEFAULT_AGENT, agent_id]


def profile_eq(profile: str) -> tuple[str, list[Any]]:
    """限定 profile。"""
    return f"{PROFILE_EXPR} = ?", [DEFAULT_PROFILE, profile]


def shared_only() -> tuple[str, list[Any]]:
    """只取允许跨 Agent 共享的事实。"""
    return f"{SHARED_EXPR} = 1", []
