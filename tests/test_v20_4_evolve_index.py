"""v20.4.0-alpha · P1-8：evolve_mem 进化循环去全表扫描。

背景（Sonnet / Qwen / Grok 三方外审共识 + 项目自披露）：
`run_evolution_cycle()` 此前 `SELECT ... FROM salience` 无 WHERE 无 LIMIT，
把整张权重表拉进内存逐行过筛 —— 记忆量增长后单轮成本随全库线性膨胀。
但逐行逻辑里**只有两类行会被写**：高频命中待 boost、超窗未访问待 decay，
其余行读完即弃 —— 全表扫是浪费，不是必需。

修法：候选集下推 SQL（两个写分支的条件析取），Python 分支逻辑原样保留，
语义逐行等价（含「两条件都中的行只 boost 不 decay」的优先级）；
salience 表补 access_count 索引，让两个析取支都走索引。

本文件钉三件事：
1. 行为等价：八类边界行的 boost/decay 结果与逐行手算一致；
2. 查询计划：候选 SQL 在 10 万行夹具上不走全表 SCAN；
3. 裁剪有效：候选 SELECT 只回候选行（~5%），不再回全表。
"""
import os
import sqlite3
import tempfile

import pytest

import ducky.utils as utils

_TMPDIR = tempfile.mkdtemp(prefix="aidumem_v20_4_evolve_idx_")
utils.FACTS_DB = os.path.join(_TMPDIR, "facts.db")
utils.SALIENCE_DB = os.path.join(_TMPDIR, "salience.db")

import ducky.evolve_mem as evolve_mem  # noqa: E402

_NOW = 1_800_000_000.0
_OLD = _NOW - 30 * 86400      # 30 天前（远超 14 天窗口）
_RECENT = _NOW - 86400        # 1 天前（窗口内）


def _seed_salience(rows):
    conn = sqlite3.connect(utils.SALIENCE_DB)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS salience ("
        "memory_id TEXT PRIMARY KEY, salience REAL NOT NULL DEFAULT 0.5,"
        "last_access REAL NOT NULL, access_count INTEGER NOT NULL DEFAULT 0,"
        "created_at REAL NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_salience ON salience(salience)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_last_access ON salience(last_access)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_access_count ON salience(access_count)")
    conn.executemany(
        "INSERT OR REPLACE INTO salience(memory_id, salience, last_access, access_count, created_at)"
        " VALUES(?,?,?,?,?)",
        rows)
    conn.commit()
    conn.close()


@pytest.fixture()
def evolve_sandbox(monkeypatch):
    """隔离 salience/evolve 两库，并冻结时钟到 _NOW。

    为什么必须在 fixture 里**重新钉路径**（不能只靠模块头那次赋值）：
    pytest 收集期会 import 全部测试模块，本模块头对 utils.SALIENCE_DB 的
    钉扎会被**字母序更靠后**的模块（test_v20_fallback_discipline /
    test_v20_ledger_evolve_bank_scope / test_v20_salience_bank_scope 等）
    在 import 时覆盖 —— 运行时 utils.SALIENCE_DB 指向的是别人的 tmpdir。
    更糟的是 get_salience_conn 走线程本地连接缓存（按路径字符串做 key）：
    排在本模块前面的用例会先把那条外来路径的连接缓存上，本 fixture 的
    os.remove 只删文件、删不掉那条打开着的连接（旧 inode 还活着），
    run_evolution_cycle 读到的就是别人库里的残行 —— 全量跑时曾因此
    decayed=8（外来残行被冻结时钟全部判成超窗），单跑却全绿。
    monkeypatch 钉扎在用例结束自动还原，也不把本模块的路径泄漏给别人。
    """
    monkeypatch.setattr(utils, "SALIENCE_DB", os.path.join(_TMPDIR, "salience.db"))
    monkeypatch.setattr(utils, "FACTS_DB", os.path.join(_TMPDIR, "facts.db"))
    for f in (utils.SALIENCE_DB, utils.FACTS_DB):
        if os.path.exists(f):
            os.remove(f)
    monkeypatch.setattr(evolve_mem.time, "time", lambda: _NOW)
    monkeypatch.setattr(evolve_mem, "EVOLVE_DB_PATH",
                        os.path.join(_TMPDIR, "evolve_mem.db"))
    return utils.SALIENCE_DB


def _row(mid, sal, acc, last):
    return (mid, sal, last, acc, _NOW - 40 * 86400)


class TestCycleSemanticsUnchanged:
    """八类边界行逐类钉死（行为等价锚：对旧实现也应成立）。"""

    def test_boost_decay_matrix(self, evolve_sandbox):
        H = evolve_mem.HIGH_HIT_THRESHOLD  # 5
        _seed_salience([
            _row("boost_ok", 0.50, H + 2, _RECENT),    # 高频+窗口内 → boost(+0.02)
            _row("boost_edge", 0.89, H + 3, _RECENT),  # sal<0.9 贴边 → boost(+0.03)
            _row("decay_ok", 0.50, 0, _OLD),           # 超窗未访问 → decay
            _row("both_boost_wins", 0.50, H + 2, _OLD),  # 两条件都中 → 只 boost
            _row("high_sal_decay", 0.95, H, _OLD),     # boost 条件 sal 卡住 → 落 elif → decay
            _row("floor_no_decay", 0.20, 0, _OLD),     # sal≤0.25 → 不动
            _row("active_no_action", 0.50, 0, _RECENT),  # 窗口内低频 → 不动
            _row("ceiling_no_boost", 0.90, H, _RECENT),  # sal≥0.9 且窗口内 → 不动
        ])
        result = evolve_mem.run_evolution_cycle()
        assert result["boosted"] == 3, f"boost 应恰 3 行：{result}"
        assert result["decayed"] == 2, f"decay 应恰 2 行：{result}"

        conn = sqlite3.connect(utils.SALIENCE_DB)
        got = dict(conn.execute("SELECT memory_id, salience FROM salience").fetchall())
        conn.close()
        assert got["boost_ok"] > 0.50 and got["boost_edge"] > 0.89
        assert got["both_boost_wins"] > 0.50, "两条件都中的行必须走 boost 分支"
        assert got["decay_ok"] < 0.50 and got["high_sal_decay"] < 0.95
        assert got["floor_no_decay"] == 0.20 and got["active_no_action"] == 0.50
        assert got["ceiling_no_boost"] == 0.90


class TestCandidateQueryPlan:
    """修复前必红：旧实现没有候选 SQL（全表扫），10 万行上计划含 SCAN。"""

    def _seed_100k(self):
        rows = []
        for i in range(100_000):
            if i % 20 == 0:      # 5% 候选（一半 boost 一半 decay）
                rows.append(_row(f"cand_{i}", 0.5, 9, _OLD))
            else:
                rows.append(_row(f"skip_{i}", 0.5, 0, _RECENT))
        _seed_salience(rows)

    def test_plan_uses_index_not_full_scan(self, evolve_sandbox):
        self._seed_100k()
        sql = evolve_mem._EVOLVE_CANDIDATE_SQL
        conn = sqlite3.connect(utils.SALIENCE_DB)
        plan = " ".join(
            str(r) for r in conn.execute(
                f"EXPLAIN QUERY PLAN {sql}",
                (evolve_mem.HIGH_HIT_THRESHOLD, _NOW - evolve_mem.LOW_HIT_WINDOW_DAYS * 86400),
            ).fetchall())
        conn.close()
        assert "SCAN salience" not in plan, f"候选查询仍在全表扫：{plan}"
        assert "INDEX" in plan.upper(), f"候选查询未走索引：{plan}"

    def test_candidate_select_returns_only_candidates(self, evolve_sandbox):
        self._seed_100k()
        conn = sqlite3.connect(utils.SALIENCE_DB)
        n = conn.execute(
            f"SELECT COUNT(*) FROM ({evolve_mem._EVOLVE_CANDIDATE_SQL})",
            (evolve_mem.HIGH_HIT_THRESHOLD, _NOW - evolve_mem.LOW_HIT_WINDOW_DAYS * 86400),
        ).fetchone()[0]
        conn.close()
        assert n == 5_000, f"候选裁剪应恰回 5%（5000 行），实回 {n}"
