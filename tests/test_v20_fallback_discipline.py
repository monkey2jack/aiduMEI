"""
tests/test_v20_fallback_discipline.py — v20 兼容降级纪律回归
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

本文件守的是 v20 全仓最容易出人命的一种写法：

    先按 (user_id, bank_id) 过滤 → 失败就退回老口径（空集 / 全库 /
    把行一律当 default 域）。

**降级本身是对的**：v19 的老库根本没有 bank_id 列，具名域的行存不下，
只能按老规矩办。错的是用 ``except Exception`` 去接它——那等于宣布
「凡是这条 SQL 出问题，就当这是个老库」。数据库被锁、磁盘写满、连接被
回收，全都会命中同一个降级分支，于是**域过滤被悄悄摘掉**，调用方拿到
跨库的行，而返回值的形状与一次正常查询完全一样。租户隔离要是能被一次
瞬时故障摘掉，它就不叫隔离。

所以 v20 把每个降级出口都改成「先验明病因」：只有 OperationalError 且
消息里确实说了缺列/缺表才算兼容问题，其余原样抛出。本文件逐条钉住：

  ① sqlite 的实测语义（整套纪律的地基：PRAGMA 缺表不抛，查询缺表/缺列抛）
  ②③ is_legacy_schema_error 的正反两面（③ 是负向对照：锁库/满盘/只读/
     错异常类/错基类一律不许被当成老库）
  ④ table_columns 真故障时留痕（并对照健康连接不许乱告警）
  ⑤⑥ reflect 取材：锁库必须抛（且**不许**执行那条全库降级查询），
     缺列照旧降级
  ⑦ run_reflect 端到端：锁库不再回 {"status":"ok","saved":0}
     ——「库被锁」与「本来就没有事实」从此不是同一个响应
  ⑧⑨ 冲突检测：锁库必须抛（且不许把具名域的行贴上 default 标签），
     缺列照旧降级且**照旧能检出矛盾**（证明降级通路是活的，不是只会不崩）
  ⑩ /update 补上遗漏的域注册（写路径里唯一漏注册的端点）

跑法：cd <仓库根> && .venv/bin/pytest tests/test_v20_fallback_discipline.py -v
全部在临时库/假连接上跑，绝不碰生产库。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── 关键：在导入任何业务模块之前把 DB 指向临时库 ──
_TMPDIR = tempfile.mkdtemp(prefix="aidumem_v20_fallback_")
_FACTS_DB = os.path.join(_TMPDIR, "facts.db")
_SALIENCE_DB = os.path.join(_TMPDIR, "salience.db")

import ducky.utils as utils  # noqa: E402

utils.FACTS_DB = _FACTS_DB
utils.SALIENCE_DB = _SALIENCE_DB
utils.TEXT_FTS_DB = os.path.join(_TMPDIR, "text_fts.db")

import ducky.bank_contract as bank_contract  # noqa: E402
import ducky.reflect as reflect  # noqa: E402
import ducky.salience.conflict as conflict  # noqa: E402
from ducky.bank_contract import is_legacy_schema_error, table_columns  # noqa: E402

_FACTS_DDL = """
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL DEFAULT 'general',
    fact_key TEXT NOT NULL,
    fact_value TEXT NOT NULL,
    archived INTEGER DEFAULT 0,
    user_id TEXT NOT NULL DEFAULT 'default',
    bank_id TEXT NOT NULL DEFAULT 'default',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""


@pytest.fixture(autouse=True)
def _bind_test_db(monkeypatch):
    """每条用例都把库路径与模块级缓存复位。

    pytest-randomly 会打乱顺序，reflect._checked 这种模块级 global 必须
    逐例清掉，否则「先跑过谁」会决定后面几条的结果。
    """
    monkeypatch.setattr(utils, "FACTS_DB", _FACTS_DB)
    monkeypatch.setattr(utils, "SALIENCE_DB", _SALIENCE_DB)
    monkeypatch.setattr(reflect, "_checked", False, raising=False)
    conn = sqlite3.connect(_FACTS_DB)
    conn.executescript(_FACTS_DDL)
    conn.execute("DELETE FROM facts")
    conn.execute("DROP TABLE IF EXISTS memory_banks")
    conn.commit()
    conn.close()
    yield


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    """按 SQL 关键词决定「哪条查询炸、炸成什么」的假连接。

    为什么不用真锁库：真锁库得靠另一个进程持写事务，时序不稳，还要把
    连接上的 10 秒 busy_timeout 一起等掉。假连接能精确指定「作用域查询炸、
    降级查询**不许**被执行」——而后半句才是本组用例真正要守的东西：
    降级查询一旦跑了，跨域的行就已经交到调用方手里了。
    """

    def __init__(self, *, fail_on: str, error: BaseException, rows=None):
        self.fail_on = fail_on
        self.error = error
        self.rows = list(rows or [])
        self.executed: list[str] = []
        self.closed = False

    def execute(self, sql, params=()):
        self.executed.append(" ".join(str(sql).split()))
        if self.fail_on in str(sql):
            raise self.error
        return _FakeCursor(self.rows)

    def commit(self):
        pass

    def close(self):
        self.closed = True


def _fallback_queries(conn: _FakeConn, marker: str) -> list[str]:
    """假连接上「不带作用域」的那些查询——一条都不该出现。"""
    return [sql for sql in conn.executed if marker not in sql]


# ═══════════════ ① sqlite 实测语义（整套纪律的地基） ═══════════════
def test_pragma_table_info_on_missing_table_returns_empty_without_raising():
    """PRAGMA 缺表返回空集不抛；查询缺表/缺列才抛 OperationalError。

    这条不测业务代码，测的是 sqlite 本身——因为 v20 那一堆
    「先验明病因再降级」的判断全部压在这个语义上：
      · PRAGMA 不抛 ⇒ table_columns 的 except 只可能被真故障走到，
        「表不存在」根本到不了那里 ⇒ 那里必须无条件告警；
      · 真查询才抛缺表/缺列 ⇒ is_legacy_schema_error 认这两句话是对的。
    这个前提要是哪天变了（换 sqlite 大版本、换驱动），这条会先红，
    而不是等到线上悄悄跨域。
    """
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE t (a INTEGER)")

        assert conn.execute("PRAGMA table_info(不存在的表)").fetchall() == [], \
            "PRAGMA 对缺表居然不是空集了——table_columns 的无条件告警前提失效"

        with pytest.raises(sqlite3.OperationalError) as miss_table:
            conn.execute("SELECT * FROM 不存在的表").fetchall()
        assert "no such table" in str(miss_table.value).lower()

        with pytest.raises(sqlite3.OperationalError) as miss_col:
            conn.execute("SELECT 不存在的列 FROM t").fetchall()
        assert "no such column" in str(miss_col.value).lower()
    finally:
        conn.close()


# ═══════════════ ②③ is_legacy_schema_error 的正反两面 ═══════════════
def test_is_legacy_schema_error_accepts_only_missing_column_or_table():
    for msg in (
        "no such column: bank_id",
        "no such table: facts",
        "NO SUCH COLUMN: user_id",  # sqlite 文案大小写不该影响判定
        "table facts has no such column: bank_id",
    ):
        assert is_legacy_schema_error(sqlite3.OperationalError(msg)) is True, \
            f"老库缺列/缺表被判成真故障，兼容降级会失灵：{msg!r}"


def test_is_legacy_schema_error_rejects_transient_faults():
    """负向对照——这才是本版要堵的口子。

    锁库/满盘/只读一旦被当成「老库」，域过滤就被摘掉了，而调用方拿到的
    结果与一次正常查询逐字节同形，没有任何人会发现。
    """
    for msg in (
        "database is locked",
        "disk I/O error",
        "attempt to write a readonly database",
        "database or disk is full",
        "unable to open database file",
    ):
        assert is_legacy_schema_error(sqlite3.OperationalError(msg)) is False, \
            f"瞬时故障被当成老库，租户隔离会被它悄悄摘掉：{msg!r}"

    # 文案对、异常类不对：不算兼容问题（缺列/缺表只会以 OperationalError 抛出）
    assert is_legacy_schema_error(sqlite3.DatabaseError("no such column: x")) is False
    assert is_legacy_schema_error(sqlite3.IntegrityError("no such table: x")) is False
    assert is_legacy_schema_error(RuntimeError("no such table: x")) is False


# ═══════════════ ④ table_columns 真故障留痕 ═══════════════
def test_table_columns_leaves_a_trace_when_pragma_really_fails(caplog):
    """PRAGMA 真炸时返回空集**并且**必须发一条 WARNING。

    空集照旧返回（调用方的兼容路径是对的），但没有这条日志，
    「库出故障」与「这张表本来就没这些列」在运维侧完全一样。
    """
    dead = sqlite3.connect(":memory:")
    dead.execute("CREATE TABLE facts (id INTEGER, bank_id TEXT)")
    dead.close()  # 之后任何 execute 都会抛 sqlite3.ProgrammingError

    with caplog.at_level(logging.WARNING, logger="aiduMEM.bank_contract"):
        cols = table_columns(dead, "facts")

    assert cols == set(), "真故障时不该编出列名"
    assert any(
        "PRAGMA table_info" in r.message and r.levelno >= logging.WARNING
        for r in caplog.records
    ), "PRAGMA 真炸却一声不响——这正是「静默失败与成功无从区分」"

    # —— 负向对照：健康连接照常返回真列，且不许乱告警 ——
    caplog.clear()
    live = sqlite3.connect(":memory:")
    try:
        live.execute("CREATE TABLE facts (id INTEGER, bank_id TEXT)")
        with caplog.at_level(logging.WARNING, logger="aiduMEM.bank_contract"):
            good = table_columns(live, "facts")
        assert good == {"id", "bank_id"}, f"健康连接的列读错了: {good}"
        assert not [r for r in caplog.records if "PRAGMA table_info" in r.message], \
            "健康连接也告警——告警一旦常亮就等于没有告警"
    finally:
        live.close()


# ═══════════════ ⑤⑥ reflect 取材的降级纪律 ═══════════════
def test_gather_recent_facts_raises_on_locked_db_instead_of_whole_library_fallback(
    monkeypatch,
):
    """锁库必须抛，且**不许**执行那条全库降级查询。

    v19 的降级出口是「去掉 WHERE user_id/bank_id，全库再查一遍」。
    库锁一旦被当成老库，乙库的事实就会被蒸进甲库的洞察，
    而 run_reflect 的返回值与一次正常反思一模一样。
    """
    locked = _FakeConn(
        fail_on="bank_id=?",
        error=sqlite3.OperationalError("database is locked"),
    )
    monkeypatch.setattr(reflect, "get_facts_conn", lambda: locked)

    with pytest.raises(sqlite3.OperationalError) as exc:
        reflect._gather_recent_facts(10, user_id="alice", bank_id="work")
    assert "locked" in str(exc.value)

    assert _fallback_queries(locked, "bank_id=?") == [], (
        "锁库之后仍然执行了全库降级查询——跨库素材已经取到手里了，"
        f"抛不抛都晚了：{locked.executed}"
    )


def test_gather_recent_facts_still_falls_back_on_legacy_missing_column(monkeypatch):
    """正向对照：真的是老库缺列时，降级照旧生效且能取到素材。

    只会抛不会降级的「加固」等于把老库部署直接打死，
    所以正反两面必须同时钉住。
    """
    legacy = _FakeConn(
        fail_on="bank_id=?",
        error=sqlite3.OperationalError("no such column: bank_id"),
        rows=[
            {"id": 7, "category": "偏好", "fact_key": "夜间模式",
             "fact_value": "开", "updated_at": "2026-08-20"},
        ],
    )
    monkeypatch.setattr(reflect, "get_facts_conn", lambda: legacy)

    facts = reflect._gather_recent_facts(10, user_id="alice", bank_id="work")

    assert len(facts) == 1, f"老库降级取材失败，反思在老部署上会永久空转: {facts}"
    assert facts[0]["ref"] == "f1" and facts[0]["id"] == 7
    assert "夜间模式" in facts[0]["text"]
    assert _fallback_queries(legacy, "bank_id=?"), "缺列时降级查询压根没跑"


def test_run_reflect_surfaces_locked_facts_db_instead_of_ok_saved_zero(monkeypatch):
    """端到端：锁库不再伪装成「本来就没有事实」。

    这一条是本组用例的要害。_gather_recent_facts 的内层 handler 早就
    辨明了病因并原样抛出，但外层还有一个 ``except Exception → return []``
    把它接住了——那一步等于没做：run_reflect 拿到空素材，撞上
    「memories 与 facts 皆空」的早退分支，回一个
    {"status":"ok","insights":[],"saved":0}。于是**库被锁**与**本来就
    没有事实**是同一个响应，谁也发现不了。narrow 完内层不看外层，
    改的就是个心情。
    """
    locked = _FakeConn(
        fail_on="bank_id=?",
        error=sqlite3.OperationalError("database is locked"),
    )
    monkeypatch.setattr(reflect, "get_facts_conn", lambda: locked)
    # 建表已在别处验证过；这里连接被换成假的，跳过幂等建表
    monkeypatch.setattr(reflect, "_checked", True)

    # memory=None 且 get_memory 不可用 ⇒ memories 为空，只剩 facts 这条路
    import ducky.mem0_runtime as mem0_runtime

    def _no_memory():
        raise RuntimeError("测试里不连向量库")

    monkeypatch.setattr(mem0_runtime, "get_memory", _no_memory)

    with pytest.raises(sqlite3.OperationalError):
        reflect.run_reflect(memory=None, user_id="alice", bank_id="work", save=False)


# ═══════════════ ⑧⑨ 冲突检测的降级纪律 ═══════════════
def test_detect_conflicts_raises_on_locked_db_instead_of_relabelling_scopes(
    monkeypatch,
):
    """锁库必须抛，且不许把具名域的行贴上 ("default","default")。

    那个降级出口会**改写**每一行的作用域。库锁一旦被当成老库，甲库的
    「要」重新能跟乙库的「不要」配上对，resolve_conflict_salience 再把
    两库的显著性一起腰斩——跨库写污染，且没有任何报错。
    """
    locked = _FakeConn(
        fail_on="bank_id",
        error=sqlite3.OperationalError("database is locked"),
    )
    monkeypatch.setattr(conflict, "get_salience_conn", lambda: locked)

    with pytest.raises(sqlite3.OperationalError) as exc:
        conflict.detect_conflicts()
    assert "locked" in str(exc.value)

    assert _fallback_queries(locked, "bank_id") == [], (
        "锁库之后仍然执行了不带作用域的降级查询——具名域的行已经被贴上 "
        f"default 标签了：{locked.executed}"
    )


def test_detect_conflicts_still_falls_back_on_legacy_missing_column(monkeypatch):
    """正向对照：老库缺列时降级生效，而且**照旧能检出矛盾**。

    只断言「不崩」是假绿灯——降级查询把行改写成 default 域之后，配对
    机器必须还活着，否则老库部署的矛盾检测就静默瘫了。
    """
    legacy = _FakeConn(
        fail_on="bank_id",
        error=sqlite3.OperationalError("no such column: bank_id"),
        rows=[
            ("m1", "semantic", "昨晚的备份任务成功"),
            ("m2", "semantic", "昨晚的备份任务失败"),
        ],
    )
    monkeypatch.setattr(conflict, "get_salience_conn", lambda: legacy)

    conflicts = conflict.detect_conflicts()

    assert len(conflicts) == 1, f"老库降级后矛盾检测瘫了: {conflicts}"
    c = conflicts[0]
    assert {c["memory_a"], c["memory_b"]} == {"m1", "m2"}
    assert c["word_pair"] == "成功↔失败", f"反义词配对错了: {c['word_pair']}"
    assert (c["user_id"], c["bank_id"]) == ("default", "default"), \
        "老库整库本就是单一 default 域，降级标签必须是 default"
    assert _fallback_queries(legacy, "bank_id"), "缺列时降级查询压根没跑"


# ═══════════════ ⑩ /update 的域注册缺口 ═══════════════
def test_update_endpoint_registers_bank_in_registry(monkeypatch):
    """/update 必须把域登记进 memory_banks，且重复调用幂等。

    /update 会把 bank_id 盖进向量 metadata 并按该域重建 FTS 索引——它能
    把一条记忆搬进一个从没被注册过的域。写路径里只有这一处漏了注册
    （add / tombstone / core_memory / conflict_resolver 都调了），结果是
    数据落在某域、memory_banks 里却查不到这个域：域存在与否取决于当初是
    从哪个端点进来的，注册表从此不可信。

    顺带给 bank_contract.list_banks 补上它本来就没有的第一个消费者
    ——那个函数在 __all__ 里，却全仓零调用、零用例。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import ducky.hot.crud as crud

    class _RecordingMemory:
        def __init__(self):
            self.updates = []

        def update(self, memory_id, data=None, metadata=None):
            self.updates.append((memory_id, data, dict(metadata or {})))
            return {"message": "ok"}

    mem = _RecordingMemory()
    monkeypatch.setattr(crud, "get_memory", lambda: mem)

    app = FastAPI()
    crud.register_crud_routes(app)
    client = TestClient(app)

    def _bank_ids():
        return {b["bank_id"] for b in bank_contract.list_banks(user_id="alice")}

    # 非空断言：调用前这个域确实不在注册表里，否则下面的 assert 是假绿灯
    assert "work" not in _bank_ids(), "前置状态不干净，本用例无法证明任何事"

    payload = {"memory_id": "mem-1", "user_id": "alice",
               "bank_id": "work", "content": "季度目标是跑分"}
    resp = client.post("/update", json=payload)
    assert resp.status_code == 200, resp.text
    assert mem.updates, "/update 压根没走到写入——后面的断言没有意义"
    assert mem.updates[0][2].get("bank_id") == "work", \
        f"域戳没盖进向量 metadata: {mem.updates[0][2]}"

    assert "work" in _bank_ids(), \
        "/update 把记忆搬进了 work 域，注册表里却查不到这个域"

    # 幂等：INSERT OR IGNORE，第二次不许长出第二行
    resp2 = client.post("/update", json=payload)
    assert resp2.status_code == 200, resp2.text
    rows = [b for b in bank_contract.list_banks(user_id="alice")
            if b["bank_id"] == "work"]
    assert len(rows) == 1, f"重复注册长出了 {len(rows)} 行，注册不幂等"

    # 负向对照：别的用户不该被顺带登记进去
    assert "work" not in {
        b["bank_id"] for b in bank_contract.list_banks(user_id="bob")
    }, "注册串到了别的租户名下"
