"""aiduMEI v20 P0-2 — 事件账本作用域戳 + evolve 反馈域守卫测试

账本侧（ducky/event_ledger.py）：
1. 盖戳往返：具名域事件只出现在该域账本视图；default 域视图把
   无戳存量（空串）一并算进来；不传作用域 = v19 管理员全库视图
2. 改名默认身份（如 alice）与 'default'/空串折叠同域（v19.4.2 教训）
3. 非法作用域直接抛 BankScopeError，不许静默降级成全库视图
4. 老表（v19 无作用域列）就地迁移重试：账一条都不能丢
5. 端到端：set_opinion 写入的 opinion_set 事件带着 facts 行的域戳

evolve 侧（ducky/evolve_mem.py + routes_evolve.py）：
6. 不传作用域 = v19 管理员语义零改动
7. 越库反馈被拒（BankScopeError），salience 与反馈表毫发无损（负向对照）；
   本域反馈照常生效
8. 非法 bank_id 直接拒
9. 行不存在 + opt-in 作用域 → 预插带戳行，不被无戳 INSERT 错盖成 default
10. 改名默认身份折叠；他人不许蹭折叠（负向对照）
11. v19 salience 表（无作用域列）整表归 default 域，具名域声称一律拒
12. 路由层 /evolve/feedback 把 user_id/bank_id 递进 record_feedback
    （防「函数修了、路由没传」），错误按 {"status":"error"} 约定返回
"""
from __future__ import annotations

import os
import tempfile

import pytest

import ducky.utils as utils

_TMPDIR = tempfile.mkdtemp(prefix="aidumem_v20_ledger_evolve_")
_FACTS_DB = os.path.join(_TMPDIR, "facts.db")
_SALIENCE_DB = os.path.join(_TMPDIR, "salience.db")
_EVOLVE_DB = os.path.join(_TMPDIR, "evolve_mem.db")
utils.FACTS_DB = _FACTS_DB
utils.SALIENCE_DB = _SALIENCE_DB

import ducky.evolve_mem as evolve_mem  # noqa: E402
from ducky.bank_contract import BankScopeError  # noqa: E402
from ducky.event_ledger import (  # noqa: E402
    ensure_ledger_schema,
    get_history,
    record_event,
)
from ducky.evolve_mem import record_feedback  # noqa: E402

# v19 形状的账本表：没有作用域列
_LEDGER_DDL_V19 = """
CREATE TABLE memory_events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    actor       TEXT NOT NULL DEFAULT 'system',
    action      TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    reason      TEXT DEFAULT '',
    before_hash TEXT DEFAULT '',
    after_hash  TEXT DEFAULT ''
)
"""

_FACTS_DDL_V20 = """
CREATE TABLE facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL DEFAULT 'general',
    fact_key TEXT NOT NULL,
    fact_value TEXT NOT NULL,
    trust_score REAL DEFAULT 0.5,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    user_id TEXT NOT NULL DEFAULT 'default',
    bank_id TEXT NOT NULL DEFAULT 'default'
)
"""

_SALIENCE_DDL_V20 = """
CREATE TABLE salience (
    memory_id TEXT PRIMARY KEY,
    salience REAL DEFAULT 0.5,
    last_access REAL,
    access_count INTEGER DEFAULT 0,
    created_at REAL,
    user_id TEXT NOT NULL DEFAULT 'default',
    bank_id TEXT NOT NULL DEFAULT 'default'
)
"""

_SALIENCE_DDL_V19 = """
CREATE TABLE salience (
    memory_id TEXT PRIMARY KEY,
    salience REAL DEFAULT 0.5,
    last_access REAL,
    access_count INTEGER DEFAULT 0,
    created_at REAL
)
"""


def _facts_conn():
    return utils.get_facts_conn()


def _sal_conn():
    return utils.get_salience_conn()


@pytest.fixture(autouse=True)
def _fresh_dbs(monkeypatch):
    monkeypatch.setattr(utils, "FACTS_DB", _FACTS_DB)
    monkeypatch.setattr(utils, "SALIENCE_DB", _SALIENCE_DB)
    monkeypatch.setattr(evolve_mem, "EVOLVE_DB_PATH", _EVOLVE_DB)
    conn = _facts_conn()
    for table in ("memory_events", "facts", "opinions"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute(_FACTS_DDL_V20)
    conn.commit()
    ensure_ledger_schema()
    sal = _sal_conn()
    sal.execute("DROP TABLE IF EXISTS salience")
    sal.execute(_SALIENCE_DDL_V20)
    sal.commit()
    if os.path.exists(_EVOLVE_DB):
        os.remove(_EVOLVE_DB)
    yield


def _record(target, user_id="", bank_id="", action="update"):
    conn = _facts_conn()
    eid = record_event(conn, actor="test", action=action, target_id=target,
                       reason="t", user_id=user_id, bank_id=bank_id)
    conn.commit()
    return eid


def _seed_salience(memory_id, user_id="default", bank_id="default", salience=0.5):
    sal = _sal_conn()
    sal.execute(
        "INSERT INTO salience(memory_id, salience, last_access, access_count, "
        "created_at, user_id, bank_id) VALUES(?,?,0,0,0,?,?)",
        (memory_id, salience, user_id, bank_id),
    )
    sal.commit()


def _salience_row(memory_id):
    row = _sal_conn().execute(
        "SELECT * FROM salience WHERE memory_id=?", (memory_id,)
    ).fetchone()
    return dict(row) if row else None


def _feedback_count():
    evolve_mem.ensure_evolve_schema()
    conn = evolve_mem.get_evolve_conn()
    n = conn.execute("SELECT COUNT(*) FROM evolve_feedback").fetchone()[0]
    conn.close()
    return n


# ═══════════════════════════════════════════════
# 账本侧
# ═══════════════════════════════════════════════

def test_ledger_stamp_roundtrip_and_bank_views():
    """具名域事件只进该域视图；default 域含无戳存量；不传作用域=全库。"""
    assert _record("mem_1", user_id="user_x", bank_id="bank_a") is not None
    assert _record("mem_1") is not None  # 无戳存量（v19 的账）
    assert _record("mem_1", user_id="user_x", bank_id="bank_b") is not None

    admin = get_history("mem_1")
    assert len(admin) == 3, "不传作用域必须保持 v19 管理员全库视图"

    in_a = get_history("mem_1", bank_id="bank_a")
    assert [e["bank_id"] for e in in_a] == ["bank_a"]

    in_default = get_history("mem_1", bank_id="default")
    assert len(in_default) == 1
    assert in_default[0]["bank_id"] == "", "无戳存量必须算进 default 域的账"

    in_b = get_history("mem_1", user_id="user_x", bank_id="bank_b")
    assert len(in_b) == 1 and in_b[0]["bank_id"] == "bank_b"


def test_ledger_renamed_default_identity_collapses(monkeypatch):
    """部署方改名默认身份（alice）后，空串/'default'/alice 折叠同域查账；
    其他用户不许蹭折叠（负向对照）。"""
    monkeypatch.setattr(utils, "DEFAULT_USER_ID", "alice")
    _record("mem_2")                                  # 空串存量
    _record("mem_2", user_id="default", bank_id="default")
    _record("mem_2", user_id="alice", bank_id="default")
    _record("mem_2", user_id="user_y", bank_id="default")

    mine = get_history("mem_2", user_id="alice")
    assert len(mine) == 3
    assert all(e["user_id"] != "user_y" for e in mine)

    theirs = get_history("mem_2", user_id="user_y")
    assert len(theirs) == 1 and theirs[0]["user_id"] == "user_y"


def test_ledger_invalid_scope_raises_not_degrades():
    """非法作用域必须抛 BankScopeError，不许静默降级成全库视图。"""
    _record("mem_3", user_id="user_x", bank_id="bank_a")
    with pytest.raises(BankScopeError):
        get_history("mem_3", bank_id="../etc")
    with pytest.raises(BankScopeError):
        get_history("mem_3", user_id="a/b")


def test_ledger_v19_table_migrated_in_place_no_event_lost():
    """老表没有作用域列：record_event 就地迁移后重试，账一条都不能丢。"""
    conn = _facts_conn()
    conn.execute("DROP TABLE IF EXISTS memory_events")
    conn.execute(_LEDGER_DDL_V19)
    conn.commit()

    eid = _record("mem_old", user_id="user_x", bank_id="bank_a")
    assert eid is not None, "迁移重试后这条账必须落下"
    row = _facts_conn().execute(
        "SELECT user_id, bank_id FROM memory_events WHERE event_id=?", (eid,)
    ).fetchone()
    assert (row["user_id"], row["bank_id"]) == ("user_x", "bank_a")


def test_ledger_opinion_set_carries_fact_scope_end_to_end():
    """set_opinion 的 opinion_set 事件必须带着所属 facts 行的域戳。"""
    from ducky.opinion import set_opinion

    conn = _facts_conn()
    cur = conn.execute(
        "INSERT INTO facts (fact_key, fact_value, user_id, bank_id) "
        "VALUES ('k', 'v', 'user_x', 'bank_a')"
    )
    conn.commit()
    fid = cur.lastrowid

    res = set_opinion(fid, "support", source="src1", owner="user_x")
    assert res["ok"] is True

    events = get_history(f"fact:{fid}", user_id="user_x", bank_id="bank_a")
    assert any(e["action"] == "opinion_set" for e in events), \
        "opinion_set 事件必须落在 bank_a 的账本视图里"
    assert get_history(f"fact:{fid}", bank_id="bank_b") == []


# ═══════════════════════════════════════════════
# evolve 反馈侧
# ═══════════════════════════════════════════════

def test_evolve_feedback_unscoped_keeps_v19_admin_behavior():
    """不传作用域 = v19 管理员语义：具名域记忆也能按 id 反馈。"""
    _seed_salience("m1", user_id="user_x", bank_id="bank_a")
    res = record_feedback("m1", "useful")
    assert res["ok"] is True
    assert res["new_salience"] == pytest.approx(0.65)


def test_evolve_feedback_cross_bank_refused_salience_untouched():
    """越库反馈被拒，salience 与反馈表毫发无损；本域反馈照常生效。"""
    _seed_salience("m2", user_id="user_x", bank_id="bank_a")

    with pytest.raises(BankScopeError):
        record_feedback("m2", "useful", user_id="user_x", bank_id="bank_b")
    with pytest.raises(BankScopeError):
        record_feedback("m2", "useless", user_id="user_y", bank_id="bank_a")

    # 负向对照：两次被拒后一切原样
    assert _salience_row("m2")["salience"] == pytest.approx(0.5)
    assert _feedback_count() == 0

    res = record_feedback("m2", "useful", user_id="user_x", bank_id="bank_a")
    assert res["ok"] is True
    assert _salience_row("m2")["salience"] == pytest.approx(0.65)
    assert _feedback_count() == 1


def test_evolve_feedback_invalid_bank_rejected():
    _seed_salience("m3", user_id="user_x", bank_id="bank_a")
    with pytest.raises(BankScopeError):
        record_feedback("m3", "useful", user_id="user_x", bank_id="../etc")
    assert _salience_row("m3")["salience"] == pytest.approx(0.5)


def test_evolve_feedback_missing_row_preinserts_stamped_row():
    """行不存在 + opt-in 作用域：预插带戳行，不被无戳 INSERT 错盖 default。"""
    res = record_feedback("m_new", "useful", user_id="user_x", bank_id="bank_a")
    assert res["ok"] is True
    row = _salience_row("m_new")
    assert (row["user_id"], row["bank_id"]) == ("user_x", "bank_a")
    assert row["salience"] == pytest.approx(0.65)


def test_evolve_feedback_renamed_default_identity_collapses(monkeypatch):
    """改名默认身份（alice）折叠命中存量 'default' 行；他人不许蹭折叠。"""
    monkeypatch.setattr(utils, "DEFAULT_USER_ID", "alice")
    _seed_salience("m4", user_id="default", bank_id="default")

    res = record_feedback("m4", "useful", user_id="alice", bank_id="default")
    assert res["ok"] is True
    assert _salience_row("m4")["salience"] == pytest.approx(0.65)

    with pytest.raises(BankScopeError):
        record_feedback("m4", "useful", user_id="someone_else", bank_id="default")
    assert _salience_row("m4")["salience"] == pytest.approx(0.65)


def test_evolve_feedback_v19_salience_table_falls_back_default():
    """老库 salience 无作用域列：整表归 default 域。裸调/默认域照常，
    具名域声称一律拒（含行不存在的情况——没有列就盖不了戳）。"""
    sal = _sal_conn()
    sal.execute("DROP TABLE IF EXISTS salience")
    sal.execute(_SALIENCE_DDL_V19)
    sal.execute(
        "INSERT INTO salience(memory_id, salience, last_access, access_count, "
        "created_at) VALUES('m_old', 0.5, 0, 0, 0)"
    )
    sal.commit()

    res = record_feedback("m_old", "useful")
    assert res["ok"] is True

    res = record_feedback("m_old", "useful", user_id="default", bank_id="default")
    assert res["ok"] is True

    with pytest.raises(BankScopeError):
        record_feedback("m_old", "useful", user_id="user_x", bank_id="bank_a")
    with pytest.raises(BankScopeError):
        record_feedback("m_missing", "useful", user_id="user_x", bank_id="bank_a")


def test_evolve_feedback_route_threads_scope_params():
    """POST /evolve/feedback 必须把 user_id/bank_id 递进 record_feedback；
    越库按路由约定返回 {"status":"error"}，不改任何状态。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from ducky.routes_evolve import register_evolve_routes

    _seed_salience("m5", user_id="user_x", bank_id="bank_a")

    app = FastAPI()
    register_evolve_routes(app)
    client = TestClient(app)

    resp = client.post("/evolve/feedback", json={
        "memory_id": "m5", "signal": "useful",
        "user_id": "user_x", "bank_id": "bank_b",
    })
    assert resp.status_code == 200
    assert resp.json()["status"] == "error", "错库声称必须被路由层递进护栏拒绝"
    assert _salience_row("m5")["salience"] == pytest.approx(0.5)

    resp = client.post("/evolve/feedback", json={
        "memory_id": "m5", "signal": "useful",
        "user_id": "user_x", "bank_id": "bank_a",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["new_salience"] == pytest.approx(0.65)
