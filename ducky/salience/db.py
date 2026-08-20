"""ducky.salience.db — 表结构 / 迁移"""
from __future__ import annotations

import logging

from ducky.utils import get_salience_conn

logger = logging.getLogger("aiduMEM.salience")

def _ensure_db():
    """确保 salience 数据库存在 + v8.3.0 迁移"""
    conn = get_salience_conn()

    # 主表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS salience (
            memory_id TEXT PRIMARY KEY,
            salience REAL NOT NULL DEFAULT 0.5,
            last_access REAL NOT NULL,
            access_count INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_salience ON salience(salience)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_last_access ON salience(last_access)")

    # v8.3.0 迁移: 添加 lane 列
    cols = [row[1] for row in conn.execute("PRAGMA table_info(salience)").fetchall()]
    if "lane" not in cols:
        conn.execute("ALTER TABLE salience ADD COLUMN lane TEXT DEFAULT 'general'")
        logger.info("✅ salience 表已添加 lane 列")

    # v8.3.0 迁移: 添加内容缓存列（用于矛盾检测）
    if "content_preview" not in cols:
        conn.execute("ALTER TABLE salience ADD COLUMN content_preview TEXT DEFAULT ''")
        logger.info("✅ salience 表已添加 content_preview 列")

    # v20 P0-2 迁移: 补作用域列。conflict.py 此前按 lane 全库配对反义词，
    # 甲库一句「要」能把乙库一句「不要」的显著性腰斩——跨库写污染。
    # 存量行归 default 域（v19 全库本就是单一默认域），零丢失。
    if "user_id" not in cols:
        conn.execute("ALTER TABLE salience ADD COLUMN user_id TEXT NOT NULL DEFAULT 'default'")
        logger.info("✅ salience 表已添加 user_id 列")
    if "bank_id" not in cols:
        conn.execute("ALTER TABLE salience ADD COLUMN bank_id TEXT NOT NULL DEFAULT 'default'")
        logger.info("✅ salience 表已添加 bank_id 列")

    # v8.3.0 每日生长指标表
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_metrics (
            date TEXT PRIMARY KEY,
            total_memories INTEGER NOT NULL,
            avg_confidence REAL NOT NULL,
            active_lanes INTEGER NOT NULL,
            high_confidence_count INTEGER NOT NULL,
            recall_rate REAL NOT NULL,
            salience_avg REAL NOT NULL,
            salience_low_count INTEGER NOT NULL,
            decayed_count INTEGER NOT NULL,
            evicted_count INTEGER NOT NULL
        )
    """)

    conn.commit()
    conn.close()


def ensure_db() -> None:
    """公开别名（模块 import 时自动调用）。"""
    _ensure_db()
