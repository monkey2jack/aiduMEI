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

from ducky.federation import registry as registry_mod
from ducky.federation.schema import ensure_federation_schema

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


def _enforce_caller_binding(caller: str, operation: str) -> None:
    """Compatibility facade for the shared HTTP caller policy."""
    from ducky.security.auth import enforce_caller_binding
    enforce_caller_binding(caller, operation)


def _require_caller(caller_agent_id: str, *, operation: str) -> str:
    """管理/查询面身份门槛：caller_agent_id 必填（逃生门开启时放行空 caller）。

    返回规范化后的 caller（逃生门放行时为空串，调用方按单机兼容语义处理）。

    v20.5.1（T-07）：caller 非空时再过一道 caller↔凭据轻量绑定 —— 仅当
    配置了 AIDUMEI_CALLER_BINDINGS 且本请求 token 指纹已登记时强制
    caller ∈ 白名单；未配置该 env 时行为与此前逐字一致。
    """
    from ducky.security.auth import resolve_bound_caller
    caller = resolve_bound_caller(caller_agent_id, operation)
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


def register_federation_routes(app: FastAPI) -> None:
    """注册联邦层全部端点。启动时顺带跑一次幂等迁移。"""
    ensure_federation_schema()


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


    logger.info("✅ 联邦层路由注册完毕（1 个只读端点：Agent 列表）")
