"""v20.3.2-beta P0-2：存量库上 `memory_types` 迁移顺序错，租户隔离静默失效。

外审 P1-A（Qwen 独家发现，小猴实测复现，输出与其报告逐字吻合）。

`ensure_memory_types_schema()` 里，v20.2.4 外审 F-16 的**三列唯一约束**
（`user_id, bank_id, memory_ref_raw`）被建在**补这三列的 ALTER 循环之前**：

    L101  CREATE UNIQUE INDEX … ON memory_types(user_id, bank_id, memory_ref_raw)
    L126  for column, ddl in (("user_id", …), ("bank_id", …), ("memory_ref_raw", …)):
    L132      ALTER TABLE memory_types ADD COLUMN …

对 v20.0 之前建的表（无这三列），L101 抛 `no such column: user_id`，被函数最外层
`except Exception` 吞成一条 WARNING，于是**整个初始化中止** —— 包括本该补列的循环。

四层后果，逐层递进：
  1. F-16 修的那个跨用户覆盖漏洞，在**唯一需要它的场景**（存量多租户库升级）依然可利用；
     新库因 CREATE TABLE 自带三列而不受影响 —— **于是本地测试全绿，生产升级后防护缺席**。
  2. `_checked` 永不置位，而本函数是**按请求调用**（routes_p1.py / wal_engine.py 四处）
     → 热路径上每次命中都重跑一遍注定失败的 DDL 并再打一条 WARNING。
  3. 与自家铁律冲突：确实「出声」了，但出的是**重复的、不指明后果的**声 ——
     读者无法从「表初始化失败（服务继续）」知道「租户隔离已失效」。
  4. 零测试覆盖：121 个测试文件**没有一个**构造缺列形状的表。
     缺陷自 2026-08-27 引入，历经 5 个 Tag 未被发现。

**顺序即语义** —— 一个修安全缺陷的提交，静默废掉了它自己正在修的那个防护。
"""
import sqlite3

import pytest


def _legacy_table(db_path):
    """造一个 v19.x 形状的 memory_types：只有 5 列，无 scope 三列。"""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE memory_types (
            memory_ref   TEXT PRIMARY KEY,
            memory_type  TEXT NOT NULL DEFAULT 'FACTS',
            source       TEXT DEFAULT 'rule',
            confidence   REAL DEFAULT 0.5,
            updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("INSERT INTO memory_types(memory_ref, memory_type) VALUES ('legacy-ref-1','FACTS')")
    conn.commit()
    conn.close()


def _cols(db_path):
    c = sqlite3.connect(db_path)
    try:
        return [r[1] for r in c.execute("PRAGMA table_info(memory_types)")]
    finally:
        c.close()


def _indexes(db_path):
    c = sqlite3.connect(db_path)
    try:
        return [r[1] for r in c.execute("PRAGMA index_list(memory_types)")]
    finally:
        c.close()


@pytest.fixture()
def legacy_db(tmp_path, monkeypatch):
    import ducky.utils as u
    db = str(tmp_path / "facts.db")
    monkeypatch.setattr(u, "FACTS_DB", db, raising=False)
    import ducky.memory_types as mt
    monkeypatch.setattr(mt, "_checked", False, raising=False)
    # get_facts_conn 可能已绑定旧路径，逐处对齐（替身签名必须与生产一致）
    monkeypatch.setattr(mt, "get_facts_conn", lambda: sqlite3.connect(db), raising=False)
    _legacy_table(db)
    assert "user_id" not in _cols(db), "夹具前提破了：造出来的表不该有 scope 列"
    return db, mt


def test_legacy_table_gets_scope_columns_backfilled(legacy_db):
    """**P0-2 靶心**：存量缺列表必须被补齐，而不是抛异常后整段中止。"""
    db, mt = legacy_db
    mt.ensure_memory_types_schema()
    cols = _cols(db)
    missing = [c for c in ("user_id", "bank_id", "memory_ref_raw") if c not in cols]
    assert not missing, (
        f"存量库缺列未被补齐：{missing}；实际列={cols}。"
        "F-16 的跨用户覆盖防护在存量多租户库上处于失效状态"
    )


def test_legacy_table_gets_the_tenant_unique_index(legacy_db):
    """三列唯一约束必须真的建起来 —— 它就是 F-16 的防护本体。"""
    db, mt = legacy_db
    mt.ensure_memory_types_schema()
    idx = _indexes(db)
    assert "idx_memory_types_scope_ref" in idx, (
        f"唯一域索引缺席：{idx}。两个用户拿同一 memory_ref 写入会互相覆盖类型与归属"
    )


def test_schema_check_flag_is_set_so_hot_path_stops_retrying(legacy_db):
    """`_checked` 必须置位：本函数按请求调用，不置位就是热路径持续无效功 + 日志洪水。"""
    db, mt = legacy_db
    mt.ensure_memory_types_schema()
    assert mt._checked is True, (
        "_checked 未置位 —— 每个 /memory/types 请求都会重跑一遍注定失败的 DDL"
    )


def test_second_call_is_silent(legacy_db, caplog):
    """幂等：第二次调用不许再打初始化失败告警。"""
    db, mt = legacy_db
    mt.ensure_memory_types_schema()
    monkey_checked = mt._checked
    mt._checked = False           # 强制重跑一遍，验证的是「表已就位后是否安静」
    caplog.clear()
    with caplog.at_level("WARNING"):
        mt.ensure_memory_types_schema()
    noisy = [r.message for r in caplog.records if "初始化失败" in r.message]
    assert not noisy, f"表已就位仍在报初始化失败：{noisy}"
    assert monkey_checked is True


def test_new_database_path_is_unchanged(tmp_path, monkeypatch):
    """**回归**：全新库（CREATE TABLE 自带三列）路径必须不受影响。"""
    import ducky.utils as u
    db = str(tmp_path / "fresh.db")
    monkeypatch.setattr(u, "FACTS_DB", db, raising=False)
    import ducky.memory_types as mt
    monkeypatch.setattr(mt, "_checked", False, raising=False)
    monkeypatch.setattr(mt, "get_facts_conn", lambda: sqlite3.connect(db), raising=False)
    mt.ensure_memory_types_schema()
    cols = _cols(db)
    for c in ("user_id", "bank_id", "memory_ref_raw"):
        assert c in cols, f"新库路径回归失败，缺 {c}"
    assert "idx_memory_types_scope_ref" in _indexes(db)
    assert mt._checked is True


def test_migration_order_is_guarded_structurally():
    """**元守卫**：补列必须出现在建唯一域索引之前 —— 顺序即语义，不许再被调换。

    这条是承重项：下一次有人在这个函数里插语句时，它会替我们记住这一课。
    """
    import ast
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "ducky" / "memory_types.py"
    text = src.read_text(encoding="utf-8")
    tree = ast.parse(text)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "ensure_memory_types_schema")
    body = ast.get_source_segment(text, fn) or ""
    alter_at = body.find("ADD COLUMN {column}")
    index_at = body.find("idx_memory_types_scope_ref")
    assert alter_at != -1, "补列循环不见了 —— 守卫失去着力点，请同步改判据"
    assert index_at != -1, "唯一域索引不见了 —— 守卫失去着力点，请同步改判据"
    assert alter_at < index_at, (
        "唯一域索引又被建在补列之前了：存量缺列表会抛 no such column 并中止整段初始化"
    )


def test_migration_failure_is_reported_into_the_degradation_ledger(tmp_path, monkeypatch):
    """防护建不起来时，必须**真的**进 degraded 台账 —— 而不是被 except 吞掉。

    这条守的是我自己刚犯的错：第一版写的是 `DegradationTracker.mark(...)`，
    而真实 API 是 `record_degradation(component, reason, *, severity)`。
    名字对不上会抛 AttributeError，被外层 except 接住 → 「定性」这一步
    根本没发生，而日志看起来一切正常。**替身/调用签名必须对齐生产。**
    """
    import ducky.memory_types as mt
    from ducky.degradation import DegradationTracker

    db = str(tmp_path / "ro.db")
    _legacy_table(db)
    monkeypatch.setattr(mt, "_checked", False, raising=False)
    monkeypatch.setattr(mt, "get_facts_conn", lambda: sqlite3.connect(db), raising=False)

    # 让补列必然失败：ALTER 一律抛错，逼出 SchemaMigrationError 那条路
    real_connect = sqlite3.connect

    class _NoAlter:
        def __init__(self, path): self._c = real_connect(path)
        def execute(self, sql, *a, **k):
            if "ADD COLUMN" in sql:
                raise sqlite3.OperationalError("attempt to write a readonly database")
            return self._c.execute(sql, *a, **k)
        def commit(self): return self._c.commit()
        def close(self): return self._c.close()

    monkeypatch.setattr(mt, "get_facts_conn", lambda: _NoAlter(db), raising=False)
    recorded = []
    monkeypatch.setattr(DegradationTracker, "record_degradation",
                        classmethod(lambda cls, c, r, **kw: recorded.append((c, r, kw))))
    mt.ensure_memory_types_schema()
    assert recorded, "防护失效却没进 degraded 台账 —— /health 说不出哪条防护无效"
    component, reason, kw = recorded[0]
    assert component == "memory_types_scope_guard"
    assert "失效" in reason, f"理由没说清后果：{reason}"
    assert kw.get("severity") == "error"
