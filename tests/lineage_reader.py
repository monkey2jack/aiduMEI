"""Test-only reader for the memory_lineage table.

f0.6+++ WP2 removed ``ducky.memory_lineage.get_memory_lineage`` from production
(its only callers were removed HTTP routes). Lineage *writes* are still a kept
feature, so tests read the chain straight from the table with this helper.
"""
from __future__ import annotations

from typing import Any


def get_memory_lineage(memory_id: str) -> list[dict[str, Any]]:
    if not memory_id:
        return []
    from ducky.memory_lineage import ensure_lineage_schema
    from ducky.utils import get_facts_conn
    ensure_lineage_schema()
    conn = get_facts_conn()
    try:
        rows = conn.execute(
            """SELECT lineage_id, memory_id, version, content_hash, previous_version_hash,
                      action, actor, source, diff_summary, created_at
               FROM memory_lineage WHERE memory_id=? ORDER BY version ASC""",
            (memory_id,),
        ).fetchall()
    finally:
        conn.close()
    keys = ("lineage_id", "memory_id", "version", "content_hash", "previous_version_hash",
            "action", "actor", "source", "diff_summary", "created_at")
    return [{**dict(zip(keys, r)), "created_at": str(r[9])} for r in rows]
