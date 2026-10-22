"""ducky.salience.db — 表结构 / 迁移"""
from __future__ import annotations

import logging
import time

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
    # v20.4.0-alpha（P1-8）：进化循环候选下推的 boost 析取支按 access_count 过滤，
    # 无此索引时 10 万行库上该支退回全表扫（tests/test_v20_4_evolve_index.py 钉计划）。
    conn.execute("CREATE INDEX IF NOT EXISTS idx_access_count ON salience(access_count)")

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

    # f0.6 W-02（memoh 审计 P0-1）：衰减时间基准列。此前的 decay_all 每次都
    # 用「已衰减过的 salience」再乘同一个 days_elapsed（以 last_access 计），
    # 衰减强度随 cron 运行次数二次累积——当日访问过的记忆跑 9 次日更也会
    # 跌破地板。salience_at 记录上次衰减计算时刻，衰减从基准点起算：
    # s(t) = s_salience_at * exp(-λ·(now - salience_at))，与运行次数解耦。
    # 存量回填：无基准的行以「当前值 + last_access」反推——直接把 salience_at
    # 设为 now 会把历史欠账一笔勾销；设为 last_access 又会二次重罚。折中取
    # min(now, last_access + 1 天) 的中点近似，误差被一周内的 dry-run 观察兜底。
    if "salience_at" not in cols:
        conn.execute("ALTER TABLE salience ADD COLUMN salience_at REAL")
        conn.execute(
            "UPDATE salience SET salience_at = ? WHERE salience_at IS NULL",
            (time.time(),),
        )
        logger.info("✅ salience 表已添加 salience_at 列（存量基准=迁移时刻）")

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
