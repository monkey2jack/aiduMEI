"""
ducky.system_endpoints - system-only endpoint families (f0.3)
===============================================================

Every data endpoint of the engine is partitioned by the ``(user_id, bank_id)``
tenant axis.  The families listed in ``FAMILIES`` are not: they expose derived
data that is global to the whole instance (persona banks addressed by
enumerable autoincrement ids, skill crystals mined from every tenant's facts,
the server's own code graph, store-wide evolution statistics and the
store-wide evolution job, skill drafts in the shared ``skill_crystals``
table).

They are therefore **off by default**.  Each family has one explicit flag;
the route stays registered (so the route table, the OpenAPI document and the
MCP contract stay stable) but answers ``404 feature_disabled`` until the
deployment sets the flag to ``true``/``1``/``yes``/``on``.  The flag is read
on every request, so ``os.environ`` (including the ``.env`` that api_server
loads at start-up) is the single source of truth.

In OpenAPI the routes carry the ``system-only (not tenant-isolated)`` tag and
``x-aidumei-tenant-isolated: false`` / ``x-aidumei-feature-flag: <FLAG>``.

Endpoints used by the default integration paths stay on: ``/evolve/feedback``
and ``/evolve/episode/feedback`` (console, MCP) are per-memory / per-session
writes and are not part of any family here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from fastapi import Depends, HTTPException

SYSTEM_ONLY_TAG = "system-only (not tenant-isolated)"
_TRUE = frozenset({"1", "true", "yes", "on"})
_FLAG_ATTR = "__aidumei_system_flag__"


@dataclass(frozen=True)
class Family:
    name: str
    flag: str
    routes: frozenset[tuple[str, str]]
    reason: str


FAMILIES: tuple[Family, ...] = (
    Family(
        "persona", "AIDUMEM_PERSONA_ENABLED",
        frozenset({
            ("POST", "/persona/build"), ("GET", "/persona/banks"),
            ("GET", "/persona/detail"), ("POST", "/persona/retrieve"),
            ("POST", "/persona/rollback"), ("GET", "/persona/context"),
        }),
        "persona banks are instance-global; bank ids are enumerable autoincrement integers",
    ),
    Family(
        "crystals", "AIDUMEI_CRYSTALS_ENABLED",
        frozenset({
            ("GET", "/crystals"), ("POST", "/crystals/detect"), ("POST", "/crystals/use"),
            ("POST", "/crystals/prune"), ("POST", "/crystals/approve"),
        }),
        "skill crystals are mined across every tenant's facts and approved by global id",
    ),
    Family(
        "code_graph", "AIDUMEI_CODE_GRAPH_ENABLED",
        frozenset({("POST", "/code/impact"), ("GET", "/code/graph")}),
        "scans the server's own source tree under AIDUMEM_HOME",
    ),
    Family(
        "evolve_admin", "AIDUMEI_EVOLVE_ADMIN_ENABLED",
        frozenset({("GET", "/evolve/report"), ("POST", "/evolve/cycle")}),
        "store-wide search statistics and the store-wide salience maintenance job",
    ),
    Family(
        "skill_drafts", "AIDUMEI_SKILL_DRAFTS_ENABLED",
        frozenset({("POST", "/skill/grow"), ("GET", "/skill/drafts")}),
        "skill drafts live in the instance-global skill_crystals table",
    ),
)

FAMILY_BY_NAME: dict[str, Family] = {f.name: f for f in FAMILIES}
FAMILY_BY_ROUTE: dict[tuple[str, str], Family] = {
    route: family for family in FAMILIES for route in family.routes
}


def feature_enabled(flag: str) -> bool:
    """Explicit opt-in: only true/1/yes/on enable a family; unset means off."""
    return os.environ.get(flag, "").strip().lower() in _TRUE


def _gate(flag: str):
    def require_system_feature() -> None:
        if not feature_enabled(flag):
            raise HTTPException(status_code=404, detail={
                "code": "feature_disabled",
                "feature_flag": flag,
                "detail": (
                    f"System-only endpoint (not tenant-isolated), disabled by default. "
                    f"Set {flag}=true to enable it for this instance."
                ),
            })
    setattr(require_system_feature, _FLAG_ATTR, flag)
    return require_system_feature


def system_route(name: str, **route_kwargs):
    """Keyword arguments for ``@app.get/post(...)`` that gate and tag one family."""
    family = FAMILY_BY_NAME[name]
    merged = dict(route_kwargs)
    merged["dependencies"] = [*merged.get("dependencies", []), Depends(_gate(family.flag))]
    merged["tags"] = [*merged.get("tags", []), SYSTEM_ONLY_TAG]
    extra = dict(merged.get("openapi_extra") or {})
    extra.update({
        "x-aidumei-tenant-isolated": False,
        "x-aidumei-feature-flag": family.flag,
        "x-aidumei-default": "disabled",
    })
    merged["openapi_extra"] = extra
    return merged


class GatedRegistrar:
    """Pass-through facade over a FastAPI app for route modules that do not
    gate themselves: any ``get/post/put/delete/patch`` registration of a path
    listed in ``FAMILIES`` receives that family's gate and tags."""

    def __init__(self, app) -> None:
        self._app = app

    def __getattr__(self, name):
        return getattr(self._app, name)

    def _register(self, method: str, path: str, kwargs: dict):
        family = FAMILY_BY_ROUTE.get((method, path))
        if family is not None:
            kwargs = system_route(family.name, **kwargs)
        return getattr(self._app, method.lower())(path, **kwargs)

    def get(self, path: str, **kwargs):
        return self._register("GET", path, kwargs)

    def post(self, path: str, **kwargs):
        return self._register("POST", path, kwargs)

    def put(self, path: str, **kwargs):
        return self._register("PUT", path, kwargs)

    def delete(self, path: str, **kwargs):
        return self._register("DELETE", path, kwargs)

    def patch(self, path: str, **kwargs):
        return self._register("PATCH", path, kwargs)


def gate_status(app) -> dict[tuple[str, str], str]:
    """For every listed (method, path): 'gated', 'ungated' or 'missing' in *app*."""
    status = {route: "missing" for route in FAMILY_BY_ROUTE}
    for route in getattr(app, "routes", []):
        path = getattr(route, "path", None)
        for method in getattr(route, "methods", None) or ():
            key = (method, path)
            if key not in status:
                continue
            flag = FAMILY_BY_ROUTE[key].flag
            gated = any(getattr(dep.dependency, _FLAG_ATTR, None) == flag
                        for dep in getattr(route, "dependencies", []) or [])
            status[key] = "gated" if gated else "ungated"
    return status


def assert_all_gated(app) -> None:
    """Fail closed at start-up if a listed endpoint is registered without its gate."""
    bad = {route: state for route, state in gate_status(app).items() if state == "ungated"}
    if bad:
        raise RuntimeError(f"system-only endpoints registered without their feature gate: {sorted(bad)}")
