"""v20 delete_all 与 WAL 重放的作用域回归。

`cascade_delete_all` 是全系统唯一「按租户批量销毁」的入口，v20 引入 bank 后
它有过三处越界形态（向量无域批删 / FTS 只按 user 取集合 / facts 走旧渠道
字段）。这里把修复后的契约钉死：

1. 任何一仓的删除都只命中 (user_id, bank_id) 精确作用域；
2. 向量侧**永远**走「枚举 + 单条删除」，无作用域的 ``mem.delete_all``
   一次也不许出现——枚举失败时宁可如实上报不完整，也不许退回批删；
3. WAL 重放必须还原完整作用域：payload 里的 bank_id 是权威值，v19 旧条目
   没有该键时回落默认域；delete_all 的重放要补 confirm=True，否则默认
   用户的恢复会在闸门上永远失败（WAL 条目的存在即原调用已过闸的证明）。
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

import ducky.utils as utils

_TMP = tempfile.mkdtemp(prefix="aidumem_v20_delete_all_")
_DB = os.path.join(_TMP, "facts.db")
_TEXT_DB = os.path.join(_TMP, "text_fts.db")
_SAL_DB = os.path.join(_TMP, "salience.db")
utils.FACTS_DB = _DB
utils.TEXT_FTS_DB = _TEXT_DB
utils.SALIENCE_DB = _SAL_DB

# pytest 在跑任何用例前会把全部测试模块都 import 一遍，上面的重定向因此
# 对整个套件生效；ducky.salience.db 早已在旧路径上建过表，新临时库里
# 必须立刻补建 salience 主表，否则其他模块的显著性用例会踩到
# 「no such table: salience」。
from ducky.salience.db import ensure_db as _ensure_salience_db

_ensure_salience_db()


@pytest.fixture(autouse=True)
def _fresh_db():
    utils.FACTS_DB = _DB
    utils.TEXT_FTS_DB = _TEXT_DB
    utils.SALIENCE_DB = _SAL_DB
    conn = sqlite3.connect(_DB)
    for table in ("facts", "fact_events", "memory_types", "memory_banks"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute(
        """
        CREATE TABLE facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL DEFAULT 'general',
            fact_key TEXT NOT NULL,
            fact_value TEXT NOT NULL,
            source TEXT DEFAULT 'local',
            agent_id TEXT DEFAULT 'local',
            archived INTEGER DEFAULT 0,
            valid_to TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE fact_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            category TEXT DEFAULT '', fact_key TEXT DEFAULT '',
            new_value TEXT DEFAULT '', affected_ids TEXT DEFAULT '[]',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()
    tconn = sqlite3.connect(_TEXT_DB)
    tconn.execute("DROP TABLE IF EXISTS memories")
    tconn.execute(
        """
        CREATE TABLE memories (
            id TEXT PRIMARY KEY,
            content TEXT DEFAULT '',
            user_id TEXT NOT NULL DEFAULT 'default',
            bank_id TEXT NOT NULL DEFAULT 'default'
        )
        """
    )
    tconn.commit()
    tconn.close()
    import ducky.memory_types as mt
    mt._checked = False
    yield


class _FakeMem:
    """mem0 替身：记录 delete 调用，且把无作用域 delete_all 焊死为违规。"""

    def __init__(self, items, fail_get_all=False):
        self._items = list(items)
        self._fail = fail_get_all
        self.deleted: list[str] = []

    def get_all(self, filters=None, top_k=None, **kw):
        if self._fail:
            raise RuntimeError("向量后端不可用（模拟）")
        return {"results": list(self._items)}

    def delete(self, mid):
        self.deleted.append(str(mid))

    def delete_all(self, *a, **kw):  # pragma: no cover - 触发即测试失败
        raise AssertionError(
            "cascade_delete_all 调用了无作用域的 mem.delete_all —— "
            "这正是 v20 修掉的跨域批删路径"
        )


def _seed_facts(rows):
    from ducky.bank_contract import ensure_memory_banks_schema

    conn = sqlite3.connect(_DB)
    ensure_memory_banks_schema(conn)
    conn.executemany(
        "INSERT INTO facts (category, fact_key, fact_value, source, agent_id, "
        "user_id, bank_id) VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


def _seed_fts(rows):
    conn = sqlite3.connect(_TEXT_DB)
    conn.executemany(
        "INSERT INTO memories (id, content, user_id, bank_id) VALUES (?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


def test_delete_all_facts_and_fts_only_touch_requested_bank(monkeypatch):
    """具名租户清空 work 域：home 域与他人一行不许少。判据用集合比对。"""
    import ducky.mem0_runtime as rt
    import ducky.wal_engine as we

    _seed_facts([
        ("p", "k1", "alice-work", "cli", "cli", "alice", "work"),
        ("p", "k2", "alice-home", "cli", "cli", "alice", "home"),
        ("p", "k3", "bob-work", "cli", "cli", "bob", "work"),
        ("p", "k4", "default-row", "cli", "cli", "default", "default"),
    ])
    _seed_fts([
        ("bank:work:aw1", "alice work 文本", "alice", "work"),
        ("ah1", "alice home 文本", "alice", "home"),
        ("bank:work:bw1", "bob work 文本", "bob", "work"),
    ])
    from ducky.memory_types import ensure_memory_types_schema
    ensure_memory_types_schema()
    conn = sqlite3.connect(_DB)
    conn.executemany(
        "INSERT INTO memory_types (memory_ref, memory_type, user_id, bank_id, memory_ref_raw) "
        "VALUES (?,?,?,?,?)",
        [
            ("v-aw", "FACTS", "alice", "work", "v-aw"),
            ("v-ah", "FACTS", "alice", "home", "v-ah"),
            ("v-bw", "FACTS", "bob", "work", "v-bw"),
        ],
    )
    conn.commit(); conn.close()
    fake = _FakeMem([
        {"id": "v-aw", "metadata": {"bank_id": "work", "user_id": "alice"}},
        {"id": "v-ah", "metadata": {"bank_id": "home", "user_id": "alice"}},
    ])
    monkeypatch.setattr(rt, "get_memory", lambda: fake)

    out = we.cascade_delete_all(user_id="alice", bank_id="work")
    assert out["status"] == "ok"

    conn = sqlite3.connect(_DB)
    fact_survivors = {r[0] for r in conn.execute("SELECT fact_value FROM facts")}
    conn.close()
    assert fact_survivors == {"alice-home", "bob-work", "default-row"}, (
        f"facts 越界：存活集合={sorted(fact_survivors)}"
    )

    conn = sqlite3.connect(_TEXT_DB)
    fts_survivors = {r[0] for r in conn.execute("SELECT id FROM memories")}
    conn.close()
    assert fts_survivors == {"ah1", "bank:work:bw1"}, (
        f"FTS 越界：存活集合={sorted(fts_survivors)}"
    )

    conn = sqlite3.connect(_DB)
    type_survivors = {
        tuple(r)
        for r in conn.execute("SELECT user_id, bank_id, memory_ref FROM memory_types")
    }
    conn.close()
    assert type_survivors == {
        ("alice", "home", "v-ah"),
        ("bob", "work", "v-bw"),
    }, f"memory_types 越界或留孤儿：存活集合={sorted(type_survivors)}"


def test_delete_all_vector_side_is_scoped_enumeration_only(monkeypatch):
    """向量侧只删目标域已盖章的点；legacy 无戳点属默认域，具名域清空不许碰。"""
    import ducky.mem0_runtime as rt
    import ducky.wal_engine as we

    fake = _FakeMem([
        {"id": "v-work", "metadata": {"bank_id": "work", "user_id": "alice"}},
        {"id": "v-home", "metadata": {"bank_id": "home", "user_id": "alice"}},
        {"id": "v-legacy", "metadata": {"user_id": "alice"}},  # v19 存量＝默认域
    ])
    monkeypatch.setattr(rt, "get_memory", lambda: fake)

    out = we.cascade_delete_all(user_id="alice", bank_id="work")
    det = out["details"]
    assert fake.deleted == ["v-work"], (
        f"向量删除越界：实际删除={fake.deleted}，期望只删 work 域那一个"
    )
    assert det["mem0_deleted"] is True
    assert det["mem0_vector_count"] == 1
    assert det["vector_enumeration_complete"] is True


def test_delete_all_enumeration_failure_never_falls_back_to_bulk(monkeypatch):
    """枚举失败＝如实上报不完整；绝不能退回无作用域批删或谎报成功。"""
    import ducky.mem0_runtime as rt
    import ducky.wal_engine as we

    fake = _FakeMem([], fail_get_all=True)
    monkeypatch.setattr(rt, "get_memory", lambda: fake)

    out = we.cascade_delete_all(user_id="alice", bank_id="work")
    det = out["details"]
    assert fake.deleted == [], "枚举失败后不许发生任何向量删除"
    assert det["mem0_deleted"] is False
    assert det["vector_enumeration_complete"] is False


def test_reconcile_replay_restores_bank_scope_and_confirm(monkeypatch):
    """WAL 重放：payload.bank_id 是权威值；旧条目回落默认域；
    delete_all 重放补 confirm=True。"""
    import ducky.wal_engine as we

    calls: list[tuple] = []
    monkeypatch.setattr(
        we, "cascade_delete_memory",
        lambda mid, user_id=None, bank_id=None: calls.append(
            ("delete", mid, user_id, bank_id)
        ),
    )
    monkeypatch.setattr(
        we, "cascade_delete_all",
        lambda user_id=None, confirm=False, bank_id=None: calls.append(
            ("delete_all", user_id, confirm, bank_id)
        ),
    )

    entries = [
        we.WALEntry(
            user_id="alice", operation="delete",
            payload={"memory_id": "m1", "user_id": "alice", "bank_id": "work"},
        ),
        # v19 旧条目：payload 无 bank_id，条目字段是 dataclass 默认值
        we.WALEntry(
            user_id="bob", operation="delete",
            payload={"memory_id": "m2", "user_id": "bob"},
        ),
        we.WALEntry(
            user_id="default", operation="delete_all",
            payload={"user_id": "default", "bank_id": "home"},
        ),
    ]

    class _FakeWAL:
        def get_pending_entries(self):
            return entries

        def mark_status(self, *a, **kw):
            pass

    monkeypatch.setattr(we.WALEngine, "get_instance", classmethod(
        lambda cls: _FakeWAL()
    ))

    report = we.reconcile_startup()
    assert report["recovered"] == 3 and report["failed"] == 0
    assert ("delete", "m1", "alice", "work") in calls, (
        f"重放丢了 payload 里的 bank_id：{calls}"
    )
    assert ("delete", "m2", "bob", we.DEFAULT_BANK_ID) in calls, (
        f"旧条目未回落默认域：{calls}"
    )
    assert ("delete_all", "default", True, "home") in calls, (
        f"delete_all 重放缺 confirm=True 或丢 bank：{calls}"
    )
