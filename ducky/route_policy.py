"""Explicit resource policy for HTTP routes (including /api aliases).

This is deliberately not inferred from parameter names or HTTP verbs. A new
route must be classified here before it can start serving. ``manual`` means
credential binding here plus canonical-resource authorization in the handler;
``admin`` denotes instance-wide state, not a tenant operation.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class RoutePolicy:
    resource: str
    action: str


def _routes(resource, action, lines):
    return {(method.lower(), path): RoutePolicy(resource, action)
            for method, path in (line.split() for line in lines.splitlines() if line.strip())}


POLICIES = {
    **_routes('public', 'read', '''
GET /health
GET /livez
GET /readyz
'''),
    **_routes('scope', 'read', '''
POST /search
POST /search_trace
GET /recent
GET /stats
GET /tombstones
GET /events/history
GET /opinions
GET /opinions/aggregate
GET /graph/neighbors
GET /graph/stats
GET /graph/checkup
GET /graph/nebula
GET /api/core-memory
GET /knowledge/tree
GET /facts
GET /facts/categories
GET /facts/entities/list
GET /facts/trust-stats
'''),
    **_routes('scope', 'write', '''
POST /add
POST /tombstone/restore
POST /opinions/set
POST /update
POST /session/distill
POST /evolve/feedback
'''),
    **_routes('scope', 'delete', '''
POST /delete
'''),
    **_routes('scope', 'export', '''
GET /dossier
'''),
    **_routes('optional_scope', 'read', '''
GET /scene
'''),
    **_routes('manual', 'resource', '''
GET /governance/candidates
POST /governance/review
'''),
    **_routes('admin', 'admin', '''
GET /metrics
GET /usage
POST /reload
GET /federation/agents
GET /crystals
GET /evolve/report
GET /domains
GET /config
PUT /config/{section}
POST /config/_speed
POST /config/password
POST /config/test
'''),
}
