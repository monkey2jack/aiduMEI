"""ducky.salience.core — 注册 / 访问 boost / 衰减 / 查询"""
from __future__ import annotations

import logging
import math
import sqlite3
import time
from typing import Optional

from ducky.salience.config import (
    ACCESS_BOOST,
    DECAY_HALF_LIFE_DAYS,
    DECAY_RATE,
    DEFAULT_LANE,
    IDLE_EVICT_DAYS,
    LANE_DECAY_MULTIPLIER,
    LANE_KEYWORDS,
    SALIENCE_FLOOR,
)
from ducky.utils import get_salience_conn

logger = logging.getLogger("aiduMEM.salience")

def _detect_lane(memory_content: str) -> str:
    """从记忆内容自动检测 Lane"""
    if not memory_content:
        return DEFAULT_LANE
    scores = {}
    for lane, keywords in LANE_KEYWORDS.items():
        scores[lane] = sum(1 for kw in keywords if kw in memory_content)
    if scores:
        best = max(scores, key=scores.get)
        if scores[best] > 0:
            return best
    return DEFAULT_LANE


def on_memory_added(memory_id: str, initial_salience: float = 0.5,
                     lane: Optional[str] = None, content: str = "",
                     preserve_heat: bool = False):
    """新增记忆时注册 salience（v8.3.0 支持 Lane + 内容缓存）

    保留已有热度的合并路径：self-edit 合并已有记忆时传入 preserve_heat=True，
    不会把 access_count 清零（否则合并会悄悄抹掉检索热度信号）。
    """
    if preserve_heat:
        conn = get_salience_conn()
        row = conn.execute(
            "SELECT access_count, salience FROM salience WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        if row:
            # 已有热度：保留访问次数与显著性，仅刷新 content_preview/last_access
            now = time.time()
            resolved_lane = lane or DEFAULT_LANE
            conn.execute(
                "UPDATE salience SET last_access=?, content_preview=?, lane=? WHERE memory_id=?",
                (now, content[:200] if content else "", resolved_lane, memory_id),
            )
            conn.commit()
            conn.close()
            logger.debug(f"salience 合并登记（保留热度 access_count={row['access_count']}）: {memory_id[:16]}")
            return

    now = time.time()
    if lane is None and content:
        lane = _detect_lane(content)
    if lane is None:
        lane = DEFAULT_LANE
    content_preview = content[:200] if content else ""

    conn = get_salience_conn()
    conn.execute(
        "INSERT OR REPLACE INTO salience "
        "(memory_id, salience, last_access, access_count, created_at, lane, content_preview) "
        "VALUES (?, ?, ?, 0, ?, ?, ?)",
        (memory_id, initial_salience, now, now, lane, content_preview)
    )
    conn.commit()
    conn.close()
    logger.debug(f"salience 注册: {memory_id[:16]} → {initial_salience} [{lane}]")


def on_memory_accessed(memory_id: str):
    """记忆被访问时 boost 显著性"""
    now = time.time()
    conn = get_salience_conn()
    row = conn.execute(
        "SELECT salience, last_access, access_count FROM salience WHERE memory_id = ?",
        (memory_id,)
    ).fetchone()
    if row:
        old_s, old_ts, cnt = row
        days_elapsed = (now - old_ts) / 86400
        decayed = old_s * math.exp(-DECAY_RATE * days_elapsed)
        new_s = min(1.0, decayed + ACCESS_BOOST)
        conn.execute(
            "UPDATE salience SET salience = ?, last_access = ?, access_count = ? WHERE memory_id = ?",
            (new_s, now, cnt + 1, memory_id)
        )
    else:
        conn.execute(
            "INSERT INTO salience (memory_id, salience, last_access, access_count, created_at) "
            "VALUES (?, ?, ?, 0, ?)",
            (memory_id, 0.5, now, now)
        )
    conn.commit()
    conn.close()


def decay_all() -> dict:
    """衰减所有记忆的显著性（v8.3.0: Lane 感知乘系数），返回被踢出的 ID 列表"""
    now = time.time()
    conn = get_salience_conn()
    rows = conn.execute(
        "SELECT memory_id, salience, last_access, lane FROM salience"
    ).fetchall()

    evicted = []
    updated = 0
    for mid, old_s, last_ts, lane in rows:
        days_elapsed = (now - last_ts) / 86400
        # v8.3.0: Lane 感知乘系数
        multiplier = LANE_DECAY_MULTIPLIER.get(lane or DEFAULT_LANE, 1.0)
        effective_rate = DECAY_RATE * multiplier
        new_s = old_s * math.exp(-effective_rate * days_elapsed)
        days_idle = (now - last_ts) / 86400

        if new_s < SALIENCE_FLOOR and days_idle > IDLE_EVICT_DAYS:
            evicted.append(mid)
        else:
            conn.execute(
                "UPDATE salience SET salience = ? WHERE memory_id = ?",
                (new_s, mid)
            )
            updated += 1

    conn.commit()
    conn.close()

    if evicted or updated:
        logger.info(f"salience 衰减完成: {updated} 条更新, {len(evicted)} 条踢出")
    return {"updated": updated, "evicted": evicted}


def get_salience(memory_id: str) -> float:
    """获取记忆当前显著性"""
    conn = get_salience_conn()
    row = conn.execute("SELECT salience FROM salience WHERE memory_id = ?", (memory_id,)).fetchone()
    conn.close()
    return row[0] if row else 0.5


def get_salience_record(memory_id: str) -> dict:
    """获取记忆显著性完整记录"""
    conn = get_salience_conn()
    row = conn.execute("SELECT * FROM salience WHERE memory_id = ?", (memory_id,)).fetchone()
    conn.close()
    return dict(row) if row else {}


def get_stats() -> dict:
    """显著性统计（v8.3.0: 含 Lane 分布）"""
    conn = get_salience_conn()
    total = conn.execute("SELECT COUNT(*) FROM salience").fetchone()[0]
    low = conn.execute(
        "SELECT COUNT(*) FROM salience WHERE salience < ?", (SALIENCE_FLOOR,)
    ).fetchone()[0]
    avg = conn.execute("SELECT AVG(salience) FROM salience").fetchone()[0] or 0

    # v8.3.0 Lane 分布
    lane_rows = conn.execute(
        "SELECT lane, COUNT(*) FROM salience GROUP BY lane ORDER BY COUNT(*) DESC"
    ).fetchall()
    lane_distribution = {lane: cnt for lane, cnt in lane_rows}

    # v8.3.0 各 Lane 平均显著性
    lane_avg_rows = conn.execute(
        "SELECT lane, AVG(salience) FROM salience GROUP BY lane"
    ).fetchall()
    lane_avg = {lane: round(avg_s, 3) for lane, avg_s in lane_avg_rows}

    conn.close()
    return {
        "total_tracked": total,
        "below_floor": low,
        "avg_salience": round(avg, 3),
        "floor": SALIENCE_FLOOR,
        "half_life_days": DECAY_HALF_LIFE_DAYS,
        "lane_distribution": lane_distribution,
        "lane_avg_salience": lane_avg,
    }
