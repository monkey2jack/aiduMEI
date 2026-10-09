"""Evidence checks for legacy fallback around journalled SDK pipelines.

Callers hold the scope lock across checkpoint, work and fallback decision.
Read errors are never interpreted as evidence that no SDK call took place.
This module does not change journal state; the enclosing request/job records
an interrupted pipeline for repair even if its SDK child already acknowledged.
"""
from __future__ import annotations

from ducky.bank_contract import make_scope
from ducky.mutation_journal import MutationUncertain, _check_scope, _db, scope_lock
from ducky.scope_sql import scope_clause


def sdk_attempt_count(user_id, bank_id):
    scope = make_scope(user_id, bank_id)
    clause, params = scope_clause(scope)
    with scope_lock(scope.user_id, scope.bank_id), _db() as conn:
        return conn.execute("SELECT COUNT(*) FROM mutations WHERE kind IN ('mem0_add','mem0_update') "
                            + clause, params).fetchone()[0]


def require_no_sdk_since(before, user_id, bank_id, error):
    """Allow legacy fallback only while durable evidence shows no SDK attempt.

    Runtime SDK add/update are journalled. Any attempt (including success) rules
    out repeating a pipeline whose later step failed. A pre-existing scope debt
    also rules out fallback, even if this invocation has not made an SDK call.
    """
    if isinstance(error, MutationUncertain):
        raise error
    scope = make_scope(user_id, bank_id)
    with scope_lock(scope.user_id, scope.bank_id):
        _check_scope(scope.user_id, scope.bank_id)
        if sdk_attempt_count(scope.user_id, scope.bank_id) == before:
            return
        clause, params = scope_clause(scope)
        with _db() as conn:
            row = conn.execute("SELECT id FROM mutations WHERE kind IN ('mem0_add','mem0_update') "
                               + clause + " ORDER BY rowid DESC LIMIT 1", params).fetchone()
        mid = row['id'] if row else 'sdk-evidence-missing'
        raise MutationUncertain(mid, 'post_sdk_failure') from error
