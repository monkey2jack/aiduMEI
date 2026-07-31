"""
ducky.federation — aiduMEM v13.0 Pantheon 联邦记忆层
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

设计哲学（MoE：全量基建 · 按需激活）
    底层建成完整的多 Agent / 多 Profile 联邦基础设施，
    日常只激活当前 Agent 的热通道（1 次 SQL，亚毫秒级），
    仅在显式请求或本地结果不足时才动态激活联邦通道。

    「万神殿里住着所有神，但每次只请出需要的那一位。」

模块分工
    schema    — facts.db 联邦字段与联邦表的幂等迁移
    tier      — 分层记忆（episodic / semantic / procedural）与衰减权重
    dedup     — 写入时相似度去重（合并 / 更新 / 新增三态）
    registry  — Agent 注册表：注册、心跳、列表、profile 归属
    recall    — L1→L4 无缝降级检索链 + 按需 Rerank
    router    — MoE 路由：热通道 vs 联邦通道的激活决策
    broadcast — 跨 Agent 事实广播与感知摘要
    routes    — 对外 HTTP 端点

向后兼容
    所有新字段带默认值，历史 1000+ 条事实自动归属 DEFAULT_AGENT，
    不传 agent_id 的旧调用方行为与 v12 完全一致。
"""
from __future__ import annotations

from ducky.federation.schema import DEFAULT_AGENT, DEFAULT_PROFILE, ensure_federation_schema

__all__ = [
    "DEFAULT_AGENT",
    "DEFAULT_PROFILE",
    "ensure_federation_schema",
]
