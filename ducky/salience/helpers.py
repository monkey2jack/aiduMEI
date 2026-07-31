import sqlite3
from ducky.utils import get_facts_conn

def _extract_key_facts(category: str, limit: int = 100) -> list:
    """提取最新的重要事实 (原 legacy/helpers，现作为 crud 依赖)"""
    conn = get_facts_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, fact_value, timestamp FROM facts WHERE category = ? ORDER BY timestamp DESC LIMIT ?",
        (category, limit)
    )
    rows = cur.fetchall()
    return [{"id": r[0], "fact_value": r[1], "timestamp": r[2]} for r in rows]
