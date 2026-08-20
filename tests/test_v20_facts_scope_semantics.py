"""v20 facts 作用域语义：tenant_clause 迁移路径 + conflict_resolver 门控。

三条契约（与 ducky.bank_contract.legacy_fact_scope_predicate 同一语义）：

A. 已迁移库上默认租户不再「全库可见」——空片段会把具名域的行端给
   默认租户，等于隔离不存在；现在默认租户只看 bank_id='default'。
B. 具名域是 v20 原生概念，严格 (user_id, bank_id)，**没有**渠道回落；
   默认域内 source/agent_id 渠道标记只对「尚无主人」的行
   （user_id 为 NULL/空白/占位 default）生效——已有主人的行，
   渠道字段再像也不给看、更不给失效（conflict_resolver 的写口子）。
C. 未迁移库（无 bank_id 列）保持 v19 形状一个字符不动——那样的库
   存不下具名域的行，全库可见没有泄漏面（v19.4.1 形状测试原样有效）。
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

import ducky.utils as utils

_TMP = tempfile.mkdtemp(prefix="aidumem_v20_facts_scope_")
_DB = os.path.join(_TMP, "facts.db")
utils.FACTS_DB = _DB


@pytest.fixture(autouse=True)
def _fresh_db():
    utils.FACTS_DB = _DB
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
    yield


def _migrate_and_seed(rows):
    """跑正式迁移补列后播种。rows = (key, value, source, agent, user, bank)。"""
    from ducky.bank_contract import ensure_memory_banks_schema

    conn = sqlite3.connect(_DB)
    ensure_memory_banks_schema(conn)
    conn.executemany(
        "INSERT INTO facts (category, fact_key, fact_value, source, agent_id, "
        "user_id, bank_id) VALUES ('p',?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    return conn


def _visible(conn, user_id, bank_id="default"):
    from ducky.facts_recall import tenant_clause

    clause, params = tenant_clause(user_id, bank_id=bank_id, conn=conn)
    rows = conn.execute(
        "SELECT fact_value FROM facts WHERE 1=1" + clause, params
    ).fetchall()
    return {r[0] for r in rows}


# ── A：默认租户与具名域的可见边界 ──────────────────────────────────

def test_default_tenant_cannot_see_named_banks_after_migration():
    """默认租户在已迁移库上只看默认域——v19 的空片段在这儿就是泄漏。"""
    conn = _migrate_and_seed([
        ("k1", "default-row", "cli", "cli", "default", "default"),
        ("k2", "alice-default", "cli", "cli", "alice", "default"),
        ("k3", "alice-work", "cli", "cli", "alice", "work"),
        ("k4", "bob-secret", "cli", "cli", "bob", "secret"),
    ])
    from ducky.facts_recall import tenant_clause

    clause, params = tenant_clause(None, conn=conn)
    assert clause.strip(), "已迁移库上默认租户的片段不许再是空串（全库可见）"

    seen = _visible(conn, None)
    conn.close()
    assert "alice-work" not in seen and "bob-secret" not in seen, (
        f"默认租户看见了具名域: {sorted(seen)}"
    )
    # 默认域内部对默认租户保持 v19 的宽可见（域内无泄漏面）
    assert {"default-row", "alice-default"} <= seen


def test_named_bank_is_strict_no_channel_fallback():
    """具名域没有旧行要兼容：bob 的行就算 source='alice' 也不给 alice 看。"""
    conn = _migrate_and_seed([
        ("k1", "alice-work", "cli", "cli", "alice", "work"),
        ("k2", "bob-work-src-alice", "alice", "alice", "bob", "work"),
        ("k3", "unclaimed-work", "alice", "alice", "default", "work"),
        ("k4", "alice-home", "cli", "cli", "alice", "home"),
    ])
    seen = _visible(conn, "alice", bank_id="work")
    conn.close()
    assert seen == {"alice-work"}, (
        f"具名域出现渠道回落或跨域可见: {sorted(seen)}"
    )


def test_default_bank_channel_fallback_only_for_unclaimed_rows():
    """默认域内：渠道标记只认领「尚无主人」的行，已有主的行一律只认 user_id。"""
    conn = _migrate_and_seed([
        ("k1", "alice-own", "cli", "cli", "alice", "default"),
        ("k2", "unclaimed-by-channel", "alice", "alice", "default", "default"),
        # 迁移后 user_id 列 NOT NULL，未认领的另一形态是空白串
        ("k3", "unclaimed-blank", "alice", "alice", " ", "default"),
        ("k4", "bob-claimed-src-alice", "alice", "alice", "bob", "default"),
        ("k5", "alice-named-bank", "cli", "cli", "alice", "work"),
    ])
    seen = _visible(conn, "alice")
    conn.close()
    assert {"alice-own", "unclaimed-by-channel", "unclaimed-blank"} <= seen, (
        f"正规归属或未认领回落丢了: {sorted(seen)}"
    )
    assert "bob-claimed-src-alice" not in seen, (
        "渠道标记对已有主人的行生效了——这是跨租户读口子"
    )
    assert "alice-named-bank" not in seen, "默认域查询看见了具名域的行"


# ── C：未迁移库保持 v19 原形 ──────────────────────────────────────

def test_unmigrated_db_keeps_v19_shape():
    """无 bank_id 列的库：tenant_clause 传 conn 也必须退回 v19 原形。"""
    from ducky.facts_recall import tenant_clause

    conn = sqlite3.connect(_DB)  # fixture 建的表没跑迁移，无 bank_id 列
    assert tenant_clause(None, conn=conn) == ("", [])
    assert tenant_clause("alice", conn=conn) == tenant_clause("alice")
    conn.close()


# ── B：conflict_resolver 的失效写必须同一把尺子 ────────────────────

def test_conflict_resolver_channel_marker_cannot_invalidate_claimed_rows():
    """bob 已认领的行（哪怕 source='alice'）不许被 alice 的消解失效；
    未认领 + 渠道匹配的行照旧可被认领失效（v19 存量语义）。"""
    conn = _migrate_and_seed([
        ("shared_key", "alice-old", "cli", "cli", "alice", "default"),
        ("shared_key", "unclaimed-old", "alice", "alice", "default", "default"),
        ("shared_key", "bob-old", "alice", "alice", "bob", "default"),
    ])
    conn.close()

    from ducky.conflict_resolver import resolve_fact_conflict

    out = resolve_fact_conflict("p", "shared_key", "brand-new", user_id="alice")
    assert out["invalidated"] == 2, f"期望失效 2 行（自有+未认领）: {out}"

    conn = sqlite3.connect(_DB)
    alive = {
        r[0] for r in conn.execute(
            "SELECT fact_value FROM facts WHERE valid_to IS NULL"
        )
    }
    conn.close()
    assert alive == {"bob-old"}, (
        f"存活集合={sorted(alive)}：bob 已认领的行被渠道标记打穿即为跨租户写"
    )


def test_conflict_resolver_named_bank_scope_is_strict():
    """alice 在 work 域消解：她自己 home 域、bob 的 work 域一行不许动。"""
    conn = _migrate_and_seed([
        ("nb_key", "alice-work-old", "cli", "cli", "alice", "work"),
        ("nb_key", "alice-home-old", "cli", "cli", "alice", "home"),
        ("nb_key", "bob-work-old", "alice", "alice", "bob", "work"),
        ("nb_key", "unclaimed-src-alice", "alice", "alice", "default", "work"),
    ])
    conn.close()

    from ducky.conflict_resolver import resolve_fact_conflict

    out = resolve_fact_conflict(
        "p", "nb_key", "brand-new", user_id="alice", bank_id="work"
    )
    assert out["invalidated"] == 1, f"具名域只该失效 alice 自己那一行: {out}"

    conn = sqlite3.connect(_DB)
    alive = {
        r[0] for r in conn.execute(
            "SELECT fact_value FROM facts WHERE valid_to IS NULL"
        )
    }
    conn.close()
    assert alive == {"alice-home-old", "bob-work-old", "unclaimed-src-alice"}, (
        f"存活集合={sorted(alive)}"
    )


# ── 路由层：bank_id 参数接通（防「函数修了、路由没传」） ─────────────

def test_facts_route_threads_bank_id(monkeypatch):
    """GET /facts 必须把 bank_id 递进 tenant_clause——默认域请求不返具名域行。"""
    import ducky.hot.legacy_helpers as helpers
    from ducky.hot.legacy_routes import register_legacy_routes
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    conn = _migrate_and_seed([
        ("rk1", "alice-default", "cli", "cli", "alice", "default"),
        ("rk2", "alice-work", "cli", "cli", "alice", "work"),
        ("rk3", "bob-claimed-src-alice", "alice", "alice", "bob", "default"),
    ])
    conn.close()
    monkeypatch.setattr(helpers, "FACTS_DB", _DB)

    app = FastAPI()
    register_legacy_routes(app)
    client = TestClient(app)

    resp = client.get("/facts", params={"user_id": "alice"})
    assert resp.status_code == 200
    values = {r["fact_value"] for r in resp.json()["facts"]}
    assert values == {"alice-default"}, (
        f"/facts 默认域返回了越界行: {sorted(values)}"
    )

    resp = client.get("/facts", params={"user_id": "alice", "bank_id": "work"})
    assert resp.status_code == 200
    values = {r["fact_value"] for r in resp.json()["facts"]}
    assert values == {"alice-work"}, (
        f"/facts bank_id=work 未生效: {sorted(values)}"
    )
