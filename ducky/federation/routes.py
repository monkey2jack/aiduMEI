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


def _implicit_caller_allowed() -> bool:
    """逃生门：AIDUMEI_ALLOW_IMPLICIT_CALLER=1 时兼容「不传 caller 的旧单机请求」。

    v20.5.0 正式版（用户审计 🔴-2）：默认**关**。零信任模型不认「我没说我
    是谁」——那正是要拒绝的形态。存量单机部署升级时若客户端暂时无法传
    caller_agent_id，可显式开启本开关过渡，但请在客户端补传后尽快关闭。
    """
    import os
    return os.environ.get("AIDUMEI_ALLOW_IMPLICIT_CALLER", "").strip() == "1"


def _federation_admins() -> frozenset[str]:
    """联邦管理面 admin 名单：AIDUMEI_FEDERATION_ADMINS（逗号分隔）。"""
    import os
    raw = os.environ.get("AIDUMEI_FEDERATION_ADMINS", "")
    return frozenset(a.strip() for a in raw.split(",") if a.strip())


def _is_admin_caller(caller: str) -> bool:
    return bool(caller) and caller in _federation_admins()


def _caller_bindings() -> dict[str, Any] | None:
    """AIDUMEI_CALLER_BINDINGS：`{"<token_sha256前16位>": ["agent_a", ...]}`。

    返回 None = 未配置 → 调用方逐字走旧行为（兼容红线，v20.5.1 T-07）。
    配置了但 JSON 非法/不是对象 → fail-closed 抛 403：安全档配置写错
    不能静默失效（与 scoring._evidence_gate_on「非法值按开」同一家训）。
    请求时实时解析，不做模块级定格 —— 与凭据读取同一纪律。
    """
    import os
    raw = os.environ.get("AIDUMEI_CALLER_BINDINGS", "").strip()
    if not raw:
        return None
    from fastapi import HTTPException
    import json
    try:
        table = json.loads(raw)
        if not isinstance(table, dict):
            raise ValueError("顶层必须是 JSON 对象")
    except ValueError:
        logger.error("🛑 [Security] AIDUMEI_CALLER_BINDINGS 不是合法 JSON 对象，"
                     "本次调用 fail-closed 拒绝（配置修正前绑定面不可用）")
        raise HTTPException(
            status_code=403,
            detail={
                "error": "caller_bindings_misconfigured",
                "hint": "AIDUMEI_CALLER_BINDINGS 须为 JSON 对象："
                        "{\"<token_sha256前16位>\": [\"agent_a\", ...]}；"
                        "暂不需要绑定时请整体移除该变量",
            },
        )
    return table


def _enforce_caller_binding(caller: str, operation: str) -> None:
    """🛡️ caller↔凭据轻量绑定（v20.5.1 · T-07）：token 可代表的 agent_id 白名单。

    强制条件**同时**成立才拦（缺一即按现状放行）：
      ① 配置了 AIDUMEI_CALLER_BINDINGS；
      ② 本请求经 Bearer / X-API-Token 过闸，且其指纹已登记在 bindings。
    未配置 env 时本函数逐字等价于不存在 —— 这是兼容红线。
    session cookie（控制台）与无凭据回环请求不带指纹，不参与绑定。
    """
    table = _caller_bindings()
    if table is None:
        return
    from ducky.security.auth import current_request_token_fingerprint
    fp = current_request_token_fingerprint()
    if not fp or fp not in table:
        return
    allowed = table[fp]
    if not isinstance(allowed, list):
        allowed = []
    if caller not in {str(a) for a in allowed}:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=403,
            detail={
                "error": "caller_token_binding_mismatch",
                "operation": operation,
                "caller_agent_id": caller,
                "hint": "本凭据（token）未登记可代表该 agent_id；"
                        "请在 AIDUMEI_CALLER_BINDINGS 的白名单中补登，或换用对应凭据",
            },
        )


def _require_caller(caller_agent_id: str, *, operation: str) -> str:
    """管理/查询面身份门槛：caller_agent_id 必填（逃生门开启时放行空 caller）。

    返回规范化后的 caller（逃生门放行时为空串，调用方按单机兼容语义处理）。

    v20.5.1（T-07）：caller 非空时再过一道 caller↔凭据轻量绑定 —— 仅当
    配置了 AIDUMEI_CALLER_BINDINGS 且本请求 token 指纹已登记时强制
    caller ∈ 白名单；未配置该 env 时行为与此前逐字一致。
    """
    caller = (caller_agent_id or "").strip()
    if not caller:
        if _implicit_caller_allowed():
            return ""
        from fastapi import HTTPException
        raise HTTPException(
            status_code=403,
            detail={
                "error": "caller_agent_id_required",
                "operation": operation,
                "hint": "联邦管理/查询操作必须声明调用者身份（caller_agent_id）；"
                        "存量单机部署可显式设 AIDUMEI_ALLOW_IMPLICIT_CALLER=1 过渡",
            },
        )
    _enforce_caller_binding(caller, operation)
    return caller


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
    if not caller:
        # v20.5.0 正式版（用户审计 🔴-2）：空 caller 不再默认放行——
        # 「不传 caller + 传 victim 的 agent_id」就是冒充本人。默认拒绝，
        # 仅显式逃生门 AIDUMEI_ALLOW_IMPLICIT_CALLER=1 兼容旧单机请求。
        if _implicit_caller_allowed():
            return
        from fastapi import HTTPException
        raise HTTPException(
            status_code=403,
            detail={
                "error": "caller_agent_id_required",
                "owner_agent": owner_agent,
                "action": action,
                "hint": "跨/本 Agent 访问均须声明 caller_agent_id；"
                        "存量单机部署可显式设 AIDUMEI_ALLOW_IMPLICIT_CALLER=1 过渡",
            },
        )
    if caller == owner_agent:
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


def _enforce_lineage_read(memory_id: str, caller: str) -> None:
    """谱系读取的归属校验（🟡-6）：memory_id → facts 行 → owner，再套 Grant 语义。

    caller 为空（逃生门开启的旧单机请求）放行；facts 行不存在（已删除/幽灵）
    或非 fact 形态的 memory_id 无法确立归属 → 仅 admin 可读。
    """
    if not caller or _is_admin_caller(caller):
        return
    from fastapi import HTTPException
    owner = ""
    mid = (memory_id or "").strip()
    if mid.startswith("fact:") and mid.split(":", 1)[1].isdigit():
        conn = get_facts_conn()
        try:
            row = conn.execute(
                "SELECT agent_id FROM facts WHERE id=?", (int(mid.split(":", 1)[1]),)
            ).fetchone()
            owner = (row[0] or "") if row else ""
        finally:
            conn.close()
    if not owner:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "lineage_owner_unresolvable",
                "memory_id": memory_id,
                "hint": "该谱系无法确立事实归属（行已删除或形态非法），仅 admin 可读",
            },
        )
    _enforce_grant(owner, caller, "read")


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
        caller_agent_id: str = "",
    ):
        # 🛡️ v20.5.1（T-06 · 根因 R-1 接缝排查）：register 是 upsert——
        # 无门槛时任何持 Bearer 者可改写他人 display_name/endpoint，
        # 且 ON CONFLICT 会把已 deactivate 的 agent 重新激活（active=1），
        # 等于 deactivate 被 register 反制。规则：本人或 admin。
        caller = _require_caller(caller_agent_id, operation="register_agent")
        if caller and caller != agent_id and not _is_admin_caller(caller):
            from fastapi import HTTPException
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "register_forbidden",
                    "hint": "只能注册/刷新自己（caller == agent_id），或由 admin 代劳",
                },
            )
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
        # 有意不加 caller 门槛：心跳是高频自保信号，registry.heartbeat 对未注册
        # id 自动补注册是文档化的宽容设计；伪造心跳的最坏后果是让一个 agent
        # 「看起来在线」，不读不写他人数据。改这里要先想清楚生产 cron 的调用形态。
        return _safe(registry_mod.heartbeat, agent_id)

    @app.post("/federation/agents/deactivate")
    def federation_deactivate(agent_id: str, caller_agent_id: str = ""):
        # 🛡️ v20.5.1（T-06）：无门槛时任何持 Bearer 者可休眠任意 agent ——
        # 联邦面 DoS。规则：本人或 admin。
        caller = _require_caller(caller_agent_id, operation="deactivate_agent")
        if caller and caller != agent_id and not _is_admin_caller(caller):
            from fastapi import HTTPException
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "deactivate_forbidden",
                    "hint": "只能休眠自己（caller == agent_id），或由 admin 代劳",
                },
            )
        return _safe(registry_mod.deactivate_agent, agent_id)

    @app.get("/federation/agents")
    def federation_list_agents(
        profile: str | None = None,
        include_inactive: bool = True,
        caller_agent_id: str = "",
    ):
        # 🛡️ v20.5.1（T-08）：此前全仓唯一没接 _require_caller 的管理/查询端点。
        # Agent 清单（谁在线、谁挂了多少事实）是侦察面 —— 拿到它才能挑受害者
        # agent_id 去试 grants。与 create/list/revoke_grant、get/verify_lineage
        # 同构 fail-closed；逃生门语义由 _require_caller 统一承载。
        _require_caller(caller_agent_id, operation="list_agents")
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
        # 有意不加 caller 门槛：ensure_federation_schema 幂等且只做 ADD COLUMN /
        # CREATE TABLE（无任何 DROP/改写），重复触发无副作用。若哪天它长出破坏性
        # 分支，这里必须先补门槛。
        return _safe(ensure_federation_schema, force)

    # ── 授权管理 (Grants & Revocation · v20.5.0a) ──
    # v20.5.0 正式版（用户审计 🔴-2）：管理面三端点接入调用者校验——
    # 创建/撤销必须是「授权方本人或 admin」，created_by/revoked_by 从
    # caller 派生（不再吃请求参数，审计身份不可自报）。
    @app.post("/federation/grants")
    def federation_create_grant(
        grantor_agent: str,
        grantee_agent: str,
        resource_scope: str = "*",
        actions: str = "read",
        expires_at: str | None = None,
        grant_id: str | None = None,
        caller_agent_id: str = "",
    ):
        from fastapi import HTTPException
        caller = _require_caller(caller_agent_id, operation="create_grant")
        if caller and caller != grantor_agent and not _is_admin_caller(caller):
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "grantor_identity_mismatch",
                    "grantor_agent": grantor_agent,
                    "caller_agent_id": caller,
                    "hint": "只有授权方本人或联邦 admin 能为其签发授权（自签授权已禁止）",
                },
            )
        from ducky.federation.grants import create_grant
        return _safe(
            create_grant,
            grantor_agent,
            grantee_agent,
            resource_scope=resource_scope,
            actions=actions,
            expires_at=expires_at,
            grant_id=grant_id,
            created_by=caller or grantor_agent,
        )

    @app.get("/federation/grants")
    def federation_list_grants(
        grantor_agent: str | None = None,
        grantee_agent: str | None = None,
        include_revoked: bool = False,
        caller_agent_id: str = "",
    ):
        from fastapi import HTTPException
        caller = _require_caller(caller_agent_id, operation="list_grants")
        # 非 admin 只能看自己签出的授权；显式查别人 → 403（🟡-6 同源收窄）
        if caller and not _is_admin_caller(caller):
            if grantor_agent and grantor_agent != caller:
                raise HTTPException(
                    status_code=403,
                    detail={
                        "error": "grantor_identity_mismatch",
                        "grantor_agent": grantor_agent,
                        "caller_agent_id": caller,
                        "hint": "只能查询自己签出的授权；admin 可查全部",
                    },
                )
            grantor_agent = caller
        from ducky.federation.grants import list_grants
        return _safe(
            list_grants,
            grantor_agent=grantor_agent,
            grantee_agent=grantee_agent,
            include_revoked=include_revoked,
        )

    @app.post("/federation/grants/revoke")
    def federation_revoke_grant(grant_id: str, caller_agent_id: str = ""):
        from fastapi import HTTPException
        caller = _require_caller(caller_agent_id, operation="revoke_grant")
        from ducky.federation.grants import get_grant, revoke_grant
        if caller:
            grant = get_grant(grant_id)
            if grant is None:
                raise HTTPException(
                    status_code=404,
                    detail={"error": "grant_not_found", "grant_id": grant_id},
                )
            if grant["grantor_agent"] != caller and not _is_admin_caller(caller):
                raise HTTPException(
                    status_code=403,
                    detail={
                        "error": "grantor_identity_mismatch",
                        "grantor_agent": grant["grantor_agent"],
                        "caller_agent_id": caller,
                        "hint": "只有授权方本人或联邦 admin 能撤销该授权",
                    },
                )
        return _safe(revoke_grant, grant_id, revoked_by=caller or "system")

    # ── 谱系查询与验证 (Memory Lineage · v20.5.0a) ──
    # v20.5.0 正式版（用户审计 🟡-6）：谱系元数据（actor/source/diff_summary/
    # 时间戳）本身即泄漏面，查询端点同样要做归属校验。
    @app.get("/federation/lineage")
    def federation_get_lineage(memory_id: str, caller_agent_id: str = ""):
        caller = _require_caller(caller_agent_id, operation="get_lineage")
        _enforce_lineage_read(memory_id, caller)
        from ducky.memory_lineage import get_memory_lineage
        return _safe(get_memory_lineage, memory_id)

    @app.get("/federation/lineage/verify")
    def federation_verify_lineage(memory_id: str | None = None, caller_agent_id: str = ""):
        caller = _require_caller(caller_agent_id, operation="verify_lineage")
        if memory_id:
            _enforce_lineage_read(memory_id, caller)
        elif caller and not _is_admin_caller(caller):
            # 全库对账可能枚举他人链状态：非 admin 须逐链指定 memory_id
            from fastapi import HTTPException
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "admin_required",
                    "hint": "全库谱系对账仅 admin 可用；请指定 memory_id 校验自己的链",
                },
            )
        from ducky.memory_lineage import verify_lineage_integrity
        return _safe(verify_lineage_integrity, memory_id=memory_id)

    logger.info("✅ 联邦层路由注册完毕（15 端点，含 Grants 授权与 Lineage 谱系）")
