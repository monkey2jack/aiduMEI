"""f0.7+ · forget (P8) and the write-policy shadow log (P1/P4/P5 review).

Endpoints (under /api, console session or Bearer):
  POST /forget                 delete memories for good (+ optional "never store" rule)
                               body {user_id, bank_id, memory_ids?, query?, never_store?}
                               - query without memory_ids only lists candidates (never deletes)
                               - each memory id goes through the same cascade as /delete
                                 (vector/FTS/facts/verbatim/derived summaries/… + our own log)
  GET  /forget/rules           list "never store" rules of a scope
  POST /forget/rules/delete    drop one rule {user_id, bank_id, rule_id}
  GET  /write-policy/verdicts  shadow verdicts (summary=1 for counts) for the misjudgment report
"""
from __future__ import annotations

import logging

from typing import Annotated

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ducky.api_models import DEFAULT_BANK_ID, DEFAULT_USER_ID, ID_FIELD_MAX_CHARS
from ducky.scope_auth import require_scope_access

logger = logging.getLogger("aiduMEM.write_policy")


class ForgetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(default=DEFAULT_USER_ID, max_length=ID_FIELD_MAX_CHARS)
    bank_id: str = Field(default=DEFAULT_BANK_ID, max_length=ID_FIELD_MAX_CHARS)
    caller_user_id: str = Field(default="", max_length=ID_FIELD_MAX_CHARS)
    memory_ids: list[Annotated[str, Field(max_length=ID_FIELD_MAX_CHARS)]] = Field(default_factory=list,
                                                                                   max_length=200)
    query: str = Field(default="", max_length=500)
    never_store: str = Field(default="", max_length=200)


class RuleDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(default=DEFAULT_USER_ID, max_length=ID_FIELD_MAX_CHARS)
    bank_id: str = Field(default=DEFAULT_BANK_ID, max_length=ID_FIELD_MAX_CHARS)
    caller_user_id: str = Field(default="", max_length=ID_FIELD_MAX_CHARS)
    rule_id: int


def _scope(user_id: str, bank_id: str):
    from ducky.bank_contract import make_scope
    from ducky.mem0_runtime import _normalize_user_id
    scope = make_scope(user_id, bank_id)
    return (_normalize_user_id(scope.user_id) if scope.user_id else DEFAULT_USER_ID), scope.bank_id


def _candidates(query: str, user_id: str, bank_id: str) -> list[dict]:
    from ducky.mem0_runtime import get_memory
    from ducky.bank_contract import vector_scope_filters
    raw = get_memory().search(query, filters=vector_scope_filters(user_id, bank_id), top_k=10)
    rows = raw.get("results", raw) if isinstance(raw, dict) else raw
    out = []
    for r in rows or []:
        if isinstance(r, dict) and r.get("id"):
            out.append({"id": r["id"], "preview": " ".join(str(r.get("memory") or "").split())[:80],
                        "score": r.get("score")})
    return out


def register_write_policy_routes(app: FastAPI) -> None:
    @app.post("/forget")
    def forget(req: ForgetRequest):
        user_id, bank_id = _scope(req.user_id, req.bank_id)
        require_scope_access(user_id, req.caller_user_id, bank_id=bank_id, action="delete")
        from ducky import write_policy
        if not req.memory_ids:
            if not req.query.strip():
                raise HTTPException(400, "memory_ids or query required")
            # Listing only: deleting by a fuzzy query is how unrelated memories get lost.
            return {"status": "candidates", "dry_run": True,
                    "candidates": _candidates(req.query, user_id, bank_id),
                    "hint": "review, then call again with memory_ids"}
        from ducky.wal_engine import cascade_delete_memory
        results, failed = [], 0
        for mid in dict.fromkeys(str(m).strip() for m in req.memory_ids if str(m).strip()):
            res = cascade_delete_memory(mid, user_id=user_id, bank_id=bank_id)
            st = res.get("status", "failed")
            failed += st not in ("committed", "not_found")
            results.append({"memory_id": mid, "status": st,
                            "failed_layers": [f.get("layer") for f in res.get("failed_layers", [])]})
        rule_id = None
        if req.never_store.strip():
            try:
                rule_id = write_policy.add_rule(user_id, bank_id, req.never_store,
                                                created_by=req.caller_user_id or "owner")
            except ValueError as exc:
                raise HTTPException(400, str(exc))
        return {"status": "partial" if failed else "ok", "deleted": results, "rule_id": rule_id,
                "rule_mode": write_policy.policy_mode(),
                "note": "deleted rows go to tombstones (restorable until TTL); the rule is checked at write"}

    @app.get("/forget/rules")
    def forget_rules(user_id: str = DEFAULT_USER_ID, bank_id: str = DEFAULT_BANK_ID, caller_user_id: str = ""):
        user_id, bank_id = _scope(user_id, bank_id)
        require_scope_access(user_id, caller_user_id, bank_id=bank_id, action="read")
        from ducky import write_policy
        return {"status": "ok", "rules": write_policy.list_rules(user_id, bank_id)}

    @app.post("/forget/rules/delete")
    def forget_rule_delete(req: RuleDeleteRequest):
        user_id, bank_id = _scope(req.user_id, req.bank_id)
        require_scope_access(user_id, req.caller_user_id, bank_id=bank_id, action="write")
        from ducky import write_policy
        return {"status": "ok", "deleted": write_policy.delete_rule(user_id, bank_id, req.rule_id)}

    @app.get("/write-policy/verdicts")
    def write_policy_verdicts(user_id: str = DEFAULT_USER_ID, bank_id: str = DEFAULT_BANK_ID,
                              since: str = "", verdict: str = "", reason: str = "", limit: int = 100,
                              summary: bool = False, caller_user_id: str = ""):
        user_id, bank_id = _scope(user_id, bank_id)
        require_scope_access(user_id, caller_user_id, bank_id=bank_id, action="read")
        from ducky import write_policy
        if summary:
            return {"status": "ok", **write_policy.summary(user_id, bank_id, since=since)}
        return {"status": "ok", "verdicts": write_policy.list_verdicts(
            user_id, bank_id, since=since, verdict=verdict, reason=reason, limit=limit)}
