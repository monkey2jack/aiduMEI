"""Resolve explicitly registered entity aliases inside an exact memory scope."""
from __future__ import annotations

import json
import logging
import re

from ducky.bank_contract import make_scope
from ducky.utils import get_facts_conn
from ducky.scope_sql import scope_clause

logger = logging.getLogger("aiduMEI.aliases")


def resolve_query_aliases(query: str, user_id: str, bank_id: str) -> str:
    """No inferred aliases, cross-scope joins, or recursive query expansion."""
    scope = make_scope(user_id, bank_id)
    clause, params = scope_clause(scope)
    try:
        conn = get_facts_conn()
        rows = list(conn.execute(
            "SELECT name, aliases FROM entities WHERE 1=1 " + clause + " "
            "AND aliases<>'' ORDER BY entity_id LIMIT 1000",
            params,
        ).fetchall())
        # Hosts can register a confirmed mapping through the existing facts API.
        # Category/key are explicit intent; ordinary prose is never mined here.
        rows.extend(conn.execute(
            "SELECT fact_key, fact_value FROM facts WHERE 1=1 " + clause + " "
            "AND category='entity_alias' AND epistemic_mode='user_provided' "
            "AND confidence>=90 AND archived=0 AND superseded_by IS NULL "
            "AND (expires_at IS NULL OR expires_at='') "
            "AND (valid_to IS NULL OR valid_to='') AND (valid_from IS NULL OR valid_from='') "
            "ORDER BY id LIMIT 1000", params,
        ).fetchall())
    except Exception as exc:
        logger.debug("Entity aliases unavailable: %s", type(exc).__name__)
        return query
    mapping: dict[str, set[str]] = {}
    for row in rows:
        name, raw = row[0], row[1]
        if not isinstance(name, str) or not 2 <= len(name) <= 80:
            continue
        try:
            aliases = json.loads(raw)
        except (ValueError, TypeError):
            aliases = re.split(r"[,，;；|\n]", str(raw))
        if not isinstance(aliases, list):
            continue
        for alias in aliases[:30]:
            if isinstance(alias, str) and 2 <= len(alias.strip()) <= 80:
                mapping.setdefault(alias.strip(), set()).add(name)
    valid = {a: next(iter(names)) for a, names in mapping.items() if len(names) == 1}
    if not valid:
        return query
    patterns = []
    for alias in sorted(valid, key=len, reverse=True):
        pattern = re.escape(alias)
        if alias.isascii():
            pattern = r"(?<![A-Za-z0-9_])" + pattern + r"(?![A-Za-z0-9_])"
        patterns.append(pattern)
    return re.sub("|".join(patterns), lambda m: valid[m.group()], query, count=4)
