"""
ducky.routes_registry — 统一路由注册表
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
收拢所有业务线路由，api_server 仅需调用此处。

f0.6+++ WP2（接口瘦身）：只保留真实调用方在用的端点 ——
  · 扁平根路径 = 宿主智能体 CC 钩子（/health /api/core-memory /search /add /session/distill）
    + 写契约（/update /delete，回归集与 push_gate 走它）+ 运维探针（/livez /readyz /metrics）
  · /api 子应用 = 控制台 UI（frontend/js 以 /api 为根）实际调用的端点
两处共用同一套 handler，按 ``only`` 白名单各自注册，不再整表镜像。
"""
from __future__ import annotations

import logging
from fastapi import FastAPI

from ducky.hot import register_core_routes
from ducky.routes_v8 import register_v8_routes
from ducky.routes_clotho import register_clotho_routes
from ducky.hot.legacy_routes import register_legacy_routes
from ducky.extended.routes import register_extended_routes
from ducky.federation.routes import register_federation_routes
from ducky.routes_octopus import register_octopus_routes
from ducky.routes_config import register_config_routes
from ducky.routes_evolve import register_evolve_routes
from ducky.routes_knowledge import register_knowledge_routes
from ducky.routes_graph import register_graph_routes
from ducky.system_endpoints import assert_all_gated

logger = logging.getLogger("aiduMEM.RoutesRegistry")

# 扁平根路径：CC 钩子 + 写契约 + 运维
FLAT_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/health"), ("GET", "/livez"), ("GET", "/readyz"), ("GET", "/metrics"),
    ("GET", "/api/core-memory"), ("POST", "/search"), ("POST", "/add"),
    ("POST", "/session/distill"), ("POST", "/update"), ("POST", "/delete"),
})

# /api 子应用：控制台 UI（/login、/login/hint 由 api_server 自己注册）
UI_ROUTES: frozenset[tuple[str, str]] = frozenset({
    ("GET", "/health"), ("GET", "/config"), ("PUT", "/config/{section}"),
    ("POST", "/config/_speed"), ("POST", "/config/password"), ("POST", "/config/test"), ("GET", "/domains"),
    ("GET", "/crystals"), ("GET", "/dossier"), ("GET", "/events/history"),
    ("GET", "/evolve/report"), ("POST", "/evolve/feedback"),
    ("GET", "/facts"), ("GET", "/facts/categories"), ("GET", "/facts/entities/list"),
    ("GET", "/facts/trust-stats"), ("GET", "/federation/agents"),
    ("GET", "/governance/candidates"), ("POST", "/governance/review"),
    ("GET", "/graph/neighbors"), ("GET", "/graph/stats"), ("GET", "/graph/checkup"), ("GET", "/graph/nebula"), ("GET", "/knowledge/tree"),
    ("GET", "/opinions"), ("GET", "/opinions/aggregate"), ("POST", "/opinions/set"),
    ("GET", "/recent"), ("GET", "/scene"), ("GET", "/stats"), ("GET", "/usage"),
    ("GET", "/tombstones"), ("POST", "/tombstone/restore"),
    ("POST", "/search"), ("POST", "/search_trace"), ("POST", "/update"), ("POST", "/delete"),
    ("POST", "/reload"),
})


class _Only:
    """注册门面：只把白名单内的 (method, path) 真正挂到 app 上，其余 handler 不注册。"""

    def __init__(self, app, only: frozenset[tuple[str, str]]) -> None:
        self._app, self._only = app, only

    def __getattr__(self, name):
        return getattr(self._app, name)

    def _register(self, method: str, path: str, kwargs: dict):
        if (method, path) not in self._only:
            return lambda fn: fn
        return getattr(self._app, method.lower())(path, **kwargs)

    def get(self, path: str, **kw):
        return self._register("GET", path, kw)

    def post(self, path: str, **kw):
        return self._register("POST", path, kw)

    def put(self, path: str, **kw):
        return self._register("PUT", path, kw)

    def delete(self, path: str, **kw):
        return self._register("DELETE", path, kw)


def register_all_routes(app: FastAPI, get_memory_fn, get_db_fn, extract_entities_fn,
                        only: frozenset[tuple[str, str]] = FLAT_ROUTES) -> None:
    """按 ``only`` 白名单注册端点（默认扁平集；/api 子应用传 UI_ROUTES）。"""
    from ducky.scope_auth import ScopeRegistrar
    target = app
    app = _Only(ScopeRegistrar(app), only)

    register_core_routes(app)        # health / add / search / crud
    register_v8_routes(app)          # /session/distill
    register_clotho_routes(app)      # /api/core-memory
    register_extended_routes(app, get_memory_fn, get_db_fn, extract_entities_fn)  # /knowledge/tree
    register_legacy_routes(app)      # /facts* /scene
    register_federation_routes(app)  # /federation/agents
    register_octopus_routes(app)     # /crystals
    register_evolve_routes(app)      # /evolve/feedback /evolve/report
    register_config_routes(app)      # /config* /domains
    register_graph_routes(app)       # /graph/*
    register_knowledge_routes(app)   # /dossier

    # f0.3：系统级端点必须挂着各自的默认关闭特性闸，漏闸即启动失败。
    assert_all_gated(target)
    logger.info("✅ 路由注册完毕（%d 个白名单端点）", len(only))
