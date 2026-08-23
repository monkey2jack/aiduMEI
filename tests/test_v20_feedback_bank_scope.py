"""aiduMEI v20 P0-2 — /facts/feedback 的 opt-in 作用域护栏测试

覆盖点：
1. 不传作用域 = v19 管理员语义零改动（全库可按 id 反馈）
2. 传了作用域：本域事实可反馈；他库声称同一 fact_id 被拒（404），
   且信任分/计数毫发无损（负向对照）；404 文案与「不存在」同款，
   不泄露他库 fact 的存在性
3. 护栏先于 L0 豁免：错库调用连铁律事实都探不到
4. 非法 bank_id → 400，不许静默放行
5. 改名默认身份（如 alice）与字面量 'default' 折叠同域（v19.4.2 教训）
6. 老库（无作用域列）整表视作 default 域，v19 行为不炸
7. 路由层 /facts/feedback 把 user_id/bank_id 查询参数递进 impl
   （防「函数修了、路由没传」）
"""
from __future__ import annotations

import os
import tempfile

import pytest
from fastapi import HTTPException

import ducky.utils as utils

_TMPDIR = tempfile.mkdtemp(prefix="aidumem_v20_feedbackscope_")
_DB = os.path.join(_TMPDIR, "facts.db")
utils.FACTS_DB = _DB

import ducky.hot.legacy_helpers as helpers  # noqa: E402
from ducky.hot.legacy_helpers import _fact_feedback_impl  # noqa: E402

# v20 形状：facts 已完成 bank 迁移（有作用域列）
_FACTS_DDL_V20 = """
CREATE TABLE facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL DEFAULT 'general',
    fact_key TEXT NOT NULL,
    fact_value TEXT NOT NULL,
    trust_score REAL DEFAULT 0.5,
    helpful_count INTEGER DEFAULT 0,
    unhelpful_count INTEGER DEFAULT 0,
    archived INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    user_id TEXT NOT NULL DEFAULT 'default',
    bank_id TEXT NOT NULL DEFAULT 'default'
)
"""

# v19 形状：没有作用域列
_FACTS_DDL_V19 = """
CREATE TABLE facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category TEXT NOT NULL DEFAULT 'general',
    fact_key TEXT NOT NULL,
    fact_value TEXT NOT NULL,
    trust_score REAL DEFAULT 0.5,
    helpful_count INTEGER DEFAULT 0,
    unhelpful_count INTEGER DEFAULT 0,
    archived INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
)
"""


def _conn():
    return utils.get_facts_conn()


def _seed(user_id="default", bank_id="default", category="general",
          trust=0.5) -> int:
    conn = _conn()
    cur = conn.execute(
        "INSERT INTO facts (category, fact_key, fact_value, trust_score, "
        "user_id, bank_id) VALUES (?, 'k', 'v', ?, ?, ?)",
        (category, trust, user_id, bank_id),
    )
    conn.commit()
    return cur.lastrowid


def _fact_state(fact_id: int):
    row = _conn().execute(
        "SELECT trust_score, helpful_count, unhelpful_count FROM facts WHERE id=?",
        (fact_id,),
    ).fetchone()
    return (row["trust_score"], row["helpful_count"], row["unhelpful_count"])


@pytest.fixture(autouse=True)
def _fresh_facts(monkeypatch):
    monkeypatch.setattr(utils, "FACTS_DB", _DB)
    monkeypatch.setattr(helpers, "FACTS_DB", _DB)
    conn = _conn()
    conn.execute("DROP TABLE IF EXISTS facts")
    conn.execute(_FACTS_DDL_V20)
    conn.commit()
    yield


def test_feedback_unscoped_keeps_v19_admin_behavior():
    """不传作用域 = v19 管理员语义：具名域的事实也可以按 id 反馈。"""
    fid = _seed(user_id="user_x", bank_id="bank_a")
    res = _fact_feedback_impl(fid, True)
    assert res["status"] == "ok"
    assert res["trust_after"] == pytest.approx(0.6)
    assert _fact_state(fid) == (pytest.approx(0.6), 1, 0)


def test_feedback_scoped_own_bank_succeeds_wrong_scope_refused():
    fid = _seed(user_id="user_x", bank_id="bank_a")

    # 本域反馈：放行
    res = _fact_feedback_impl(fid, True, user_id="user_x", bank_id="bank_a")
    assert res["status"] == "ok"
    assert _fact_state(fid) == (pytest.approx(0.6), 1, 0)

    # 错库声称：404，且与「不存在」同文案（不泄露存在性）
    with pytest.raises(HTTPException) as exc:
        _fact_feedback_impl(fid, False, user_id="user_x", bank_id="bank_b")
    assert exc.value.status_code == 404
    assert "不存在" in exc.value.detail

    # 错用户声称：同样 404
    with pytest.raises(HTTPException) as exc:
        _fact_feedback_impl(fid, False, user_id="user_y", bank_id="bank_a")
    assert exc.value.status_code == 404

    # 负向对照：两次被拒后信任分/计数毫发无损
    assert _fact_state(fid) == (pytest.approx(0.6), 1, 0)


def test_feedback_scope_guard_precedes_l0_exemption():
    """错库调用连铁律事实都探不到——护栏先于 L0 noop，否则可用 noop
    响应差异枚举他库 L0 事实的存在性。"""
    fid = _seed(user_id="user_x", bank_id="bank_a", category="铁律")
    with pytest.raises(HTTPException) as exc:
        _fact_feedback_impl(fid, True, user_id="user_x", bank_id="bank_b")
    assert exc.value.status_code == 404

    # 本域打铁律：仍是 v19 的 noop 豁免
    res = _fact_feedback_impl(fid, True, user_id="user_x", bank_id="bank_a")
    assert res.get("noop") is True
    assert _fact_state(fid) == (pytest.approx(0.5), 0, 0)


def test_feedback_invalid_bank_id_rejected_400():
    fid = _seed(user_id="user_x", bank_id="bank_a")
    with pytest.raises(HTTPException) as exc:
        _fact_feedback_impl(fid, True, user_id="user_x", bank_id="../etc")
    assert exc.value.status_code == 400
    assert _fact_state(fid) == (pytest.approx(0.5), 0, 0)


def test_feedback_renamed_default_identity_collapses(monkeypatch):
    """部署方把默认身份改名（如 alice）后，存量行仍是字面量 'default'——
    scoped 调用传 alice 必须能命中，其他用户不许蹭折叠（负向对照）。"""
    monkeypatch.setattr(helpers, "DEFAULT_USER_ID", "alice")
    fid = _seed(user_id="default", bank_id="default")

    res = _fact_feedback_impl(fid, True, user_id="alice", bank_id="default")
    assert res["status"] == "ok"
    assert _fact_state(fid) == (pytest.approx(0.6), 1, 0)

    with pytest.raises(HTTPException) as exc:
        _fact_feedback_impl(fid, True, user_id="someone_else", bank_id="default")
    assert exc.value.status_code == 404


def test_feedback_v19_facts_without_scope_columns_falls_back_default():
    """老库没作用域列：整表视作 default 域。裸调/默认域 scoped 调用照常，
    具名域声称一律 404。"""
    conn = _conn()
    conn.execute("DROP TABLE IF EXISTS facts")
    conn.execute(_FACTS_DDL_V19)
    conn.execute("INSERT INTO facts (fact_key, fact_value) VALUES ('老键', '老值')")
    conn.commit()
    fid = conn.execute("SELECT id FROM facts").fetchone()["id"]

    res = _fact_feedback_impl(fid, True)
    assert res["status"] == "ok"
    assert res["trust_after"] == pytest.approx(0.6)

    res = _fact_feedback_impl(fid, True, user_id="default", bank_id="default")
    assert res["status"] == "ok"

    with pytest.raises(HTTPException) as exc:
        _fact_feedback_impl(fid, True, user_id="user_x", bank_id="bank_a")
    assert exc.value.status_code == 404


def test_feedback_route_threads_scope_params(monkeypatch):
    """POST /facts/feedback 必须把 user_id/bank_id 查询参数递进 impl。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from ducky.hot.legacy_routes import register_legacy_routes

    fid = _seed(user_id="user_x", bank_id="bank_a")

    app = FastAPI()
    register_legacy_routes(app)
    client = TestClient(app)

    resp = client.post("/facts/feedback",
                       params={"fact_id": fid, "helpful": "true",
                               "user_id": "user_x", "bank_id": "bank_b"})
    assert resp.status_code == 404, "错库声称必须被路由层递进护栏拒绝"
    assert _fact_state(fid) == (pytest.approx(0.5), 0, 0)

    resp = client.post("/facts/feedback",
                       params={"fact_id": fid, "helpful": "true",
                               "user_id": "user_x", "bank_id": "bank_a"})
    assert resp.status_code == 200
    assert resp.json()["trust_after"] == pytest.approx(0.6)
