"""
ducky.federation.router — MoE 门控：热通道 vs 联邦通道
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

「总容量几百 T，激活几十 B 就够强大。」

门控规则（按优先级短路，全部为本地判断，零 LLM 调用）
    1. 显式 federated=True/False    → 听调用方的
    2. 查询里出现其他 Agent 名/联邦关键词 → 激活联邦
    3. 联邦内只有一个 Agent          → 永远走热通道
    4. 其余情况                      → 热通道（默认，最省）

热通道 ≠ 阉割：L1+L2 已含分层衰减与铁律优先，
日常陪伴的检索质量不因不联邦而下降，只是不去问别人。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ducky.federation.recall import federated_recall
from ducky.federation.registry import list_agents
from ducky.federation.schema import DEFAULT_AGENT

logger = logging.getLogger("aiduMEM.Federation.Router")

HOT = "hot"
FEDERATED = "federated"

# 出现这些词，说明用户/Agent 在问「大家」而不是「我」
_FEDERATION_HINTS = (
    "联邦", "其他 agent", "其他agent", "别的agent", "所有agent",
    "跨 agent", "跨agent", "全局", "大家", "federation", "all agents",
    "cross-agent", "其他助手", "别的助手",
)


@dataclass(frozen=True)
class RouteDecision:
    """门控决策。reason 是人话解释，方便排障时一眼看懂为什么走了这条路。"""

    channel: str
    federated: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"channel": self.channel, "federated": self.federated, "reason": self.reason}


def decide(
    query: str,
    *,
    agent_id: str = DEFAULT_AGENT,
    federated: bool | None = None,
) -> RouteDecision:
    """决定这次检索走热通道还是联邦通道。"""
    if federated is not None:
        channel = FEDERATED if federated else HOT
        return RouteDecision(channel, bool(federated), "调用方显式指定")

    lowered = (query or "").lower()
    for hint in _FEDERATION_HINTS:
        if hint in lowered:
            return RouteDecision(FEDERATED, True, f"查询含联邦意图关键词「{hint}」")

    try:
        peers = [a for a in list_agents(include_inactive=False) if a["agent_id"] != agent_id]
    except Exception as exc:
        logger.debug("门控查询 Agent 列表失败，保守走热通道: %s", exc)
        return RouteDecision(HOT, False, "注册表不可用，保守走热通道")

    if not peers:
        return RouteDecision(HOT, False, "联邦内无其他在线 Agent")
    return RouteDecision(HOT, False, f"默认热通道（联邦内另有 {len(peers)} 个 Agent 待唤）")


def route_recall(
    query: str,
    *,
    agent_id: str = DEFAULT_AGENT,
    profile: str | None = None,
    category: str | None = None,
    top_k: int = 10,
    federated: bool | None = None,
    rerank: bool = False,
    tier_filter: str | None = None,
) -> dict[str, Any]:
    """门控 + 检索一体入口：这是上层唯一需要调用的函数。"""
    decision = decide(query, agent_id=agent_id, federated=federated)
    result = federated_recall(
        query,
        agent_id=agent_id,
        profile=profile,
        category=category,
        top_k=top_k,
        federated=decision.federated,
        rerank=rerank,
        tier_filter=tier_filter,
    )
    result["route"] = decision.to_dict()
    return result
