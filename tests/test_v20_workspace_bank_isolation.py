"""v20 工作区（working memory）域隔离回归。

工作区是缓存，但它的内容会被注入上下文 —— 一旦跨域串味，用户看到的是
「家庭域的记忆出现在工作域的对话里」。所以它的隔离要求与主存一样硬。
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest


_TMP = tempfile.mkdtemp(prefix="aidumem_v20_ws_")


@pytest.fixture(autouse=True)
def _fresh_workspace(monkeypatch):
    """每条用例一份全新的 workspace.db，绝不碰仓库 data/。"""
    import ducky.pipeline.memory_workspace as mw

    db = os.path.join(_TMP, f"ws_{os.urandom(6).hex()}.db")
    monkeypatch.setattr(mw, "WORKSPACE_DB", db)
    monkeypatch.setattr(mw, "_db_initialized", False)
    monkeypatch.setattr(mw, "_workspace", {})
    yield mw


def test_same_memory_id_survives_in_two_banks(_fresh_workspace):
    """同一个 memory_id 在两个域里必须**各自独立存活**。

    ⚠️ 原表主键是 ``(user_id, memory_id)``，bank_id 是 v20 用 ALTER 加的
    普通列 —— 而 SQLite 无法用 ALTER 把一列并入主键。于是：

        ws_push(alice, "m-1", "工作域内容", bank_id="work")
        ws_push(alice, "m-1", "家庭域内容", bank_id="home")

    第二次写入在 ``ON CONFLICT(user_id, memory_id) DO UPDATE`` 上命中，
    直接**覆盖**掉工作域那条。表里有 bank_id 列、有 (user_id,bank_id)
    索引、每个函数都老实传了 bank_id —— 唯独主键没跟上，隔离就是假的。
    不报错、不告警，`ws_status` 还会各报各的数。
    """
    mw = _fresh_workspace

    mw.ws_push("alice", "m-1", "工作域内容", bank_id="work")
    mw.ws_push("alice", "m-1", "家庭域内容", bank_id="home")
    mw.ws_push("bob", "m-1", "另一个租户", bank_id="work")

    conn = sqlite3.connect(mw.WORKSPACE_DB)
    rows = set(
        conn.execute("SELECT user_id, bank_id, memory_id, text FROM workspace").fetchall()
    )
    conn.close()

    assert rows == {
        ("alice", "work", "m-1", "工作域内容"),
        ("alice", "home", "m-1", "家庭域内容"),
        ("bob", "work", "m-1", "另一个租户"),
    }, f"跨域/跨租户发生了覆盖，实际落库: {sorted(rows)}"


def test_workspace_pk_actually_contains_bank_id(_fresh_workspace):
    """直接钉主键本身 —— 行为测试会绿，是因为主键对了，而不是碰巧。"""
    mw = _fresh_workspace
    mw._ensure_db()

    conn = sqlite3.connect(mw.WORKSPACE_DB)
    info = conn.execute("PRAGMA table_info(workspace)").fetchall()
    conn.close()
    pk_cols = {row[1] for row in info if row[5] > 0}

    assert pk_cols == {"user_id", "bank_id", "memory_id"}, (
        f"workspace 主键是 {sorted(pk_cols)}，缺 bank_id 则域隔离结构性不可能"
    )


def test_pk_rebuild_carries_every_legacy_row_over(_fresh_workspace):
    """旧库升级：重建主键必须**一行不少**地搬运存量。

    用集合比对而非计数 —— 「还是 3 条」和「还是原来那 3 条」是两回事。
    """
    mw = _fresh_workspace

    # 先手工造一个 v19 形态的旧库：主键 (user_id, memory_id)，没有 bank_id。
    os.makedirs(os.path.dirname(mw.WORKSPACE_DB), exist_ok=True)
    conn = sqlite3.connect(mw.WORKSPACE_DB)
    conn.execute(
        """
        CREATE TABLE workspace (
            user_id      TEXT NOT NULL,
            memory_id    TEXT NOT NULL,
            text         TEXT NOT NULL DEFAULT '',
            score        REAL NOT NULL DEFAULT 0.0,
            metadata     TEXT NOT NULL DEFAULT '{}',
            created_at   TEXT NOT NULL DEFAULT '',
            access_count INTEGER NOT NULL DEFAULT 1,
            last_accessed REAL NOT NULL DEFAULT 0.0,
            PRIMARY KEY (user_id, memory_id)
        )
        """
    )
    legacy = [
        ("alice", "m-1", "旧的工作笔记", 0.5, "{}", "2026-01-01", 3, 1.0),
        ("alice", "m-2", "另一条旧记忆", 0.7, "{}", "2026-01-02", 1, 2.0),
        ("bob", "m-1", "bob 的旧记忆", 0.9, "{}", "2026-01-03", 5, 3.0),
    ]
    conn.executemany(
        "INSERT INTO workspace (user_id, memory_id, text, score, metadata, "
        "created_at, access_count, last_accessed) VALUES (?,?,?,?,?,?,?,?)",
        legacy,
    )
    conn.commit()
    conn.close()

    mw._db_initialized = False
    mw._ensure_db()

    conn = sqlite3.connect(mw.WORKSPACE_DB)
    after = set(
        conn.execute(
            "SELECT user_id, memory_id, text, score, metadata, created_at, "
            "access_count, last_accessed FROM workspace"
        ).fetchall()
    )
    banks = {row[0] for row in conn.execute("SELECT DISTINCT bank_id FROM workspace")}
    pk_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(workspace)").fetchall()
        if row[5] > 0
    }
    conn.close()

    assert after == set(legacy), (
        f"重建丢/改了存量行。少了 {sorted(set(legacy) - after)}；"
        f"多了 {sorted(after - set(legacy))}"
    )
    assert banks == {"default"}, f"存量行被搬到了非默认域: {banks}"
    assert "bank_id" in pk_cols, "重建后主键仍不含 bank_id"


def test_cold_eviction_actually_reaches_sqlite(_fresh_workspace):
    """冷记忆淘汰必须**真的落到盘上**，不能只从内存里消失。

    ⚠️ `_maybe_cleanup` 遍历 `_workspace` 拿到的 uid 是 v20 新引入的复合键
    ``user\\x1fbank``，却原样当 user_id 传给 `_db_delete` —— DELETE 条件变成
    ``user_id='alice\\x1fwork' AND bank_id='default'``，匹配 0 行且无人过问
    rowcount。内存里干净了，盘上还在，进程一重启就被 `_db_load_all` 原样
    捞回来：清理形同虚设，workspace.db 只涨不消。
    """
    import time

    mw = _fresh_workspace

    mw.ws_push("alice", "m-cold", "该被淘汰的冷记忆", bank_id="work")
    mw.ws_push("alice", "m-hot", "常访问的热记忆", bank_id="work")

    now = time.time()
    scope_key = mw._scope_key("alice", "work")
    # 冷：很久没碰 + 访问次数低；热：刚访问过。
    mw._workspace[scope_key]["m-cold"]["last_accessed"] = now - mw.WORKSPACE_TTL_SECONDS - 10
    mw._workspace[scope_key]["m-cold"]["access_count"] = 1
    mw._workspace[scope_key]["m-hot"]["last_accessed"] = now

    mw._last_cleanup = 0
    mw._maybe_cleanup(now)

    conn = sqlite3.connect(mw.WORKSPACE_DB)
    rows = {r[0] for r in conn.execute("SELECT memory_id FROM workspace").fetchall()}
    conn.close()

    assert "m-hot" in rows, "正面锚点：热记忆被误删，说明清理条件本身就跑偏了"
    assert "m-cold" not in rows, (
        f"冷记忆只从内存淘汰、没落到 SQLite，盘上仍有: {sorted(rows)}"
    )


def test_clear_one_bank_leaves_the_other_intact(_fresh_workspace):
    """清空一个域，不得动到同租户的另一个域。"""
    mw = _fresh_workspace

    mw.ws_push("alice", "m-1", "工作域内容", bank_id="work")
    mw.ws_push("alice", "m-2", "家庭域内容", bank_id="home")
    mw.ws_clear("alice", bank_id="work")

    conn = sqlite3.connect(mw.WORKSPACE_DB)
    rows = set(
        conn.execute("SELECT bank_id, memory_id FROM workspace").fetchall()
    )
    conn.close()

    assert rows == {("home", "m-2")}, (
        f"清空 work 域波及了 home 域，剩余: {sorted(rows)}"
    )
