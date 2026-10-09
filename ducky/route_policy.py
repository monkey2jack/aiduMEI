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
GET /add/job/{job_id}
POST /search
POST /search_trace
GET /gate
GET /recent
GET /stats
GET /tombstones
GET /events/history
GET /opinions
GET /opinions/aggregate
POST /facts/inject-context
POST /ignition_test
GET /workspace
POST /recall_chain
POST /broadcast_expand
POST /jlens
POST /session/search
GET /session/report
GET /api/core-memory/{block_key}/history
GET /api/core-memory
GET /api/core-memory/{block_key}
POST /api/core-memory/inject
GET /api/checkpoint/latest
GET /api/checkpoint/{session_id}
POST /api/checkpoint/inject
GET /persona/ai-self
GET /facts/preferences
GET /knowledge/tree
GET /facts/delta
GET /search/deep
GET /facts
GET /facts/categories
GET /facts/entities
GET /facts/related
GET /facts/reason
GET /facts/entities/list
GET /facts/trust-stats
GET /facts/search
GET /observe
GET /observe/related
GET /tree/nodes
GET /raw/stats
GET /reflect/list
GET /reflect/context
GET /memory/types
GET /memory/types/query
GET /knowledge/{memory_id}/evolution
GET /pantheon/hall/{user_id}
'''),
    **_routes('scope', 'write', '''
POST /add
POST /tombstone/restore
POST /opinions/set
POST /update
POST /workspace/clear
POST /session/start
POST /session/pin
POST /session/unpin
POST /session/distill
POST /session/end
POST /graduate
POST /api/core-memory/core_current_project/refresh
PUT /api/core-memory/{block_key}
POST /api/checkpoint
POST /facts/preference
POST /facts/expire
POST /facts/add
POST /prune/contradiction-v2
POST /facts/feedback
POST /prune/contradiction
POST /observe/consolidate
POST /conflict/resolve
POST /tree/node
POST /add/raw
POST /evolve/feedback
POST /api/obsidian/sync
POST /reflect
POST /memory/types/backfill
POST /memory/types/reset
POST /memory/refine
POST /memory/refine/apply
POST /memory/refine/rollback
POST /pantheon/hall
'''),
    **_routes('scope', 'delete', '''
POST /delete
DELETE /delete
POST /delete_all
DELETE /api/checkpoint/cleanup
'''),
    **_routes('owner', 'read', '''
GET /session/list
GET /self-edit/edits
GET /memory/refinements
'''),
    **_routes('scope', 'export', '''
GET /dossier
'''),
    **_routes('optional_scope', 'read', '''
GET /scene
GET /persona
'''),
    **_routes('optional_scope', 'write', '''
POST /scene/cluster
POST /persona/refresh
POST /add/coalesce/flush
'''),
    **_routes('default_owner', 'write', '''
POST /persona/ai-self/add
'''),
    **_routes('manual', 'resource', '''
POST /self-edit/rollback
GET /governance/candidates
POST /governance/review
POST /federation/agents/register
POST /federation/agents/heartbeat
POST /federation/agents/deactivate
GET /federation/recall
POST /federation/facts/add
GET /federation/broadcast
GET /federation/awareness
POST /federation/grants
GET /federation/grants
POST /federation/grants/revoke
GET /federation/lineage
GET /federation/lineage/verify
POST /pantheon/hall/{user_id}/deactivate
POST /pantheon/grant
POST /pantheon/grant/{grant_id}/revoke
GET /pantheon/grants
'''),
    **_routes('admin', 'admin', '''
POST /evolve/episode/feedback
GET /diagnostics
GET /metrics
GET /add/coalesce
GET /add/coalesce/stats
GET /usage
POST /reload
GET /api/autodream/status
POST /api/autodream/trigger
GET /api/autodream/report
GET /auto-memory/status
POST /auto-memory/trigger
POST /facts/compress
POST /facts/tags/generate
GET /facts/tags
POST /skill/discover
GET /federation/agents
GET /federation/tiers
POST /federation/migrate
GET /crystals
POST /crystals/detect
POST /crystals/use
POST /crystals/prune
POST /crystals/approve
POST /code/impact
GET /code/graph
GET /evolve/report
POST /evolve/cycle
GET /domains
GET /config
PUT /config/{section}
GET /config/_speed
POST /config/_speed
POST /config/password
POST /skill/grow
GET /skill/drafts
POST /persona/build
GET /persona/banks
GET /persona/detail
POST /persona/retrieve
POST /persona/rollback
GET /persona/context
GET /dossier/scope-hint
GET /pantheon/halls
'''),
}
