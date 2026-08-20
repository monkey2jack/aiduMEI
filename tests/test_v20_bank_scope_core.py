"""v20 core bank-scope regression tests.

These fixtures intentionally exercise the three ledgers that previously had
no domain namespace.  The tests use a disposable facts database and never
touch the repository's ``data/`` directory.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

import ducky.utils as utils


_TMP = tempfile.mkdtemp(prefix="aidumem_v20_bank_core_")
_DB = os.path.join(_TMP, "facts.db")
_TEXT_DB = os.path.join(_TMP, "text_fts.db")
utils.FACTS_DB = _DB
utils.TEXT_FTS_DB = _TEXT_DB


def _reset_modules():
    import ducky.memory_types as mt
    import ducky.core_memory as cm

    mt._checked = False
    cm._initialized = False
    cm._initialized_scopes.clear()


@pytest.fixture(autouse=True)
def _fresh_db():
    utils.FACTS_DB = _DB
    utils.TEXT_FTS_DB = _TEXT_DB
    conn = sqlite3.connect(_DB)
    for table in (
        "facts", "fact_events", "memory_types", "core_memory", "memory_banks",
        "verbatim_turns",
    ):
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
    text_conn = sqlite3.connect(_TEXT_DB)
    text_conn.execute("DROP TABLE IF EXISTS verbatim_fts")
    text_conn.execute("DROP TABLE IF EXISTS verbatim_fts_map")
    text_conn.commit()
    text_conn.close()
    _reset_modules()
    yield


def test_memory_types_same_ref_isolated_by_user_and_bank():
    from ducky.memory_types import (
        classify_and_record,
        get_batch_memory_types,
        get_memory_type,
        list_types,
        reset_all_types,
    )

    classify_and_record("same-id", "用户偏好 Python", user_id="alice", bank_id="work")
    classify_and_record("same-id", "用户决定迁移", user_id="alice", bank_id="home")
    classify_and_record("same-id", "观察到端口暴露", user_id="bob", bank_id="work")

    assert get_memory_type("same-id", user_id="alice", bank_id="work") == "PREFERENCES"
    assert get_memory_type("same-id", user_id="alice", bank_id="home") == "DECISIONS"
    assert get_memory_type("same-id", user_id="bob", bank_id="work") == "OBSERVATIONS"
    # An omitted/default scope must not see a named-bank record.
    assert get_memory_type("same-id") == "FACTS"

    batch = get_batch_memory_types(["same-id"], user_id="alice", bank_id="home")
    assert batch == {"same-id": "DECISIONS"}
    assert {r["memory_type"]: r["count"] for r in list_types("alice", "work")} == {
        "PREFERENCES": 1
    }

    assert reset_all_types("alice", "work") == 1
    assert get_memory_type("same-id", user_id="alice", bank_id="home") == "DECISIONS"
    assert get_memory_type("same-id", user_id="bob", bank_id="work") == "OBSERVATIONS"


def test_core_memory_same_block_isolated_by_scope():
    from ducky.core_memory import get_all_blocks, get_block, inject_context, init_core_memory, put_block

    init_core_memory("alice", "work")
    init_core_memory("alice", "home")
    put_block("core_current_project", "工作域内容仅属于 work bank", "alice", "work")
    put_block("core_current_project", "家庭域内容仅属于 home bank", "alice", "home")

    work = get_block("core_current_project", "alice", "work")
    home = get_block("core_current_project", "alice", "home")
    assert work and "工作域" in work["content"]
    assert home and "家庭域" in home["content"]
    assert "家庭域" not in inject_context("alice", "work")
    assert "工作域" not in inject_context("alice", "home")
    assert len(get_all_blocks("alice", "work")) == 3
    assert len(get_all_blocks("alice", "home")) == 3


def test_conflict_resolution_does_not_cross_bank_or_user():
    import ducky.conflict_resolver as cr

    conn = sqlite3.connect(_DB)
    conn.executemany(
        "INSERT INTO facts (category, fact_key, fact_value, source, agent_id, archived) "
        "VALUES (?, ?, ?, ?, ?, 0)",
        [
            ("profile", "status", "old-work", "alice", "alice"),
            ("profile", "status", "old-home", "alice", "alice"),
            ("profile", "status", "old-bob", "bob", "bob"),
        ],
    )
    conn.commit()
    # Add canonical columns and assign explicit scopes before resolving.
    from ducky.bank_contract import ensure_memory_banks_schema

    ensure_memory_banks_schema(conn)
    conn.execute(
        "UPDATE facts SET user_id='alice', bank_id='work' WHERE fact_value='old-work'"
    )
    conn.execute(
        "UPDATE facts SET user_id='alice', bank_id='home' WHERE fact_value='old-home'"
    )
    conn.execute(
        "UPDATE facts SET user_id='bob', bank_id='work' WHERE fact_value='old-bob'"
    )
    conn.commit()
    conn.close()

    result = cr.resolve_fact_conflict(
        "profile", "status", "new-work", user_id="alice", bank_id="work"
    )
    assert result["invalidated"] == 1

    conn = sqlite3.connect(_DB)
    rows = conn.execute(
        "SELECT fact_value, valid_to FROM facts ORDER BY id"
    ).fetchall()
    assert rows[0][1] is not None
    assert rows[1][1] is None
    assert rows[2][1] is None
    event = conn.execute(
        "SELECT user_id, bank_id FROM fact_events WHERE event_type='conflict.resolved'"
    ).fetchone()
    assert event == ("alice", "work")
    conn.close()


def test_legacy_schema_migration_is_additive_and_idempotent():
    """存量行必须**逐条原样**活过迁移，且迁移可重复执行。

    ⚠️ 本条初版是这样写的::

        before = SELECT COUNT(*) FROM facts     # 夹具刚 DROP 完，这里是 0
        ensure_memory_banks_schema(conn)
        after  = SELECT COUNT(*) FROM facts     # 还是 0
        assert before == after                  # 0 == 0，永真

    夹具里一条 INSERT 都没有，于是这个名为「迁移不丢数据」的用例
    **从来没有见过一行数据**。它不会红 —— 哪怕迁移把整张表删光也不会红。
    这正是本项目反复付学费的那类断言：**自洽，但什么都没证明**。

    现改为：先铺真实存量行（v19 形态、没有 user_id/bank_id 两列），
    再用**集合比对**验证每一行的每个字段原样还在 —— 不是比条数。
    「还剩 3 条」和「还是原来那 3 条」是两回事，v19.4.1 的幽灵 id
    连环案（日志漂亮报「成功删除 25/25」而向量库分毫未变）就栽在前者上。
    """
    from ducky.bank_contract import DEFAULT_BANK_ID, ensure_memory_banks_schema
    from ducky.utils import DEFAULT_USER_ID

    legacy_rows = [
        ("profile", "nickname", "船长", "cli", "cli"),
        ("profile", "timezone", "Asia/Shanghai", "hook", "hook"),
        ("project", "codename", "含 ' 单引号 与 \x1f 之外的怪字符", "mcp", "mcp"),
    ]
    conn = sqlite3.connect(_DB)
    conn.executemany(
        "INSERT INTO facts (category, fact_key, fact_value, source, agent_id) "
        "VALUES (?,?,?,?,?)",
        legacy_rows,
    )
    conn.commit()

    # 迁移前必须确实**有**数据，否则下面的集合比对会退化成 set() == set()。
    before = set(
        conn.execute(
            "SELECT category, fact_key, fact_value, source, agent_id FROM facts"
        ).fetchall()
    )
    assert len(before) == len(legacy_rows) > 0, "正面锚点：存量行没铺进去，本条会空转"

    first = ensure_memory_banks_schema(conn)
    second = ensure_memory_banks_schema(conn)

    after = set(
        conn.execute(
            "SELECT category, fact_key, fact_value, source, agent_id FROM facts"
        ).fetchall()
    )
    cols = {row[1] for row in conn.execute("PRAGMA table_info(facts)").fetchall()}
    scopes = conn.execute("SELECT DISTINCT user_id, bank_id FROM facts").fetchall()
    conn.close()

    assert first["status"] == "ok" and second["status"] == "ok"
    assert {"user_id", "bank_id"} <= cols, "additive 迁移没把两列加上"

    # 集合比对：双向都要空，才叫「一行没丢、一行没变、一行没多」。
    assert not (before - after), f"迁移把存量行弄丢/改写了: {sorted(before - after)}"
    assert not (after - before), f"迁移凭空多出了行: {sorted(after - before)}"

    # 老行一律落进默认域，且**只**落进默认域。
    assert scopes == [(DEFAULT_USER_ID, DEFAULT_BANK_ID)], (
        f"存量行被分配到了非默认作用域: {scopes} —— 渠道标记不等于所有权，"
        "迁移期不得把 source/agent_id 重新解释成归属"
    )

    # 幂等：第二次调用不得再宣称加过列。
    assert second["added_columns"] == [], (
        f"迁移不幂等，第二次仍报告加列: {second['added_columns']}"
    )


def test_default_bank_keeps_the_v19_key_shape_for_every_tenant():
    """默认域的存储键必须与 v19 逐字相同 —— 对**任何**租户都是。

    ⚠️ 这条是从一次真实的静默失败里长出来的。首版 `scoped_storage_key`
    写的是「user 是默认 **且** bank 是默认才保持原样」，于是
    `(user_id="tenant_a", bank_id="default")` 这种再普通不过的组合
    也会被加上前缀。后果不是报错，是：

      * 写进去用带前缀的键，读出来用带前缀的键 —— 自己跟自己对得上，测试全绿；
      * 而**升级前就存在的**那些行用的是裸键，从此再也没人去读它们。

    也就是说，所有非默认租户的 v19 存量记忆会在升级那一刻**整体失踪**，
    服务不报错、健康检查全绿、计数照样是数字。实测症状是 tombstone 恢复
    宣称 `restored=True`，回查 `SELECT content ... WHERE id='mem-003'`
    却是 None ——「恢复成功」和「什么都没恢复」返回同一个东西。

    判据只有一条：**默认域看 bank，不看 user**。
    """
    from ducky.bank_contract import (
        DEFAULT_BANK_ID,
        make_scope,
        raw_storage_key,
        scoped_storage_key,
    )
    from ducky.utils import DEFAULT_USER_ID

    raw = "mem-003"

    # ① 默认租户 + 默认域：v19 原样（这条即使写错也会过，故不能只有它）。
    assert scoped_storage_key(raw, make_scope(DEFAULT_USER_ID, DEFAULT_BANK_ID)) == raw

    # ② 非默认租户 + 默认域：**这就是当初翻车的那一格**。
    for tenant in ("tenant_a", "alice", "bob", "李雷"):
        scope = make_scope(tenant, DEFAULT_BANK_ID)
        assert scoped_storage_key(raw, scope) == raw, (
            f"租户 {tenant!r} 在默认域里的键被加了前缀 —— "
            "升级瞬间该租户的 v19 存量记忆会整体变成读不到的孤儿"
        )
        # 所有权不靠键的形状兜底，靠列。
        assert scope.user_id == tenant and scope.bank_id == DEFAULT_BANK_ID

    # ③ 具名域必须加前缀，否则跨域会撞同一个单列主键 —— 后写的直接盖掉先写的。
    named = make_scope("alice", "work")
    scoped = scoped_storage_key(raw, named)
    assert scoped != raw and scoped.endswith(raw)
    assert scoped != scoped_storage_key(raw, make_scope("alice", "home")), (
        "同一租户的两个具名域生成了相同的键，跨域覆盖仍然可能发生"
    )
    assert scoped != scoped_storage_key(raw, make_scope("bob", "work")), (
        "两个租户的同名域生成了相同的键，跨租户覆盖仍然可能发生"
    )

    # ④ 往返可逆：具名域能还原出对外的裸键；默认域本来就是裸键。
    assert raw_storage_key(scoped, named) == raw
    assert raw_storage_key(raw, make_scope("tenant_a", DEFAULT_BANK_ID)) == raw


def test_scope_values_are_validated_and_never_interpolated():
    """作用域取值是**不透明标识符**：非法即拒，合法即参数绑定。"""
    import pytest as _pytest

    from ducky.bank_contract import (
        BankScopeError,
        MAX_SCOPE_LENGTH,
        make_scope,
        normalize_bank_id,
        scope_predicate,
    )

    # 负向：控制字符 / 换行 / 路径分隔符 / 超长，一律拒绝。
    for bad in (
        "a\x00b", "a\nb", "a\rb", "a\tb", "a\x1fb", "a\x7fb",
        "../../etc/passwd", "work/sub", "work\\sub", "x" * (MAX_SCOPE_LENGTH + 1),
    ):
        with _pytest.raises(BankScopeError):
            normalize_bank_id(bad)

    # 正面锚点：正常值（含中文、含 SQL 标点）必须放行 ——
    # 否则上面那组「全拒」可能只是因为函数无条件抛异常。
    for ok in ("work", "家庭域", "team-2026", "a'b", "x" * MAX_SCOPE_LENGTH):
        assert normalize_bank_id(ok) == ok

    # 空值回落到默认域，而不是变成一个空字符串域。
    assert normalize_bank_id(None) == "default"
    assert normalize_bank_id("   ") == "default"

    # 谓词只发放精确等值 + 独立参数，绝不把取值拼进 SQL。
    sql, params = scope_predicate(make_scope("alice", "work"), alias="f")
    assert "LIKE" not in sql.upper(), "作用域谓词用了 LIKE —— 边界会被前缀绕过"
    assert sql.count("?") == len(params) == 2
    assert "alice" not in sql and "work" not in sql, (
        f"取值被拼进了 SQL 文本: {sql!r} —— 契约要求参数绑定"
    )
    assert params == ["alice", "work"]


def test_channel_markers_never_grant_ownership_in_legacy_predicate():
    """``source``/``agent_id`` 是渠道标记，不是所有权凭证。

    过渡谓词允许「无主老行」按渠道标记回落可见，这是必要的兼容。
    但回落**不得越过正规归属**：一行已经写明属于 bob，就不能因为它的
    ``source`` 恰好叫 alice 而出现在 alice 的读结果里。``source`` 的取值
    是 ``cli``/``hook``/``mcp`` 这类字符串，租户完全可能同名 —— 一旦并列成
    OR，跨租户读就不需要任何攻击技巧，取个名字就够了。
    """
    from ducky.bank_contract import (
        ensure_memory_banks_schema,
        legacy_fact_scope_predicate,
        make_scope,
    )

    conn = sqlite3.connect(_DB)
    ensure_memory_banks_schema(conn)
    conn.executemany(
        "INSERT INTO facts (category, fact_key, fact_value, source, agent_id, "
        "user_id, bank_id) VALUES (?,?,?,?,?,?,?)",
        [
            # ① 正规归属：属于 alice。
            ("p", "own", "alice-owned", "cli", "cli", "alice", "default"),
            # ② 正规归属：属于 bob，但渠道标记恰好叫 alice —— 陷阱行。
            ("p", "trap", "bob-owned", "alice", "alice", "bob", "default"),
            # ③ 无主老行：user_id 空白，渠道标记记着 alice —— 应当回落可见。
            ("p", "legacy", "legacy-alice", "alice", "alice", "", "default"),
        ],
    )
    conn.commit()

    sql, params = legacy_fact_scope_predicate(make_scope("alice", "default"), alias="f")
    seen = {
        row[0]
        for row in conn.execute(
            f"SELECT fact_value FROM facts f WHERE 1=1{sql}", params
        ).fetchall()
    }

    # 正面锚点：自己的行、以及无主老行的回落，都必须看得见 ——
    # 否则下面那条「看不见 bob」可能只是因为谓词把所有行都挡了。
    assert "alice-owned" in seen, "正规归属的行反而读不到，谓词过紧"
    assert "legacy-alice" in seen, "无主老行的兼容回落失效，v19 存量会失踪"

    # 关键判据：有主的行，渠道标记再像也不给看。
    assert "bob-owned" not in seen, (
        "bob 的行因为 source='alice' 被 alice 读到了 —— "
        "渠道标记被当成了所有权，这是跨租户读"
    )

    # 具名域永不回落到无主行：兼容只发生在默认域。
    named_sql, named_params = legacy_fact_scope_predicate(
        make_scope("alice", "work"), alias="f"
    )
    named_seen = {
        row[0]
        for row in conn.execute(
            f"SELECT fact_value FROM facts f WHERE 1=1{named_sql}", named_params
        ).fetchall()
    }
    conn.close()
    assert named_seen == set(), (
        f"具名域 work 回落看到了默认域的行: {named_seen}"
    )


def test_cascade_delete_never_reaches_across_banks_or_tenants():
    """级联删除必须**只**命中请求的那一个 (user, bank)。

    这条盯的是唯一不可逆的路径。旧实现分两支，两支都没有 bank_id，
    且默认支连 user_id 都没有::

        if user_id == "default":
            DELETE FROM facts WHERE id=? OR fact_key=? ...   # 整库
        else:
            ... AND (source=? OR agent_id=?)                 # 无 bank

    于是默认用户删一条，会把所有租户所有域的同名行一起带走；具名租户删
    work 域的一条，会把自己 home 域的也删掉。全过程不抛错 ——
    `res["facts"]` 回报的 rowcount 反而更大，看起来「删得更干净」。

    判据用**集合比对**：删完之后剩下的行必须与预期集合逐条相等。
    只数「还剩 3 条」是不够的 —— 删错一条同时留错一条，条数一样。
    """
    import ducky.wal_engine as we
    from ducky.bank_contract import ensure_memory_banks_schema

    conn = sqlite3.connect(_DB)
    ensure_memory_banks_schema(conn)
    conn.executemany(
        "INSERT INTO facts (category, fact_key, fact_value, source, agent_id, "
        "user_id, bank_id) VALUES (?,?,?,?,?,?,?)",
        [
            ("p", "shared-key", "alice-work", "cli", "cli", "alice", "work"),
            ("p", "shared-key", "alice-home", "cli", "cli", "alice", "home"),
            ("p", "shared-key", "bob-work", "cli", "cli", "bob", "work"),
            ("p", "shared-key", "default-row", "cli", "cli", "default", "default"),
        ],
    )
    conn.commit()
    conn.close()

    we.cascade_delete_memory("shared-key", user_id="alice", bank_id="work")

    conn = sqlite3.connect(_DB)
    survivors = {
        row[0] for row in conn.execute("SELECT fact_value FROM facts").fetchall()
    }
    conn.close()

    assert survivors == {"alice-home", "bob-work", "default-row"}, (
        f"级联删除越界了。存活集合={sorted(survivors)}；"
        "期望只少掉 alice/work 那一条"
    )


def test_cascade_delete_by_default_user_does_not_wipe_other_tenants():
    """默认用户的删除，不得变成「删全库」。

    这是上一条的镜像：`user_id == "default"` 曾经是**无 WHERE 全表删**的
    触发条件（v19.4.1 已在各仓修过一轮，见 version.py P0-3），v20 引入
    bank 之后它以新形态复活在 facts 支上。默认用户是最常见的调用者，
    这条路径每天都在跑。
    """
    import ducky.wal_engine as we
    from ducky.bank_contract import ensure_memory_banks_schema

    conn = sqlite3.connect(_DB)
    ensure_memory_banks_schema(conn)
    conn.executemany(
        "INSERT INTO facts (category, fact_key, fact_value, source, agent_id, "
        "user_id, bank_id) VALUES (?,?,?,?,?,?,?)",
        [
            ("p", "dup-key", "default-own", "cli", "cli", "default", "default"),
            ("p", "dup-key", "alice-work", "cli", "cli", "alice", "work"),
            ("p", "dup-key", "bob-default", "cli", "cli", "bob", "default"),
        ],
    )
    conn.commit()
    conn.close()

    we.cascade_delete_memory("dup-key", user_id="default", bank_id="default")

    conn = sqlite3.connect(_DB)
    survivors = {
        row[0] for row in conn.execute("SELECT fact_value FROM facts").fetchall()
    }
    conn.close()

    assert survivors == {"alice-work", "bob-default"}, (
        f"默认用户删除波及了他人。存活集合={sorted(survivors)}；"
        "期望只少掉 default/default 那一条"
    )


def test_verbatim_vault_same_user_bank_isolation_and_delete():
    from ducky.verbatim_vault import (
        cascade_delete_verbatim,
        count_verbatim,
        store_verbatim,
        verbatim_search,
    )

    store_verbatim(
        "alice",
        "工作域原文秘密",
        {"session_id": "v20-work"},
        bank_id="work",
    )
    store_verbatim(
        "alice",
        "家庭域原文秘密",
        {"session_id": "v20-home"},
        bank_id="home",
    )
    assert verbatim_search("工作域", "alice", bank_id="work")
    assert verbatim_search("工作域", "alice", bank_id="home") == []
    assert verbatim_search("家庭域", "alice", bank_id="home")
    assert count_verbatim("alice", "work") == 1
    assert count_verbatim("alice", "home") == 1

    assert cascade_delete_verbatim("alice", "work") == 1
    assert count_verbatim("alice", "work") == 0
    assert count_verbatim("alice", "home") == 1
