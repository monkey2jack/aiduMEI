"""
ducky.federation.routes — 联邦层 HTTP 端点
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    POST /federation/agents/register    注册 Agent
    POST /federation/agents/heartbeat   心跳
    POST /federation/agents/deactivate  置休眠
    GET  /federation/agents             Agent 列表（含事实数/在线态）
    GET  /federation/recall             MoE 门控检索（热/联邦自动决策）
    POST /federation/facts/add          联邦写入（去重+分层+归属）
    GET  /federation/broadcast          拉取其他 Agent 的新共享事实
    GET  /federation/awareness          联邦态势摘要
    GET  /federation/tiers              分层统计与衰减配置
    POST /federation/migrate            手动触发 schema 迁移（幂等）

全部端点异常都返回结构化 error，不抛 500——
记忆层是基础设施，宁可降级也不能拖垮上层 Agent。
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI

from ducky.federation import broadcast as broadcast_mod
from ducky.federation import registry as registry_mod
from ducky.federation import tier as tier_mod
from ducky.federation.router import route_recall
from ducky.federation.schema import DEFAULT_AGENT, DEFAULT_PROFILE, ensure_federation_schema
from ducky.federation.writer import write_fact
from ducky.utils import DEFAULT_USER_ID, get_facts_conn

logger = logging.getLogger("aiduMEM.Federation.Routes")


def _safe(fn, *args, **kwargs) -> dict[str, Any]:
    """统一异常包裹：任何端点崩了都返回 error dict 而不是 500。"""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        logger.error("联邦端点异常 %s: %s", getattr(fn, "__name__", fn), exc)
        return {"status": "error", "detail": str(exc)}


def register_federation_routes(app: FastAPI) -> None:
    """注册联邦层全部端点。启动时顺带跑一次幂等迁移。"""
    ensure_federation_schema()

    # ── Agent 注册表 ──────────────────────────────
    @app.post("/federation/agents/register")
    def federation_register_agent(
        agent_id: str,
        display_name: str = "",
        profile: str = DEFAULT_PROFILE,
        description: str = "",
        endpoint: str = "",
    ):
        return _safe(
            registry_mod.register_agent,
            agent_id,
            display_name=display_name,
            profile=profile,
            description=description,
            endpoint=endpoint,
        )

    @app.post("/federation/agents/heartbeat")
    def federation_heartbeat(agent_id: str = DEFAULT_AGENT):
        return _safe(registry_mod.heartbeat, agent_id)

    @app.post("/federation/agents/deactivate")
    def federation_deactivate(agent_id: str):
        return _safe(registry_mod.deactivate_agent, agent_id)

    @app.get("/federation/agents")
    def federation_list_agents(profile: str | None = None, include_inactive: bool = True):
        agents = _safe(registry_mod.list_agents, profile, include_inactive)
        if isinstance(agents, dict):  # 异常路径
            return agents
        return {"status": "ok", "count": len(agents), "agents": agents}

    # ── 检索 ──────────────────────────────────────
    @app.get("/federation/recall")
    def federation_recall_endpoint(
        query: str = "",
        agent_id: str = DEFAULT_AGENT,
        profile: str | None = None,
        category: str | None = None,
        top_k: int = 10,
        federated: bool | None = None,
        rerank: bool = False,
        tier: str | None = None,
    ):
        return _safe(
            route_recall,
            query,
            agent_id=agent_id,
            profile=profile,
            category=category,
            top_k=top_k,
            federated=federated,
            rerank=rerank,
            tier_filter=tier,
        )

    # ── 写入 ──────────────────────────────────────
    @app.post("/federation/facts/add")
    def federation_add_fact(
        category: str = "general",
        fact_key: str = "",
        fact_value: str = "",
        agent_id: str = DEFAULT_AGENT,
        profile: str = DEFAULT_PROFILE,
        memory_tier: str = "",
        source: str = DEFAULT_USER_ID,
        tags: str = "",
        shared: bool = True,
        dedup: bool = True,
        valid_from: str = "",
        valid_to: str = "",
    ):
        return _safe(
            write_fact,
            category,
            fact_key,
            fact_value,
            agent_id=agent_id,
            profile=profile,
            memory_tier=memory_tier or None,
            source=source,
            tags=tags,
            shared=shared,
            dedup=dedup,
            valid_from=valid_from,
            valid_to=valid_to,
        )

    # ── 广播与感知 ────────────────────────────────
    @app.get("/federation/broadcast")
    def federation_broadcast(
        agent_id: str = DEFAULT_AGENT,
        limit: int = broadcast_mod.BROADCAST_LIMIT,
        same_profile_only: bool = True,
        preview: bool = False,
    ):
        return _safe(
            broadcast_mod.collect_updates,
            agent_id,
            limit=limit,
            same_profile_only=same_profile_only,
            advance_cursor=not preview,
        )

    @app.get("/federation/awareness")
    def federation_awareness(agent_id: str = DEFAULT_AGENT):
        return _safe(broadcast_mod.awareness_summary, agent_id)

    # ── 分层统计 ──────────────────────────────────
    @app.get("/federation/tiers")
    def federation_tiers():
        def _stats():
            conn = get_facts_conn()
            try:
                rows = conn.execute(
                    """SELECT COALESCE(memory_tier,'semantic') AS memory_tier, COUNT(*) AS cnt
                       FROM facts WHERE archived=0 GROUP BY memory_tier"""
                ).fetchall()
            finally:
                conn.close()
            return {
                "status": "ok",
                "distribution": {r["memory_tier"]: r["cnt"] for r in rows},
                "config": {
                    t: {
                        "ttl_days": tier_mod.TIER_TTL_DAYS[t],
                        "weight": tier_mod.TIER_WEIGHT[t],
                        "decays": tier_mod.TIER_TTL_DAYS[t] is not None,
                    }
                    for t in tier_mod.VALID_TIERS
                },
            }

        return _safe(_stats)

    # ── 迁移 ──────────────────────────────────────
    @app.post("/federation/migrate")
    def federation_migrate(force: bool = False):
        return _safe(ensure_federation_schema, force)

    logger.info("✅ 联邦层路由注册完毕（10 端点）")
