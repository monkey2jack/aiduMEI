"""
ducky.salience — 显著性引擎子包（v9.1 一次到位）
真源拆自 memory_salience.py；对外经 ducky.memory_salience 兼容 re-export。
"""
from ducky.salience.audit import audit_health_anomalies
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
from ducky.salience.conflict import detect_conflicts, resolve_conflict_salience
from ducky.salience.core import (
    _detect_lane,
    decay_all,
    delete_salience,
    get_salience,
    get_stats,
    on_memory_accessed,
    on_memory_added,
    prune_orphan_salience,
)
from ducky.salience.db import _ensure_db, ensure_db
from ducky.salience.metrics import get_historical_metrics, record_daily_metrics
from ducky.salience.lesson_verify import verify_lessons_closed

# 模块加载时确保表结构（与旧 memory_salience 行为一致）
_ensure_db()

__all__ = [
    "on_memory_added",
    "on_memory_accessed",
    "decay_all",
    "delete_salience",
    "prune_orphan_salience",
    "get_salience",
    "get_stats",
    "record_daily_metrics",
    "get_historical_metrics",
    "detect_conflicts",
    "resolve_conflict_salience",
    "audit_health_anomalies",
    "_detect_lane",
    "_ensure_db",
    "ensure_db",
    "SALIENCE_FLOOR",
    "DECAY_HALF_LIFE_DAYS",
    "LANE_DECAY_MULTIPLIER",
    "DEFAULT_LANE",
    "verify_lessons_closed",
]
