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
from ducky.bank_contract import is_legacy_schema_error
from ducky.utils import get_salience_conn

logger = logging.getLogger("aiduMEM.salience")

# 甲17：缺表/缺列降级只在每个进程里喊第一次（键是降级位点）。
_schema_warned: set[str] = set()


def _warn_schema_degraded(where: str, exc: BaseException) -> None:
    """记一次「显著性整体不在」，而不是逐次记账。

    这两个位点都在 ``/search`` 热路径上。表真不在的时候，每次检索喊一遍会把
    日志刷成噪音，运维反而看不见那一行 —— 埋掉信号和没有信号一样糟。所以
    喊一次说清「加成整体失效、检索仍照常返回」，后续降为 debug。
    """
    if where in _schema_warned:
        logger.debug("salience 表结构缺失，%s 继续降级：%s", where, exc)
        return
    _schema_warned.add(where)
    logger.warning(
        "salience 表结构缺失（缺表或缺列），%s 已降级 —— 显著性加成整体失效，"
        "检索照常返回但排序少了这一档；本进程内同一位点不再重复告警。"
        "建表在 ducky/salience/db.py::_ensure_db（甲17）：%s",
        where, exc,
    )

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
                     preserve_heat: bool = False,
                     user_id: str = "", bank_id: str = ""):
    """新增记忆时注册 salience（v8.3.0 支持 Lane + 内容缓存）

    保留已有热度的合并路径：self-edit 合并已有记忆时传入 preserve_heat=True，
    不会把 access_count 清零（否则合并会悄悄抹掉检索热度信号）。

    v20 P0-2：登记时盖 (user_id, bank_id) 戳，conflict.py 才能分域配对。
    不传 = 'default' 域（与存量行回填口径一致）；preserve_heat 更新路径
    只在调用方显式给出作用域时才刷新戳，否则保留行上原有归属。
    """
    from ducky.bank_contract import normalize_bank_id, normalize_user_id
    scope_user = normalize_user_id(user_id) if user_id else "default"
    scope_bank = normalize_bank_id(bank_id) if bank_id else "default"

    if preserve_heat:
        conn = get_salience_conn()
        row = conn.execute(
            "SELECT access_count, salience FROM salience WHERE memory_id = ?", (memory_id,)
        ).fetchone()
        if row:
            # 已有热度：保留访问次数与显著性，仅刷新 content_preview/last_access
            now = time.time()
            resolved_lane = lane or DEFAULT_LANE
            if user_id or bank_id:
                conn.execute(
                    "UPDATE salience SET last_access=?, content_preview=?, lane=?, "
                    "user_id=?, bank_id=? WHERE memory_id=?",
                    (now, content[:200] if content else "", resolved_lane,
                     scope_user, scope_bank, memory_id),
                )
            else:
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
        "(memory_id, salience, last_access, access_count, created_at, lane, content_preview, user_id, bank_id) "
        "VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?)",
        (memory_id, initial_salience, now, now, lane, content_preview, scope_user, scope_bank)
    )
    conn.commit()
    conn.close()
    logger.debug(f"salience 注册: {memory_id[:16]} → {initial_salience} [{lane}] {scope_user}/{scope_bank}")


def on_memory_accessed(memory_id: str):
    """记忆被访问时 boost 显著性

    ⚠️ 缺表/缺列时**静默跳过**，不许抛给调用方（甲17）。

    这是 ``/search`` 上的第二个出口，而且比批量查询那条更隐蔽：
    ``mem0_runtime.boost_salience_for_results`` 在 ``hot/search.py`` 里被调两次，
    一次完全裸调，另一次裹在 ``except ImportError`` 里 —— 而
    ``OperationalError`` 不是 ``ImportError``，照样逃出去。「访问提权」是一次
    纯副作用：提不动权，检索结果一个字都不该少。

    同一文件里的兄弟 ``on_memory_added`` 在调用点就被 ``try/except`` 兜住了，
    这条没有 —— 又一处漏网，不是两种设计。

    病因判据同上：只有确实缺表/缺列才降级，其余原样抛出。顺带收口一个旧洞：
    原来这里没有 ``try``，任何一次异常都会让连接漏掉（``conn.close()`` 走不到）。
    """
    now = time.time()
    conn = get_salience_conn()
    try:
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
    except sqlite3.Error as exc:
        if not is_legacy_schema_error(exc):
            raise
        _warn_schema_degraded("访问提权", exc)
    finally:
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

def get_batch_salience_records(memory_ids: list[str]) -> dict[str, dict]:
    """批量获取记忆显著性记录，消除 N+1 查询。

    ⚠️ 缺表/缺列时**必须降级成空 map**，不许把异常抛给调用方（甲17）。

    显著性只是排序加成（read-side enrichment）：``scoring.score_and_rank_candidates``
    拿它给候选加分，拿不到就该按「没有这一档加成」去排，而不是让整次检索失败。
    原来这里是 ``try/finally`` 而没有 ``except``，于是 ``salience`` 表一旦不在，
    ``OperationalError`` 一路穿过 ``scoring.py`` → ``engine.py``，**整个 /search
    500**；而它只是想给结果加一点分。判据很朴素：富化查询失败，降级的是排序质量，
    不该是可用性。

    这不是设计意图，是漏网 —— 同一个函数里紧挨着的下一次批量富化查询
    （``get_batch_memory_types``）本来就是降级的，两次同类查询一个有兜一个没兜。

    降级前先验明病因（与同子包 ``conflict.py`` 同一道判据）：只有确实缺表/缺列
    才算兼容问题；库被锁、磁盘写满一律原样抛出。否则一次瞬时故障会被悄悄读成
    「这批记忆都没有显著性」，排序被整体抹平，**而返回值形状与一次正常查询
    一模一样** —— 静默失败与成功无从区分。
    """
    if not memory_ids:
        return {}
    valid_ids = [str(m) for m in memory_ids if m]
    if not valid_ids:
        return {}
    conn = get_salience_conn()
    try:
        placeholders = ",".join("?" * len(valid_ids))
        rows = conn.execute(f"SELECT * FROM salience WHERE memory_id IN ({placeholders})", valid_ids).fetchall()
        return {r["memory_id"]: dict(r) for r in rows}
    except sqlite3.Error as exc:
        if not is_legacy_schema_error(exc):
            raise
        _warn_schema_degraded("批量显著性查询", exc)
        return {}
    finally:
        conn.close()

def delete_salience(memory_ids) -> int:
    """按 memory_id 批量删除显著性记录。返回删除行数。

    v19.4.1 补齐：wal_engine 的级联删除第 4 步写的是
        `DELETE FROM memory_salience WHERE memory_id=? AND user_id=?`
    但本库的真实表名是 `salience`，且**没有 user_id 列**
    （显著性是记忆级信号，不按租户分区）。两个错误都被
    `except Exception: logger.debug(...)` 吞掉，res["salience"] 恒为 0 ——
    「删除记忆会清理 salience.db」从引入起从未真正发生过。

    实测后果：生产 salience 表 1099 条记录里有 252 条是向量库中
    早已不存在的幽灵 id。这些幽灵会被 decay_all 当作正常记忆持续衰减、
    最终进入 evicted 列表，consolidator 再逐个调 /delete 去删
    「早就不存在的东西」——日志报「删除成功 25/25」，实际全是空转。
    这解释了为什么删除计数漂亮而向量库数量分毫未变。

    租户维度由调用方保证：只传本租户下的 memory_id。
    """
    ids = [str(m) for m in (memory_ids or []) if str(m or "").strip()]
    if not ids:
        return 0
    try:
        conn = get_salience_conn()
        placeholders = ",".join("?" for _ in ids)
        n = conn.execute(
            f"DELETE FROM salience WHERE memory_id IN ({placeholders})", ids
        ).rowcount or 0
        conn.commit()
        return n
    except Exception as exc:
        logger.warning("delete_salience 降级: %s", exc)
        return 0


def prune_orphan_salience(known_ids) -> int:
    """清理幽灵记录：salience 里存在、但 known_ids 中不存在的条目。

    known_ids 应为「当前真实存在的记忆 id 全集」（向量库 ∪ FTS）。
    传空集合时**不做任何删除** —— 防止调用方拿不到全集时反而清空全表。
    """
    known = {str(i) for i in (known_ids or []) if str(i or "").strip()}
    if not known:
        logger.warning("prune_orphan_salience: known_ids 为空，跳过（防误清全表）")
        return 0
    try:
        conn = get_salience_conn()
        rows = [r[0] for r in conn.execute("SELECT memory_id FROM salience").fetchall()]
        orphans = [r for r in rows if str(r) not in known]
        if not orphans:
            return 0
        placeholders = ",".join("?" for _ in orphans)
        n = conn.execute(
            f"DELETE FROM salience WHERE memory_id IN ({placeholders})", orphans
        ).rowcount or 0
        conn.commit()
        logger.info("🧹 清理 salience 幽灵记录 %d 条", n)
        return n
    except Exception as exc:
        logger.warning("prune_orphan_salience 降级: %s", exc)
        return 0
