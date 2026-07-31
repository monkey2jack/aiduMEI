"""ducky.salience.audit — 健康审计（噩梦推演）"""
from __future__ import annotations

import logging

from ducky.utils import get_salience_conn

logger = logging.getLogger("aiduMEM.salience")

_CASCADE_THRESHOLD = 5     # 低显著性记忆 ≥5 条 → error_cascade
_FADE_THRESHOLD = 3        # 零召回+低显著性 ≥3 条 → memory_fade
_OVERFLOW_THRESHOLD = 200  # 总记忆超过此值建议清理


def audit_health_anomalies() -> dict:
    """健康审计：error_cascade / memory_fade / overflow"""
    conn = get_salience_conn()
    alerts = []

    low = conn.execute(
        "SELECT COUNT(*) FROM salience WHERE salience < 0.3"
    ).fetchone()[0]
    if low >= _CASCADE_THRESHOLD:
        alerts.append(f"🔥 error_cascade: {low} 条记忆显著低于 0.3")

    fade = conn.execute(
        "SELECT COUNT(*) FROM salience WHERE access_count = 0 AND salience < 0.25"
    ).fetchone()[0]
    if fade >= _FADE_THRESHOLD:
        alerts.append(f"🌫️ memory_fade: {fade} 条孤立低质记忆")

    total = conn.execute("SELECT COUNT(*) FROM salience").fetchone()[0]
    if total > _OVERFLOW_THRESHOLD:
        alerts.append(f"📦 总记忆 {total} 条，建议清理")

    conn.close()

    if alerts:
        logger.warning("👻 健康审计触发：%s", " | ".join(alerts))
    return {"triggered": bool(alerts), "alerts": alerts, "total_memories": total}
