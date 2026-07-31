"""ducky.salience.metrics — 每日生长指标"""
from __future__ import annotations

import logging
import sqlite3
import time

from ducky.salience.config import SALIENCE_FLOOR
from ducky.utils import get_salience_conn

logger = logging.getLogger("aiduMEM.salience")

def record_daily_metrics(decayed: int = 0, evicted: int = 0) -> dict:
    """v8.3.0: 记录每日生长指标到 daily_metrics 表"""
    today = time.strftime("%Y-%m-%d")
    conn = get_salience_conn()

    total = conn.execute("SELECT COUNT(*) FROM salience").fetchone()[0]
    avg_s = conn.execute("SELECT AVG(salience) FROM salience").fetchone()[0] or 0
    high_conf = conn.execute(
        "SELECT COUNT(*) FROM salience WHERE salience >= 0.8"
    ).fetchone()[0]
    low_conf = conn.execute(
        "SELECT COUNT(*) FROM salience WHERE salience < ?", (SALIENCE_FLOOR,)
    ).fetchone()[0]

    # 活跃 Lane 数（≥3 条记忆的 lane）
    lane_counts = conn.execute(
        "SELECT lane, COUNT(*) as cnt FROM salience GROUP BY lane HAVING cnt >= 3"
    ).fetchall()
    active_lanes = len(lane_counts)

    # 召回率（有访问记录的占比）
    recalled = conn.execute(
        "SELECT COUNT(*) FROM salience WHERE access_count > 0"
    ).fetchone()[0]
    recall_rate = recalled / total if total > 0 else 0.0

    conn.execute(
        "INSERT OR REPLACE INTO daily_metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (today, total, round(avg_s, 3), active_lanes, high_conf,
         round(recall_rate, 3), round(avg_s, 3), low_conf, decayed, evicted)
    )
    conn.commit()
    conn.close()

    metrics = {
        "date": today,
        "total": total,
        "avg_salience": round(avg_s, 3),
        "active_lanes": active_lanes,
        "high_confidence": high_conf,
        "recall_rate": round(recall_rate, 3),
        "decayed": decayed,
        "evicted": evicted,
    }
    logger.info(f"📊 每日指标: total={total} avg_s={avg_s:.3f} lanes={active_lanes} "
                f"high_conf={high_conf} recall={recall_rate:.1%}")
    return metrics


def get_historical_metrics(days: int = 7) -> list:
    """v8.3.0: 获取历史生长指标"""
    conn = get_salience_conn()
    rows = conn.execute(
        "SELECT * FROM daily_metrics ORDER BY date DESC LIMIT ?", (days,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
