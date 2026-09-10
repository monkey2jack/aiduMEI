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
from ducky.bank_contract import DEFAULT_BANK_ID
from ducky.federation.schema import DEFAULT_AGENT, DEFAULT_PROFILE, ensure_federation_schema
from ducky.federation.writer import write_fact
from ducky.utils import DEFAULT_USER_ID, get_facts_conn

logger = logging.getLogger("aiduMEM.Federation.Routes")


def _safe(fn, *args, **kwargs) -> dict[str, Any]:
    """统一异常包裹：任何端点崩了都返回 error dict 而不是 500。

    v20.4.0（三方审计 P2-6 · Codex P2-04）：原样 str(exc) 会把 SQL、路径、
    内部状态直接放进客户端响应。改走错误信封（类名 + 可重试性 + 指引），
    原始异常文本只进服务端日志。"""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        logger.error("联邦端点异常 %s: %s", getattr(fn, "__name__", fn), exc)
        from ducky.api_errors import error_envelope
        return {"status": "error", **error_envelope(exc)}


def _enforce_grant(
    owner_agent: str,
    caller_agent_id: str,
    action: str,
    *,
    category: str = "",
) -> None:
    """🛡️ 联邦授权策略实施点（v20.5.0a P0-2 · Zero-Trust PEP）。

    语义（与任务书对齐）：
      · caller_agent_id 为空 = 旧版单机请求 → 单用户回环，零改动放行
        （「向下兼容过渡」条款）；
      · caller == owner = 本 Agent 访问自己的记忆 → 放行；
      · 其余跨 Agent 访问：必须命中有效、未过期、动作匹配的
        federation_grants 记录，否则 403 Forbidden——默认拒绝，
        不再认 shared: bool 单方标记。

    只在 HTTP 边界拦（routes 层 PEP），不动 recall/broadcast 内部
    梯子——梯子是库内检索逻辑，策略归边界。"""
    caller = (caller_agent_id or "").strip()
    if not caller or caller == owner_agent:
        return
    from ducky.federation.grants import check_grant_permission
    if not check_grant_permission(owner_agent, caller, action, category=category):
        from fastapi import HTTPException
        raise HTTPException(
            status_code=403,
            detail={
                "error": "federation_grant_required",
                "owner_agent": owner_agent,
                "caller_agent_id": caller,
                "action": action,
                "hint": "POST /federation/grants 先取得授权，或撤销后重授",
            },
        )


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
        user_id: str = "",
        bank_id: str = "",
        caller_agent_id: str = "",
    ):
        # 🛡️ v20.5.0a P0-2：跨 Agent 检索须持有效 Grant（单机/本 Agent 回环放行）
        _enforce_grant(agent_id, caller_agent_id, "read", category=category or "")
        # v20 P0-2：opt-in 作用域——传了就四级梯子全收窄，不传 = v19 全库。
        # 非法作用域由 _safe 包成结构化 error（联邦层约定不抛 500）。
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
            user_id=user_id,
            bank_id=bank_id,
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
        user_id: str = DEFAULT_USER_ID,
        bank_id: str = DEFAULT_BANK_ID,
        tags: str = "",
        shared: bool = True,
        dedup: bool = True,
        valid_from: str = "",
        valid_to: str = "",
        caller_agent_id: str = "",
    ):
        # 🛡️ v20.5.0a P0-2：跨 Agent 写入须持 write Grant（单机/本 Agent 回环放行）
        _enforce_grant(agent_id, caller_agent_id, "write", category=category)
        return _safe(
            write_fact,
            category,
            fact_key,
            fact_value,
            agent_id=agent_id,
            profile=profile,
            memory_tier=memory_tier or None,
            source=source,
            user_id=user_id,
            bank_id=bank_id,
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
        caller_agent_id: str = "",
    ):
        # 🛡️ v20.5.0a P0-2：跨 Agent 拉取广播须持 read Grant
        #（拉的是 peers 共享事实，owner 侧按 agent_id 判）
        _enforce_grant(agent_id, caller_agent_id, "read")
        return _safe(
            broadcast_mod.collect_updates,
            agent_id,
            limit=limit,
            same_profile_only=same_profile_only,
            advance_cursor=not preview,
        )

    @app.get("/federation/awareness")
    def federation_awareness(agent_id: str = DEFAULT_AGENT, caller_agent_id: str = ""):
        # 🛡️ v20.5.0a P0-2：跨 Agent 态势摘要同样须持 read Grant（摘要含事实计数）
        _enforce_grant(agent_id, caller_agent_id, "read")
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

    # ── 授权管理 (Grants & Revocation · v20.5.0a) ──
    @app.post("/federation/grants")
    def federation_create_grant(
        grantor_agent: str,
        grantee_agent: str,
        resource_scope: str = "*",
        actions: str = "read",
        expires_at: str | None = None,
        grant_id: str | None = None,
    ):
        from ducky.federation.grants import create_grant
        return _safe(
            create_grant,
            grantor_agent,
            grantee_agent,
            resource_scope=resource_scope,
            actions=actions,
            expires_at=expires_at,
            grant_id=grant_id,
        )

    @app.get("/federation/grants")
    def federation_list_grants(
        grantor_agent: str | None = None,
        grantee_agent: str | None = None,
        include_revoked: bool = False,
    ):
        from ducky.federation.grants import list_grants
        return _safe(
            list_grants,
            grantor_agent=grantor_agent,
            grantee_agent=grantee_agent,
            include_revoked=include_revoked,
        )

    @app.post("/federation/grants/revoke")
    def federation_revoke_grant(grant_id: str, revoked_by: str = "system"):
        from ducky.federation.grants import revoke_grant
        return _safe(revoke_grant, grant_id, revoked_by=revoked_by)

    # ── 谱系查询与验证 (Memory Lineage · v20.5.0a) ──
    @app.get("/federation/lineage")
    def federation_get_lineage(memory_id: str):
        from ducky.memory_lineage import get_memory_lineage
        return _safe(get_memory_lineage, memory_id)

    @app.get("/federation/lineage/verify")
    def federation_verify_lineage(memory_id: str | None = None):
        from ducky.memory_lineage import verify_lineage_integrity
        return _safe(verify_lineage_integrity, memory_id=memory_id)

    logger.info("✅ 联邦层路由注册完毕（15 端点，含 Grants 授权与 Lineage 谱系）")
