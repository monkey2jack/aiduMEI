#!/usr/bin/env python3
"""
ducky.evolve_mem — EvolveMem 检索自进化引擎 (v18.1 Zeus-Beta)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
融合来源: SimpleMem EvolveMem 设计哲学 (3.7k⭐)

核心思想：
  每次记忆召回后，记录搜索质量信号（命中数 / 相关分 / 用户反馈）。
  周期性地把这些信号转化为结构调整：
    - 高频命中 → 提升 salience + 拆分（防止单条记忆承载过多语义）
    - 低命中 / 低分 → 降低 salience + 合并（避免碎片化）
    - 用户标记「有用/无用」→ 直接 ±bonus 写入 salience

三张表：
  evolve_queries     — 每次搜索的质量日志
  evolve_feedback    — 用户显式反馈（有用/无用/修正）
  evolve_adjustments — 自动调整动作日志

对外暴露：
  log_search_quality(query, results, ms)   → 记录一次搜索
  record_feedback(memory_id, signal)       → 记录用户反馈
  run_evolution_cycle()                    → 执行一次进化循环
  get_evolve_report()                      → 获取进化报告
  ensure_evolve_schema()                   → 建表（幂等）
"""
from __future__ import annotations

import logging
import math
import sqlite3
import time
from typing import Literal

from ducky.utils import DATA_DIR, get_salience_conn

import os

EVOLVE_DB_PATH = os.path.join(DATA_DIR, "evolve_mem.db")

logger = logging.getLogger("aiduMEM.evolve")

# ── 进化阈值配置 ──
HIGH_HIT_THRESHOLD = 5        # 命中 ≥5 次 → 高价值记忆，boost salience
LOW_HIT_WINDOW_DAYS = 14      # 14 天内 0 命中 → 降低 salience
FEEDBACK_BOOST_USEFUL = 0.15  # 用户标记「有用」→ +0.15 salience
FEEDBACK_PENALTY_USELESS = 0.12  # 用户标记「无用」→ -0.12 salience
EVOLUTION_INTERVAL_HOURS = 6  # 每 6 小时自动进化一次
MIN_SCORE_TO_PROMOTE = 0.65   # 搜索分数 ≥ 此值才算高质量命中


# ═══════════════════════════════════════════════
# 数据库初始化
# ═══════════════════════════════════════════════

def _get_evolve_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(EVOLVE_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# 公开别名 + 清理入口（v19.4.1）
#
# 为什么补这两个符号：
#     wal_engine 的级联删除第 5 步写的是
#         `from ducky.evolve_mem import get_evolve_conn`
#         `DELETE FROM evolve_snapshots WHERE ...`
#     但本模块从来只有私有的 `_get_evolve_conn`，而且**没有**
#     `evolve_snapshots` 这张表（真实表是 evolve_queries / evolve_feedback /
#     evolve_adjustments / evolve_meta）。
#     两个错误都被 `except Exception: logger.debug(...)` 吞掉，
#     于是「删除记忆会清理 evolve_mem.db」这件事**从引入起从未真正发生过** ——
#     删掉的记忆在检索自进化库里留下永久的反馈与调权残留（孤儿数据），
#     而 res["evolve"] 一直如实报 0，没人多看一眼。
#
#     这里补公开别名与按 memory_id 的精确清理入口，让那一步真的干活。
def get_evolve_conn() -> sqlite3.Connection:
    """公开连接入口（与 salience/facts 各仓的 get_*_conn 命名对齐）。"""
    return _get_evolve_conn()


def delete_evolve_by_memory_ids(memory_ids) -> int:
    """按 memory_id 批量清理 evolve 反馈与调权记录。返回删除行数。

    evolve 各表没有 user_id 列（它记录的是检索质量信号，不是租户数据），
    因此租户维度由调用方在传入 memory_ids 时保证 —— 调用方只会传
    自己租户下的记忆 id。
    """
    ids = [str(m) for m in (memory_ids or []) if str(m or "").strip()]
    if not ids:
        return 0
    deleted = 0
    try:
        conn = _get_evolve_conn()
        placeholders = ",".join("?" for _ in ids)
        for table in ("evolve_feedback", "evolve_adjustments"):
            try:
                deleted += conn.execute(
                    f"DELETE FROM {table} WHERE memory_id IN ({placeholders})", ids
                ).rowcount or 0
            except Exception as exc:
                logger.debug("evolve %s 清理跳过: %s", table, exc)
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.warning("delete_evolve_by_memory_ids 降级: %s", exc)
    return deleted


def ensure_evolve_schema() -> None:
    """建表（幂等）。"""
    conn = _get_evolve_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS evolve_queries (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            query     TEXT    NOT NULL,
            hit_count INTEGER NOT NULL DEFAULT 0,
            avg_score REAL    NOT NULL DEFAULT 0.0,
            latency_ms INTEGER NOT NULL DEFAULT 0,
            gate_passed INTEGER NOT NULL DEFAULT 1,
            ts        REAL    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_eq_ts ON evolve_queries(ts);

        CREATE TABLE IF NOT EXISTS evolve_feedback (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id TEXT    NOT NULL,
            query     TEXT    NOT NULL DEFAULT '',
            signal    TEXT    NOT NULL,  -- 'useful' | 'useless' | 'correction'
            correction_text TEXT DEFAULT NULL,
            ts        REAL    NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ef_mid ON evolve_feedback(memory_id);

        CREATE TABLE IF NOT EXISTS evolve_adjustments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id   TEXT NOT NULL,
            action      TEXT NOT NULL,  -- 'salience_boost' | 'salience_decay' | 'feedback_boost' | 'feedback_penalty'
            delta       REAL NOT NULL,
            reason      TEXT NOT NULL,
            ts          REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ea_ts ON evolve_adjustments(ts);

        CREATE TABLE IF NOT EXISTS evolve_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    logger.info("✅ EvolveMem schema 就绪")


# ═══════════════════════════════════════════════
# 搜索质量日志
# ═══════════════════════════════════════════════

def log_search_quality(
    query: str,
    results: list[dict],
    latency_ms: int = 0,
    gate_passed: bool = True,
) -> None:
    """记录一次搜索的质量信号（异步安全，失败静默）。"""
    try:
        ensure_evolve_schema()
        hit_count = len(results)
        avg_score = 0.0
        if results:
            scores = [r.get("score", 0.0) for r in results if isinstance(r.get("score"), (int, float))]
            avg_score = sum(scores) / len(scores) if scores else 0.0

        conn = _get_evolve_conn()
        conn.execute(
            "INSERT INTO evolve_queries(query, hit_count, avg_score, latency_ms, gate_passed, ts) VALUES(?,?,?,?,?,?)",
            (query[:500], hit_count, avg_score, latency_ms, int(gate_passed), time.time()),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug(f"log_search_quality 失败（静默）: {e}")


# ═══════════════════════════════════════════════
# 用户反馈
# ═══════════════════════════════════════════════

FeedbackSignal = Literal["useful", "useless", "correction"]


def record_feedback(
    memory_id: str,
    signal: FeedbackSignal,
    query: str = "",
    correction_text: str | None = None,
) -> dict:
    """
    记录用户对某条记忆的反馈，并立即更新 salience。

    Args:
        memory_id:        记忆 UUID
        signal:           'useful' | 'useless' | 'correction'
        query:            触发这条记忆的搜索词（可选，用于关联分析）
        correction_text:  若 signal='correction'，填入修正后的正确内容

    Returns:
        {"ok": True, "salience_delta": ±x, "new_salience": y}
    """
    ensure_evolve_schema()
    conn = _get_evolve_conn()
    conn.execute(
        "INSERT INTO evolve_feedback(memory_id, query, signal, correction_text, ts) VALUES(?,?,?,?,?)",
        (memory_id, query[:500], signal, correction_text, time.time()),
    )
    conn.commit()
    conn.close()

    # 立即调整 salience
    delta = 0.0
    if signal == "useful":
        delta = FEEDBACK_BOOST_USEFUL
    elif signal == "useless":
        delta = -FEEDBACK_PENALTY_USELESS
    # correction 也给轻微 boost（说明记忆被注意了，价值尚存）
    elif signal == "correction":
        delta = 0.05

    new_sal = _apply_salience_delta(memory_id, delta, f"feedback:{signal}")
    return {"ok": True, "salience_delta": delta, "new_salience": new_sal}


def _apply_salience_delta(memory_id: str, delta: float, reason: str) -> float:
    """
    对指定记忆的 salience 施加增减量，写入日志。
    salience 钳制在 [0.05, 1.0]。
    """
    sal_conn = get_salience_conn()
    row = sal_conn.execute(
        "SELECT salience FROM salience WHERE memory_id=?", (memory_id,)
    ).fetchone()

    if row is None:
        # 记忆尚未在 salience 表，插入默认值再调整
        now = time.time()
        sal_conn.execute(
            "INSERT OR IGNORE INTO salience(memory_id, salience, last_access, access_count, created_at) VALUES(?,?,?,?,?)",
            (memory_id, 0.5, now, 0, now),
        )
        old_sal = 0.5
    else:
        old_sal = row["salience"] if hasattr(row, "__getitem__") else row[0]

    new_sal = max(0.05, min(1.0, old_sal + delta))
    sal_conn.execute(
        "UPDATE salience SET salience=?, last_access=? WHERE memory_id=?",
        (new_sal, time.time(), memory_id),
    )
    sal_conn.commit()
    sal_conn.close()

    # 写进化动作日志
    try:
        ev_conn = _get_evolve_conn()
        ev_conn.execute(
            "INSERT INTO evolve_adjustments(memory_id, action, delta, reason, ts) VALUES(?,?,?,?,?)",
            (memory_id, "salience_boost" if delta >= 0 else "salience_decay", delta, reason, time.time()),
        )
        ev_conn.commit()
        ev_conn.close()
    except Exception as e:
        logger.debug(f"evolve salience adjustment skip: {e}")

    logger.debug(f"[evolve] {memory_id[:8]}… salience {old_sal:.3f} → {new_sal:.3f} ({reason})")
    return new_sal


# ═══════════════════════════════════════════════
# 进化循环
# ═══════════════════════════════════════════════

def run_evolution_cycle() -> dict:
    """
    执行一次 EvolveMem 进化循环。

    策略：
    1. 统计最近 14 天内每条被召回记忆的命中次数
    2. 高频命中（≥ HIGH_HIT_THRESHOLD 且 avg_score ≥ MIN_SCORE_TO_PROMOTE）→ +boost
    3. 低命中（14 天内从未被召回）→ -decay（叠加在正常时间衰减上）
    4. 从 evolve_feedback 中汇总待处理的反馈（上次循环后的新增）

    Returns:
        {"boosted": int, "decayed": int, "feedback_processed": int, "ts": float}
    """
    ensure_evolve_schema()
    now = time.time()
    window_start = now - LOW_HIT_WINDOW_DAYS * 86400

    ev_conn = _get_evolve_conn()
    sal_conn = get_salience_conn()

    # ── Step 1: 拿近期搜索命中统计 ──
    # 按 memory_id 聚合（通过 evolve_queries 间接推断：高质量搜索命中哪些 salience 记录）
    # 简化：直接用 salience 表的 access_count + last_access 做判断
    all_memories = sal_conn.execute(
        "SELECT memory_id, salience, access_count, last_access FROM salience"
    ).fetchall()
    sal_conn.close()

    boosted = 0
    decayed = 0

    for row in all_memories:
        mid = row["memory_id"] if hasattr(row, "keys") else row[0]
        sal = row["salience"] if hasattr(row, "keys") else row[1]
        acc = row["access_count"] if hasattr(row, "keys") else row[2]
        last = row["last_access"] if hasattr(row, "keys") else row[3]

        # 高频命中 boost
        if acc >= HIGH_HIT_THRESHOLD and sal < 0.9:
            boost = min(0.05, (acc - HIGH_HIT_THRESHOLD) * 0.01)
            _apply_salience_delta(mid, boost, f"evolve:high_hit(acc={acc})")
            boosted += 1

        # 超过窗口未访问 → 轻衰减
        elif last < window_start and sal > 0.25:
            decay_delta = -0.03
            _apply_salience_delta(mid, decay_delta, f"evolve:idle({LOW_HIT_WINDOW_DAYS}d)")
            decayed += 1

    # ── Step 2: 处理待汇总的搜索质量（低质量查询信号）──
    last_run_ts = float(ev_conn.execute(
        "SELECT value FROM evolve_meta WHERE key='last_cycle_ts'"
    ).fetchone()["value"] if ev_conn.execute(
        "SELECT value FROM evolve_meta WHERE key='last_cycle_ts'"
    ).fetchone() else 0.0)

    recent_queries = ev_conn.execute(
        "SELECT hit_count, avg_score FROM evolve_queries WHERE ts > ? AND gate_passed=1",
        (last_run_ts,),
    ).fetchall()
    zero_hit_queries = sum(1 for q in recent_queries if (q["hit_count"] if hasattr(q, "keys") else q[0]) == 0)
    total_recent = len(recent_queries)

    # ── Step 3: 更新循环时间戳 ──
    ev_conn.execute(
        "INSERT OR REPLACE INTO evolve_meta(key, value) VALUES('last_cycle_ts', ?)",
        (str(now),),
    )
    ev_conn.commit()
    ev_conn.close()

    result = {
        "boosted": boosted,
        "decayed": decayed,
        "zero_hit_queries": zero_hit_queries,
        "total_recent_queries": total_recent,
        "ts": now,
        "status": "ok",
    }
    logger.info(f"[evolve] 进化循环完成: boost={boosted} decay={decayed} zero_hit={zero_hit_queries}/{total_recent}")
    return result


# ═══════════════════════════════════════════════
# 进化报告
# ═══════════════════════════════════════════════

def get_evolve_report() -> dict:
    """返回 EvolveMem 的整体进化状态报告。"""
    ensure_evolve_schema()
    ev_conn = _get_evolve_conn()

    # 最近 7 天搜索统计
    week_ago = time.time() - 7 * 86400
    q_stats = ev_conn.execute("""
        SELECT COUNT(*) AS total,
               AVG(hit_count) AS avg_hits,
               AVG(avg_score) AS avg_score,
               SUM(CASE WHEN hit_count=0 THEN 1 ELSE 0 END) AS zero_hits,
               AVG(latency_ms) AS avg_ms
        FROM evolve_queries WHERE ts > ?
    """, (week_ago,)).fetchone()

    # 反馈统计
    fb_stats = ev_conn.execute("""
        SELECT signal, COUNT(*) AS cnt FROM evolve_feedback GROUP BY signal
    """).fetchall()
    feedback_dist = {row["signal"]: row["cnt"] for row in fb_stats}

    # 调整动作统计
    adj_stats = ev_conn.execute("""
        SELECT action, COUNT(*) AS cnt, AVG(delta) AS avg_delta
        FROM evolve_adjustments WHERE ts > ?
        GROUP BY action
    """, (week_ago,)).fetchall()
    adjustments = [
        {"action": r["action"], "count": r["cnt"], "avg_delta": round(r["avg_delta"], 4)}
        for r in adj_stats
    ]

    # 上次进化时间
    last_ts_row = ev_conn.execute(
        "SELECT value FROM evolve_meta WHERE key='last_cycle_ts'"
    ).fetchone()
    last_cycle_ts = float(last_ts_row["value"]) if last_ts_row else None

    ev_conn.close()

    def _row_val(row, key, idx):
        if row is None:
            return None
        return row[key] if hasattr(row, "keys") else row[idx]

    return {
        "status": "ok",
        "last_7d_search": {
            "total_queries": _row_val(q_stats, "total", 0) or 0,
            "avg_hits": round(_row_val(q_stats, "avg_hits", 1) or 0, 2),
            "avg_score": round(_row_val(q_stats, "avg_score", 2) or 0, 3),
            "zero_hit_queries": _row_val(q_stats, "zero_hits", 3) or 0,
            "avg_latency_ms": round(_row_val(q_stats, "avg_ms", 4) or 0, 1),
        },
        "feedback_distribution": feedback_dist,
        "last_7d_adjustments": adjustments,
        "last_cycle_ts": last_cycle_ts,
        "last_cycle_human": (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_cycle_ts))
            if last_cycle_ts else "从未执行"
        ),
    }


# ═══════════════════════════════════════════════
# 后台进化循环
# ═══════════════════════════════════════════════

def evolve_background_loop() -> None:
    """后台线程：每 EVOLUTION_INTERVAL_HOURS 小时自动执行一次进化循环。"""
    import threading
    logger.info(f"⚡ EvolveMem 后台进化线程启动（间隔 {EVOLUTION_INTERVAL_HOURS}h）")
    while True:
        try:
            report = run_evolution_cycle()
            logger.info(
                f"[evolve-bg] boost={report['boosted']} decay={report['decayed']} "
                f"zero_hit={report['zero_hit_queries']}"
            )
        except Exception as e:
            logger.error(f"[evolve-bg] 进化循环异常: {e}", exc_info=True)
        time.sleep(EVOLUTION_INTERVAL_HOURS * 3600)
