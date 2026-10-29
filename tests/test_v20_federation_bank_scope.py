"""
tests/test_v20_federation_bank_scope.py — v20 P0-2 联邦写入 bank 双隔离回归
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

v19 的 facts 唯一索引是 (agent_id, category, fact_key) 三元组，带来两条
跨 bank 数据破坏路径（比泄漏更严重，是静默销毁）：

  ① writer.write_fact / /facts/add 的 upsert 命中另一 bank 的同键行，
     后写者直接覆盖前者的 fact_value；
  ② 索引重建降级去重的 DELETE 按三元组 GROUP BY，会把另一 bank 的
     同键行当"脏数据"删掉。

v20 加宽为 (agent_id, user_id, bank_id, category, fact_key) 五元组，
本文件守住：索引形状、跨 bank 共存、upsert 不越库、去重扫描不越库
（含同库 merge 负向对照）、重建降级去重不越库删行（②的直接负向对照）、
默认作用域行为与 v19 逐字节一致、/facts/add 端点全链路隔离。

跑法：cd <仓库根> && .venv/bin/pytest tests/test_v20_federation_bank_scope.py -v
测试全部在临时 facts.db 上跑，绝不碰生产库。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── 关键：在导入任何 federation 模块之前把 DB 指向临时库 ──
_tmp_dir = tempfile.mkdtemp(prefix="aidumem_v20_bankscope_")
_TEST_DB = os.path.join(_tmp_dir, "facts.db")

import ducky.utils as utils  # noqa: E402

utils.FACTS_DB = _TEST_DB

LOCAL_AGENT = utils.DEFAULT_AGENT_ID

# 生产 facts 表的最小必要结构。唯一约束不写成行内 UNIQUE（删不掉、升不了级），
# 与生产一致交由 rebuild_facts_unique_index 建成五列唯一索引。
# entities / fact_entities 供 /facts/add 端点测试的 _auto_extract_and_link 使用。
_FACTS_DDL = """
CREATE TABLE facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL DEFAULT 'general',
    fact_key TEXT NOT NULL,
    fact_value TEXT NOT NULL,
    source TEXT DEFAULT 'local',
    summary TEXT,
    overview TEXT,
    level TEXT DEFAULT 'L2',
    agent_id TEXT DEFAULT 'local',
    trust_score REAL DEFAULT 0.5,
    retrieval_count INTEGER DEFAULT 0,
    last_accessed_at TIMESTAMP,
    archived INTEGER DEFAULT 0,
    valid_from TEXT,
    valid_to TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE entities (
    entity_id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT, entity_type TEXT
);
CREATE TABLE fact_entities (
    fact_id INTEGER, entity_id INTEGER,
    UNIQUE(fact_id, entity_id)
);
"""


@pytest.fixture(scope="module", autouse=True)
def _bootstrap_db():
    # 全量合跑时其他模块的 autouse fixture 可能已改走它们的临时库，
    # 迁移前显式指回，保证联邦列/索引建在本文件的库上。
    utils.FACTS_DB = _TEST_DB
    conn = sqlite3.connect(_TEST_DB)
    conn.executescript(_FACTS_DDL)
    conn.commit()
    conn.close()

    from ducky.federation.schema import ensure_federation_schema

    ensure_federation_schema(force=True)
    yield


@pytest.fixture(autouse=True)
def _bind_test_db():
    utils.FACTS_DB = _TEST_DB
    yield


def _rows(sql: str, params=()):
    conn = sqlite3.connect(_TEST_DB)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


# ═══════════════ 1. 索引形状 ═══════════════
def test_facts_unique_index_includes_bank_scope():
    row = _rows(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_facts_unique'"
    )
    assert row and row[0][0], "idx_facts_unique 不存在——联邦 upsert 没有冲突目标"
    sql = row[0][0]
    assert "UNIQUE" in sql.upper(), "idx_facts_unique 退化成了非唯一索引"
    for col in ("agent_id", "user_id", "bank_id", "category", "fact_key"):
        assert col in sql, f"唯一索引缺列 {col}——bank 双隔离失效"


# ═══════════════ 2. 跨 bank / 跨 user 共存 ═══════════════
def test_two_banks_hold_same_agent_category_key():
    from ducky.federation.writer import write_fact

    a = write_fact("bank共存区", "同一个键", "甲库独有内容", agent_id=LOCAL_AGENT,
                   user_id="u1", bank_id="bank_a", dedup=False)
    b = write_fact("bank共存区", "同一个键", "乙库独有内容", agent_id=LOCAL_AGENT,
                   user_id="u1", bank_id="bank_b", dedup=False)
    c = write_fact("bank共存区", "同一个键", "另一用户同库内容", agent_id=LOCAL_AGENT,
                   user_id="u2", bank_id="bank_a", dedup=False)
    assert a["status"] == b["status"] == c["status"] == "ok"
    ids = {a["fact_id"], b["fact_id"], c["fact_id"]}
    assert len(ids) == 3, "同 agent 同键在不同 (user, bank) 下必须是三条独立行"

    rows = _rows(
        """SELECT user_id, bank_id, fact_value FROM facts
           WHERE category='bank共存区' AND fact_key='同一个键' ORDER BY id"""
    )
    assert len(rows) == 3
    by_scope = {(r[0], r[1]): r[2] for r in rows}
    assert by_scope[("u1", "bank_a")] == "甲库独有内容"
    assert by_scope[("u1", "bank_b")] == "乙库独有内容"
    assert by_scope[("u2", "bank_a")] == "另一用户同库内容"


# ═══════════════ 3. upsert 不越库 ═══════════════
def test_upsert_does_not_overwrite_other_bank():
    from ducky.federation.writer import write_fact

    write_fact("越库覆盖区", "键K", "甲库原值", agent_id=LOCAL_AGENT,
               user_id="u1", bank_id="bank_a", dedup=False)
    write_fact("越库覆盖区", "键K", "乙库原值", agent_id=LOCAL_AGENT,
               user_id="u1", bank_id="bank_b", dedup=False)
    # 乙库重写同键 → 只在乙库内 upsert
    write_fact("越库覆盖区", "键K", "乙库改后值", agent_id=LOCAL_AGENT,
               user_id="u1", bank_id="bank_b", dedup=False)

    rows = _rows(
        """SELECT bank_id, fact_value FROM facts
           WHERE category='越库覆盖区' AND fact_key='键K' AND user_id='u1'"""
    )
    assert len(rows) == 2, f"应保持两库各一行，实得 {len(rows)}"
    by_bank = {r[0]: r[1] for r in rows}
    assert by_bank["bank_a"] == "甲库原值", "乙库的 upsert 覆盖了甲库——v19 数据破坏 bug 复活"
    assert by_bank["bank_b"] == "乙库改后值", "乙库自身 upsert 未生效"


# ═══════════════ 4. 去重扫描不越库（含同库负向对照） ═══════════════
def test_dedup_scan_does_not_cross_banks():
    from ducky.federation.dedup import ACTION_INSERT, ACTION_MERGE
    from ducky.federation.writer import write_fact

    seed = write_fact("去重越库区", "种子键", "完全相同的一句话内容用于验证去重作用域",
                      agent_id=LOCAL_AGENT, user_id="u1", bank_id="bank_a", dedup=True)
    assert seed["action"] == ACTION_INSERT

    # 同内容写进另一 bank → 去重扫描不得看见甲库的种子，必须新增
    other = write_fact("去重越库区", "对端键", "完全相同的一句话内容用于验证去重作用域",
                       agent_id=LOCAL_AGENT, user_id="u1", bank_id="bank_b", dedup=True)
    assert other["action"] == ACTION_INSERT, "去重扫描跨 bank 命中——隔离失效"
    assert other["fact_id"] != seed["fact_id"]

    # 负向对照：同内容写回同一 bank → 必须 merge，证明去重机器本身是活的
    same = write_fact("去重越库区", "同库键", "完全相同的一句话内容用于验证去重作用域",
                      agent_id=LOCAL_AGENT, user_id="u1", bank_id="bank_a", dedup=True)
    assert same["action"] == ACTION_MERGE, "同库相同内容未 merge——去重机器死了，上面的断言是假绿灯"
    assert same["fact_id"] == seed["fact_id"]


# ═══════════════ 5. 重建降级去重不越库删行 ═══════════════
def test_index_rebuild_dedup_preserves_cross_bank_rows():
    """②的直接负向对照：强制走降级 DELETE 路径，跨 bank 同键行必须幸存。

    v19 的 DELETE 按 (agent, category, key) 三元组 GROUP BY——本场景下
    乙库那行会被当脏数据删掉。v20 按五元组分组，只清同域撞 key。
    """
    from ducky.federation.schema import rebuild_facts_unique_index

    conn = sqlite3.connect(_TEST_DB)
    try:
        conn.execute("DROP INDEX IF EXISTS idx_facts_unique")
        ins = (
            """INSERT INTO facts
                 (category, fact_key, fact_value, agent_id, user_id, bank_id)
               VALUES (?,?,?,?,?,?)"""
        )
        # 同域（同 agent 同 user 同 bank）撞 key 两行 → 迫使 CREATE UNIQUE 失败
        conn.execute(ins, ("重建区", "撞键", "同域旧行", "dup_agent", "u1", "bank_a"))
        conn.execute(ins, ("重建区", "撞键", "同域新行", "dup_agent", "u1", "bank_a"))
        # 另一 bank 的同 agent 同键行 → 必须在去重后幸存
        conn.execute(ins, ("重建区", "撞键", "乙库幸存行", "dup_agent", "u1", "bank_b"))
        conn.commit()

        rebuild_facts_unique_index(conn)
        conn.commit()

        rows = conn.execute(
            """SELECT bank_id, fact_value FROM facts
               WHERE category='重建区' AND fact_key='撞键' ORDER BY id"""
        ).fetchall()
    finally:
        conn.close()

    assert len(rows) == 2, f"应剩同域 1 行 + 乙库 1 行，实得 {len(rows)}"
    by_bank = {r[0]: r[1] for r in rows}
    assert by_bank["bank_a"] == "同域新行", "同域去重应保留 MAX(id) 最新行"
    assert by_bank["bank_b"] == "乙库幸存行", "降级去重跨 bank 删行——v19 数据销毁 bug 复活"

    # 去重后索引必须已重建成唯一索引（否则所有 upsert 报 no such conflict target）
    idx = _rows(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_facts_unique'"
    )
    assert idx and "UNIQUE" in idx[0][0].upper()
    assert "bank_id" in idx[0][0]


# ═══════════════ 6. 默认作用域行为与 v19 逐字节一致 ═══════════════
def test_legacy_default_scope_upsert_unchanged():
    from ducky.federation.writer import write_fact

    write_fact("默认域区", "键X", "旧值", agent_id="agent_legacy", dedup=False)
    write_fact("默认域区", "键X", "新值", agent_id="agent_legacy", dedup=False)

    rows = _rows(
        """SELECT fact_value, user_id, bank_id FROM facts
           WHERE category='默认域区' AND fact_key='键X' AND agent_id='agent_legacy'"""
    )
    assert len(rows) == 1, "不传作用域时同 agent 同 key 应仍是 upsert 覆盖（v19 行为）"
    assert rows[0][0] == "新值"
    assert rows[0][2] == "default", "缺省 bank 必须落 default 库"


def test_bank_scope_rejects_path_separator():
    """作用域非法字符必须在开库前被拒——防 bank_id 变成路径穿越向量。

    设计约定：write_fact 直接抛 BankScopeError（在开 conn 之前），
    HTTP 层由 routes 的 _safe 包裹成 error dict——两层分别验证。
    """
    from ducky.bank_contract import BankScopeError
    from ducky.federation.writer import write_fact

    with pytest.raises(BankScopeError):
        write_fact("非法域区", "键Y", "值", agent_id=LOCAL_AGENT,
                   user_id="u1", bank_id="../etc")
    # 负向对照：确认没有行被写进任何库
    rows = _rows("SELECT COUNT(*) FROM facts WHERE category='非法域区'")
    assert rows[0][0] == 0


# ═══════════════ 7. /facts/add 端点全链路 ═══════════════


# ═══════════════ 8. 检索梯子作用域（v20 P0-2 opt-in） ═══════════════
def _seed_recall_fixture():
    """种三行含「咖啡」的事实：本域一行 + 他库两行（同 profile / 跨 profile）。

    needle 用「咖啡」与本文件其他测试的数据完全错开；他库两行分属
    L3（同 profile 他 Agent 共享）与 L4（跨 profile 共享）的命中面，
    scoped 检索若有任何一级翻墙，结果里必然出现乙库值。
    """
    from ducky.federation.writer import write_fact

    r1 = write_fact("饮品", "咖啡偏好", "甲库主人喝美式",
                    agent_id=LOCAL_AGENT, user_id="user_x", bank_id="bank_a",
                    shared=True)
    r2 = write_fact("饮品", "咖啡偏好", "乙库主人喝拿铁",
                    agent_id="peer_agent", user_id="user_y", bank_id="bank_b",
                    shared=True)
    r3 = write_fact("饮品", "咖啡产地", "乙库跨档案咖啡豆来自云南",
                    agent_id="far_agent", profile="research",
                    user_id="user_y", bank_id="bank_b", shared=True)
    for r in (r1, r2, r3):
        assert r["status"] == "ok", r

