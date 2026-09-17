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
import sqlite3
import time
import uuid
from ducky.shutdown import sleep as _shutdown_sleep
from typing import Literal

from ducky.utils import DATA_DIR, get_salience_conn

import os

EVOLVE_DB_PATH = os.path.join(DATA_DIR, "evolve_mem.db")

logger = logging.getLogger("aiduMEM.evolve")

# ── 进化阈值配置 ──
HIGH_HIT_THRESHOLD = 5        # 命中 ≥5 次 → 高价值记忆，boost salience
LOW_HIT_WINDOW_DAYS = 14      # 14 天内 0 命中 → 降低 salience

# v20.4.0-alpha（P1-8）：进化循环候选 SQL —— 与 run_evolution_cycle 的
# Python 写分支同条件：boost 支（acc≥阈值 且 sal<0.9）∪ decay 支
# （超窗 且 sal>0.25）。两析取支分别走 idx_access_count / idx_last_access。
_EVOLVE_CANDIDATE_SQL = (
    "SELECT memory_id, salience, access_count, last_access FROM salience "
    "WHERE (access_count >= ? AND salience < 0.9) "
    "OR (last_access < ? AND salience > 0.25)"
)
FEEDBACK_BOOST_USEFUL = 0.15  # 用户标记「有用」→ +0.15 salience
FEEDBACK_PENALTY_USELESS = 0.12  # 用户标记「无用」→ -0.12 salience
EVOLUTION_INTERVAL_HOURS = 6  # 每 6 小时自动进化一次

# ── v21.2 M1：轨迹级奖励信用分配（借鉴 Memmy 的 reward.* 参数语义，独立实现）──
# 一次连续任务 = 一个 episode；任务级奖励按轨迹位置回传给其中每一步记忆，
# 而不是只调「被点名的那一条」的 salience。
#
# ⚠️ 参数纪律（施工方加严）：Memmy 开源仅一周，其默认值是否经充分调优**无从验证**。
# 因此这里全部 env 可覆盖，且 credit 维度默认权重 0 —— 开权重必须先有本仓自己的
# /evolve/report 观察数据支撑，不得因「上游就这么写」而开。
EPISODE_GAMMA = 0.9           # 轨迹位置衰减：越靠后的步骤离结果越近，权重越高
EPISODE_LAMBDA = 0.5          # 均匀权重 与 gamma 位置衰减 的混合系数
EPISODE_DELTA = 0.1           # 「从跑偏恢复到正轨」那一步的额外奖励
EPISODE_HALF_LIFE_DAYS = 30   # credit 的时间半衰期
EPISODE_MERGE_GAP_SEC = 7200  # follow-up ≤2h 并入同一 episode
EPISODE_IDLE_CLOSE_SEC = 7200 # 空闲 2h 判定 episode 关闭
EPISODE_MIN_TRACE_VALUE = 0.005  # 低于此值的步骤不参与聚合
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
    """建表（幂等）。

    v21.2.0 记一笔反面经验：本版把检索埋点接到 /search 主路径后，我一度在这里
    加了「建过就跳过」的进程内缓存来省热路径开销。先用全局布尔——库路径一变
    （测试换 tmp 库、部署改 DATA_DIR、从备份恢复换文件）就永远不再建表；
    改成按路径记——同一路径上把库文件删了重建，照样失效。两次都是当场炸出
    `no such table`。结论：**缓存的失效条件比这里省下的那点开销复杂得多**，
    而 CREATE TABLE IF NOT EXISTS 对已存在的表本就是微秒级，相对 /search 的
    几十到几百毫秒可以忽略。所以不缓存，每次老实建。
    """
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

        -- v21.2 M1：轨迹级奖励信用分配（episode = 一次连续任务）
        CREATE TABLE IF NOT EXISTS evolve_episodes (
            episode_id TEXT PRIMARY KEY,
            user_id    TEXT NOT NULL DEFAULT 'default',
            bank_id    TEXT NOT NULL DEFAULT 'default',
            session_id TEXT NOT NULL DEFAULT '',
            started_at REAL NOT NULL,
            last_step_at REAL NOT NULL DEFAULT 0,
            closed_at  REAL,
            status     TEXT NOT NULL DEFAULT 'open',  -- open | closed
            reward     REAL NOT NULL DEFAULT 0.0,
            step_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_ep_session ON evolve_episodes(session_id, status);

        CREATE TABLE IF NOT EXISTS evolve_episode_steps (
            episode_id  TEXT NOT NULL,
            memory_ref  TEXT NOT NULL,
            step_idx    INTEGER NOT NULL,
            step_reward REAL NOT NULL DEFAULT 0.0,
            credit_weight REAL NOT NULL DEFAULT 0.0,
            created_at  REAL NOT NULL,
            PRIMARY KEY (episode_id, memory_ref)
        );
        CREATE INDEX IF NOT EXISTS idx_eps_ep ON evolve_episode_steps(episode_id, step_idx);
        CREATE INDEX IF NOT EXISTS idx_eps_ref ON evolve_episode_steps(memory_ref);
    """)
    # v21.2.0 迁移：存量库的 evolve_queries 没有 origin_session_id。
    # CREATE TABLE IF NOT EXISTS 对已存在的表是空操作，所以补列必须单独做。
    try:
        _eq_cols = {r[1] for r in conn.execute("PRAGMA table_info(evolve_queries)")}
        if "origin_session_id" not in _eq_cols:
            with conn:                     # 失败自动回滚，不留半迁移状态
                conn.execute("ALTER TABLE evolve_queries "
                             "ADD COLUMN origin_session_id TEXT NOT NULL DEFAULT ''")
            logger.info("✅ evolve_queries 补列 origin_session_id（区分对话检索与定时器自检）")
    except sqlite3.Error as _mig_exc:      # 迁移失败不许拖垮服务；下次启动再试
        # with conn 已经会回滚，这一句是显式化意图：补列失败绝不许留半迁移状态。
        # （本仓的无回滚棘轮按字面找 rollback，认不出 with conn —— 写出来也让
        #  下一个读代码的人一眼看到事务边界在哪。）
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        logger.warning("evolve_queries 补列失败（下次启动重试）: %s", _mig_exc)
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
    origin_session_id: str = "",
) -> None:
    """记录一次搜索的质量信号（异步安全，失败静默）。

    v21.2.0 加 origin_session_id：区分「有人在对话」与「定时器在自检」。
    此前写入活性探针拿这张表当「有人在用」的证据，而实测生产近 24h 的记录
    **每一条都是 e2e_smoke 的定时巡检**（每小时整 1 次），一条真实对话检索
    都没有 —— 探针于是在「一天没聊天」时必然误报写线断了。
    读不到 session 一律空串（老宿主不传，如实留空，不猜）。
    """
    try:
        ensure_evolve_schema()
        hit_count = len(results)
        avg_score = 0.0
        if results:
            scores = [r.get("score", 0.0) for r in results if isinstance(r.get("score"), (int, float))]
            avg_score = sum(scores) / len(scores) if scores else 0.0

        conn = _get_evolve_conn()
        conn.execute(
            "INSERT INTO evolve_queries(query, hit_count, avg_score, latency_ms, "
            "gate_passed, ts, origin_session_id) VALUES(?,?,?,?,?,?,?)",
            (query[:500], hit_count, avg_score, latency_ms, int(gate_passed),
             time.time(), str(origin_session_id or "")[:256]),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug(f"log_search_quality 失败（静默）: {e}")


# ═══════════════════════════════════════════════
# 用户反馈
# ═══════════════════════════════════════════════

FeedbackSignal = Literal["useful", "useless", "correction"]


def _guard_feedback_scope(memory_id: str, user_id: str, bank_id: str) -> tuple[str, str] | None:
    """v20 P0-2：反馈的 opt-in 域守卫（口径同 hot/legacy_helpers._fact_feedback_impl）。

    不传作用域 = v19 管理员语义，零改动放行（返回 None）。
    传了作用域：先规范化（非法直接抛 BankScopeError，路由层转 error dict），
    再对照 salience 行上的归属戳做 canon 比较（uid 折叠 DEFAULT_USER_ID↔'default'，
    老库无作用域列/空串归 default 域）。不符即拒——反馈会动 salience，
    是跨库改写他库权重的通道，必须堵。行不存在时放行并返回规范化后的
    作用域，由调用方预插带戳行（UUID 空间下探测无意义，真正的威胁
    ——挪动他库 salience——已被不符即拒挡住，这是文档化的残余取舍）。
    """
    if not (user_id or "").strip() and not (bank_id or "").strip():
        return None
    from ducky.bank_contract import BankScopeError, normalize_bank_id, normalize_user_id
    from ducky.utils import DEFAULT_USER_ID
    want_uid = normalize_user_id(user_id or DEFAULT_USER_ID)
    want_bid = normalize_bank_id(bank_id or "")
    canon_want_uid = "default" if want_uid == DEFAULT_USER_ID else want_uid
    try:
        sal_conn = get_salience_conn()
        row = sal_conn.execute(
            "SELECT * FROM salience WHERE memory_id=?", (memory_id,)
        ).fetchone()
        cols = {r[1] for r in sal_conn.execute("PRAGMA table_info(salience)").fetchall()}
        sal_conn.close()
    except Exception as exc:
        # 老库连 salience 表都没有：整库视为 default 域（口径同 legacy_helpers）
        logger.debug(f"feedback 域守卫降级 default 域: {exc}")
        if canon_want_uid != "default" or want_bid != "default":
            raise BankScopeError("记忆不在该库或不存在") from exc
        return (want_uid, want_bid)
    if "user_id" not in cols or "bank_id" not in cols:
        # v19 表无作用域列：整表归 default 域，具名域声称一律拒
        # （行缺失也一样——没有列就盖不了戳，放行只会错盖成 default）
        if canon_want_uid != "default" or want_bid != "default":
            raise BankScopeError("记忆不在该库或不存在")
        return (want_uid, want_bid)
    if row is None:
        return (want_uid, want_bid)
    keys = row.keys() if hasattr(row, "keys") else []
    row_uid = str((row["user_id"] if "user_id" in keys else "") or "default")
    row_bid = str((row["bank_id"] if "bank_id" in keys else "") or "default")
    canon_row_uid = "default" if row_uid == DEFAULT_USER_ID else row_uid
    if canon_row_uid != canon_want_uid or row_bid != want_bid:
        # 域不符与不存在同文案，不泄露他库记忆的存在性
        raise BankScopeError("记忆不在该库或不存在")
    return (want_uid, want_bid)


# ═══════════════════════════════════════════════
# v21.2 M1：Episode 轨迹级奖励信用分配
# ═══════════════════════════════════════════════

def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    """读一个有界浮点配置。非法值 fail-closed 回默认（与检索侧同纪律）。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    if not (lo <= val <= hi):
        return default
    return val


def episode_params() -> dict:
    """本次读取生效的 episode 参数（全部 env 可覆盖、全部有界）。"""
    return {
        "gamma": _env_float("AIDUMEI_EPISODE_GAMMA", EPISODE_GAMMA, 0.0, 1.0),
        "lambda": _env_float("AIDUMEI_EPISODE_LAMBDA", EPISODE_LAMBDA, 0.0, 1.0),
        "delta": _env_float("AIDUMEI_EPISODE_DELTA", EPISODE_DELTA, 0.0, 1.0),
        "half_life_days": _env_float("AIDUMEI_EPISODE_HALF_LIFE_DAYS",
                                     EPISODE_HALF_LIFE_DAYS, 1.0, 3650.0),
        "min_trace_value": _env_float("AIDUMEI_EPISODE_MIN_TRACE_VALUE",
                                      EPISODE_MIN_TRACE_VALUE, 0.0, 1.0),
    }


def credit_weights(n: int, *, gamma: float | None = None,
                   lam: float | None = None) -> list[float]:
    """轨迹位置信用权重 w_i = λ·(1/n) + (1−λ)·归一化(γ^(n−i))。

    借鉴 Memmy 的 reward.gamma / reward.lambda 语义（思路级借鉴，独立实现）。
    直觉：越靠近任务结果的那一步，对结果的贡献越可信；但也不能把功劳全给
    最后一步 —— λ 那一半是「雨露均沾」的保底。返回的权重和恒为 1.0。

    纯函数：不读库、不看钟、不读环境（参数由调用方显式给）。
    """
    if n <= 0:
        return []
    p = episode_params()
    g = p["gamma"] if gamma is None else gamma
    lm = p["lambda"] if lam is None else lam
    if n == 1:
        return [1.0]
    # γ^(n−i)：i 从 1 计，最后一步指数为 0（权重最大）
    raw = [g ** (n - i) for i in range(1, n + 1)]
    total = sum(raw)
    pos = [r / total for r in raw] if total > 0 else [1.0 / n] * n
    uni = 1.0 / n
    return [round(lm * uni + (1.0 - lm) * pv, 6) for pv in pos]


def _now() -> float:
    return time.time()


def open_or_extend_episode(session_id: str, *, user_id: str = "default",
                           bank_id: str = "default") -> str:
    """取本 session 当前开着的 episode；没有或已超合并窗则新开一个。

    follow-up ≤ EPISODE_MERGE_GAP_SEC 并入同一 episode（Memmy 的
    mergeMaxGapMs 语义）。session_id 为空返回 "" —— 无会话的写入
    （cron / 后台作业）**不产生 episode**，不污染轨迹统计。
    """
    if not session_id:
        return ""
    ensure_evolve_schema()
    now = _now()
    conn = _get_evolve_conn()
    try:
        row = conn.execute(
            "SELECT episode_id, last_step_at FROM evolve_episodes "
            "WHERE session_id=? AND status='open' ORDER BY started_at DESC LIMIT 1",
            (session_id,)).fetchone()
        if row is not None:
            last = row["last_step_at"] if hasattr(row, "keys") else row[1]
            if (now - float(last or 0)) <= EPISODE_MERGE_GAP_SEC:
                return row["episode_id"] if hasattr(row, "keys") else row[0]
            # 超窗：先关旧的，再开新的
            conn.execute(
                "UPDATE evolve_episodes SET status='closed', closed_at=? "
                "WHERE episode_id=?",
                (now, row["episode_id"] if hasattr(row, "keys") else row[0]))
        episode_id = f"ep_{uuid.uuid4().hex[:16]}"
        conn.execute(
            "INSERT INTO evolve_episodes(episode_id, user_id, bank_id, session_id,"
            " started_at, last_step_at, status) VALUES(?,?,?,?,?,?, 'open')",
            (episode_id, user_id, bank_id, session_id, now, now))
        conn.commit()
        return episode_id
    finally:
        conn.close()


def record_episode_step(memory_refs=None, *, session_id: str = "",
                        user_id: str = "default", bank_id: str = "default") -> int:
    """登记一步轨迹：本次写入产生的记忆归属到本 session 当前 episode。

    无 session_id 一律返回 0（不记）。失败只打 debug —— 轨迹统计不是
    写入主链路的一部分，绝不许它把 /add 打炸。
    """
    if not session_id:
        return 0
    refs = [str(r) for r in (memory_refs or []) if r]
    if not refs:
        return 0
    try:
        episode_id = open_or_extend_episode(session_id, user_id=user_id, bank_id=bank_id)
        if not episode_id:
            return 0
        now = _now()
        conn = _get_evolve_conn()
        try:
            cur = conn.execute(
                "SELECT COALESCE(MAX(step_idx), 0) FROM evolve_episode_steps "
                "WHERE episode_id=?", (episode_id,)).fetchone()
            next_idx = int((cur[0] if cur else 0) or 0)
            n = 0
            for ref in refs:
                next_idx += 1
                conn.execute(
                    "INSERT INTO evolve_episode_steps(episode_id, memory_ref, step_idx,"
                    " created_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(episode_id, memory_ref) DO NOTHING",
                    (episode_id, ref, next_idx, now))
                n += 1
            conn.execute(
                "UPDATE evolve_episodes SET last_step_at=?, step_count="
                "(SELECT COUNT(*) FROM evolve_episode_steps WHERE episode_id=?) "
                "WHERE episode_id=?", (now, episode_id, episode_id))
            conn.commit()
            return n
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("record_episode_step 跳过: %s", exc)
        return 0


def settle_episode(episode_id: str, reward: float) -> dict:
    """结算一个 episode：任务奖励按轨迹位置回传给每一步，并聚合进 salience。

    reward > 0 = 任务成功（整条轨迹受益）；< 0 = 失败（整条轨迹受罚，
    但越靠后的步骤担责越重 —— 这正是「哪一步开始跑偏」的可学习信号）。

    红线：只写 evolve 侧表与 salience，**绝不碰 facts 正文**。
    """
    ensure_evolve_schema()
    reward = max(-1.0, min(1.0, float(reward or 0.0)))
    conn = _get_evolve_conn()
    try:
        rows = conn.execute(
            "SELECT memory_ref, step_idx FROM evolve_episode_steps "
            "WHERE episode_id=? ORDER BY step_idx", (episode_id,)).fetchall()
        if not rows:
            return {"ok": False, "reason": "no_steps", "episode_id": episode_id}
        n = len(rows)
        weights = credit_weights(n)
        p = episode_params()
        applied = 0
        for (row, w) in zip(rows, weights):
            ref = row["memory_ref"] if hasattr(row, "keys") else row[0]
            step_reward = round(reward * w, 6)
            conn.execute(
                "UPDATE evolve_episode_steps SET step_reward=?, credit_weight=? "
                "WHERE episode_id=? AND memory_ref=?",
                (step_reward, round(w, 6), episode_id, ref))
            if abs(step_reward) >= p["min_trace_value"]:
                _apply_salience_delta(ref, step_reward,
                                      f"episode:{episode_id[:12]}")
                applied += 1
        conn.execute(
            "UPDATE evolve_episodes SET status='closed', closed_at=?, reward=? "
            "WHERE episode_id=?", (_now(), reward, episode_id))
        conn.commit()
        return {"ok": True, "episode_id": episode_id, "steps": n,
                "reward": reward, "salience_applied": applied}
    finally:
        conn.close()


def record_episode_feedback(session_id: str, reward: float) -> dict:
    """对本 session 当前 episode 给一次任务级反馈并立即结算。

    这是 M1 的用户面入口：与 record_feedback（单条记忆 ±salience）并存，
    各管各的——单条反馈调「这一条」，episode 反馈调「这一整串」。
    """
    if not session_id:
        return {"ok": False, "reason": "no_session"}
    ensure_evolve_schema()
    conn = _get_evolve_conn()
    try:
        row = conn.execute(
            "SELECT episode_id FROM evolve_episodes WHERE session_id=? "
            "AND status='open' ORDER BY started_at DESC LIMIT 1",
            (session_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"ok": False, "reason": "no_open_episode", "session_id": session_id}
    return settle_episode(row["episode_id"] if hasattr(row, "keys") else row[0], reward)


def get_credit_map(memory_refs, *, user_id: str = "", bank_id: str = "") -> dict:
    """批量取 credit_weight（供 scoring 第六维用）。

    与 _load_epi_map 同一纪律：单次批量 SQL、零 N+1、表不在如实空表。
    带 30 天半衰期衰减 —— 老轨迹的功劳会自然淡出。
    """
    out: dict = {}
    refs = [str(r) for r in (memory_refs or []) if r]
    if not refs:
        return out
    try:
        conn = _get_evolve_conn()
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "evolve_episode_steps" not in tables:
                return out
            half_life = episode_params()["half_life_days"] * 86400.0
            now = _now()
            placeholders = ",".join("?" for _ in refs)
            # v21.2.0 审计整改轮：补域收窄。与同族的 _load_epi_map / _load_echo_refs
            # 口径对齐 —— 作用域片段一律经 scope_clause() 正规入口，不手拼
            # （手拼点是「作用域棘轮」上的新缺口，也是 f-string SQL 的来源）。
            # evolve 是独立库、ref 为 UUID，碰撞概率低，但低不等于零。
            # 未传域时保持全库语义（管理面查询/存量调用方零破坏）。
            _scope_sql, _scope_args = "", []
            if user_id or bank_id:
                from ducky.scope_sql import scope_clause
                from ducky.bank_contract import make_scope
                _scope_sql, _scope_args = scope_clause(
                    make_scope(user_id or "default", bank_id or "default"),
                    alias="e", flavor="canonical")
            _sql = (
                "SELECT s.memory_ref, s.step_reward, s.created_at "
                "FROM evolve_episode_steps s "
                "JOIN evolve_episodes e ON e.episode_id = s.episode_id "
                "WHERE s.memory_ref IN (" + placeholders + ")" + _scope_sql
            )
            for row in conn.execute(_sql, (*refs, *_scope_args)):
                ref = row["memory_ref"] if hasattr(row, "keys") else row[0]
                sr = float((row["step_reward"] if hasattr(row, "keys") else row[1]) or 0.0)
                created = float((row["created_at"] if hasattr(row, "keys") else row[2]) or now)
                age = max(0.0, now - created)
                decay = 0.5 ** (age / half_life) if half_life > 0 else 1.0
                out[ref] = round(out.get(ref, 0.0) + sr * decay, 6)
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("get_credit_map 跳过: %s", exc)
    return out


def record_feedback(
    memory_id: str,
    signal: FeedbackSignal,
    query: str = "",
    correction_text: str | None = None,
    user_id: str = "",
    bank_id: str = "",
) -> dict:
    """
    记录用户对某条记忆的反馈，并立即更新 salience。

    Args:
        memory_id:        记忆 UUID
        signal:           'useful' | 'useless' | 'correction'
        query:            触发这条记忆的搜索词（可选，用于关联分析）
        correction_text:  若 signal='correction'，填入修正后的正确内容
        user_id/bank_id:  v20 opt-in 作用域。不传 = v19 管理员语义零改动；
                          传了则校验记忆归属，越库反馈直接拒（BankScopeError）

    Returns:
        {"ok": True, "salience_delta": ±x, "new_salience": y}
    """
    scope = _guard_feedback_scope(memory_id, user_id, bank_id)
    ensure_evolve_schema()
    if scope is not None:
        # 行不存在时先预插带戳行，否则 _apply_salience_delta 的无戳
        # INSERT 会让这条记忆被列默认值错盖成 default 域
        try:
            sal_conn = get_salience_conn()
            now = time.time()
            sal_conn.execute(
                "INSERT OR IGNORE INTO salience"
                "(memory_id, salience, last_access, access_count, created_at, user_id, bank_id)"
                " VALUES(?,?,?,?,?,?,?)",
                (memory_id, 0.5, now, 0, now, scope[0], scope[1]),
            )
            sal_conn.commit()
            sal_conn.close()
        except Exception as exc:
            logger.debug(f"feedback 预插带戳 salience 行跳过: {exc}")
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

    **作用域：全库维护作业，不按域隔离（v20.0 乙1）。**
    本函数按设计扫 salience 全表，不带 user_id / bank_id 过滤。它调的是
    「这条记忆最近有没有被召回」这一事实，与谁召回、落在哪个 bank 无关；
    它只改权重，不读记忆正文，也不向调用方返回任何一条记忆的内容 ——
    /evolve/cycle 拿回去的只有聚合计数。
    写下这一句，是因为不写下来，下一个人看到「一个没有 user_id 过滤的全表
    SELECT」会以为它漏了隔离而顺手「修好」。给它加上域过滤才会真出事故：
    每个域各自衰减，冷热判断的样本被切碎，全局衰减基准就失准了。

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
    #
    # v20.4.0-alpha（P1-8，三方外审共识）：候选集下推 SQL，不再全表拉进内存。
    # 逐行逻辑里只有两类行会被写 —— 高频命中待 boost、超窗未访问待 decay，
    # 其余行读完即弃；SQL 析取两个写分支的条件，Python 分支原样保留，
    # 语义逐行等价（含「两条件都中的行只 boost 不 decay」：析取含 boost 条件
    # 的行回到 Python 仍先命中 if 分支）。全库维护语义不变：仍不按域隔离
    # （见函数 docstring 乙1 条），只是候选集从「全表」收窄到「会被写的行」。
    all_memories = sal_conn.execute(
        _EVOLVE_CANDIDATE_SQL,
        (HIGH_HIT_THRESHOLD, window_start),
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
        # v21.2 M1：轨迹维度 —— 开 credit 权重前要先看这里有没有数据
        "episodes": _episode_report(),
        "last_cycle_ts": last_cycle_ts,
        "last_cycle_human": (
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_cycle_ts))
            if last_cycle_ts else "从未执行"
        ),
    }


# ═══════════════════════════════════════════════
# 后台进化循环
# ═══════════════════════════════════════════════

def get_episode_groups(memory_refs) -> dict:
    """批量取候选的 episode 归属与步序（供 M7 rollup）。

    返回 {memory_ref: (episode_id, step_idx)}。表不在如实空表。
    """
    out: dict = {}
    refs = [str(r) for r in (memory_refs or []) if r]
    if not refs:
        return out
    try:
        conn = _get_evolve_conn()
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "evolve_episode_steps" not in tables:
                return out
            placeholders = ",".join("?" for _ in refs)
            for row in conn.execute(
                    f"SELECT memory_ref, episode_id, step_idx FROM evolve_episode_steps "
                    f"WHERE memory_ref IN ({placeholders})", refs):
                out[row[0]] = (row[1], int(row[2] or 0))
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("get_episode_groups 跳过: %s", exc)
    return out


def _episode_report() -> dict:
    """v21.2 M1：episode 维度统计（供 /evolve/report）。

    表不在如实返回 available=False —— 「没这张表」与「表里没数据」
    是两回事，混成 0 会让人以为轨迹功能开着却没人用。
    """
    out = {"available": False, "open": 0, "closed": 0, "steps": 0,
           "settled_steps": 0, "credit_weight": 0.0}
    try:
        from ducky.scoring import credit_dimension_weight
        out["credit_weight"] = credit_dimension_weight()
    except Exception:
        pass
    try:
        conn = _get_evolve_conn()
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            if "evolve_episodes" not in tables:
                return out
            out["available"] = True
            for status, cnt in conn.execute(
                    "SELECT status, COUNT(*) FROM evolve_episodes GROUP BY status"):
                if status in ("open", "closed"):
                    out[status] = cnt
            row = conn.execute(
                "SELECT COUNT(*), SUM(CASE WHEN credit_weight > 0 THEN 1 ELSE 0 END) "
                "FROM evolve_episode_steps").fetchone()
            if row:
                out["steps"] = int(row[0] or 0)
                out["settled_steps"] = int(row[1] or 0)
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("episode 报表跳过: %s", exc)
    return out


def evolve_background_loop() -> None:
    """后台线程：每 EVOLUTION_INTERVAL_HOURS 小时自动执行一次进化循环。"""
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
        if not _shutdown_sleep(EVOLUTION_INTERVAL_HOURS * 3600):
            return  # 停机请求（P2-20）：收尾退出
