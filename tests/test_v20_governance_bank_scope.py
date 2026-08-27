"""
tests/test_v20_governance_bank_scope.py — v20 P0-2 治理管线 bank 作用域回归
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

v19 的 candidate_facts 没有作用域列：候选队列全局可见，人审凭
candidate_id 可归档任意 bank 的事实——A 库的审核能销毁 B 库的数据。

v20 修复（本文件守住的四件事）：
  ① 候选行落库时从被治理的 facts 行上盖 (scope_user_id, bank_id) 戳，
     user_id 保持「归属」（谁写入的）语义不变；
  ② v19 存量表 ALTER 迁移补作用域列，历史候选归 default 库；
  ③ list_candidates 可按 bank_id / scope_user_id 过滤（不传 = v19 全量视图）；
  ④ review_candidate 传了 bank_id 就启用越库守卫：库不符拒绝裁决、
     候选与事实分毫不动；不传保持管理员全权（存量调用零改动）。
     HTTP 层 /governance/review 只在调用方显式传 bank_id 时启用守卫
     （模型缺省值不算显式声明）。

跑法：cd <仓库根> && .venv/bin/pytest tests/test_v20_governance_bank_scope.py -v
测试全部在临时 facts.db 上跑，绝不碰生产库。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── 关键：在导入任何业务模块之前把 DB 指向临时库 ──
_tmp_dir = tempfile.mkdtemp(prefix="aidumem_v20_govscope_")
_TEST_DB = os.path.join(_tmp_dir, "facts.db")

import ducky.utils as utils  # noqa: E402

utils.FACTS_DB = _TEST_DB
utils.TEXT_FTS_DB = os.path.join(_tmp_dir, "text_fts.db")

LOCAL_AGENT = utils.DEFAULT_AGENT_ID

# 生产 facts 表最小必要结构（含 archived_at，reject 归档要用；
# 唯一约束与 user_id/bank_id 列交由联邦迁移补齐）。
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
    archived_at TIMESTAMP,
    valid_from TEXT,
    valid_to TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# v19 形状的 candidate_facts（无作用域列），迁移测试用
_V19_CANDIDATE_DDL = """
CREATE TABLE candidate_facts (
    candidate_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_id         INTEGER,
    fact_key        TEXT NOT NULL,
    category        TEXT DEFAULT 'general',
    fact_value      TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    rule_verdict    TEXT DEFAULT '',
    eval_verdict    TEXT DEFAULT '',
    eval_confidence REAL DEFAULT 0.0,
    eval_reason     TEXT DEFAULT '',
    review_reason   TEXT DEFAULT '',
    decided_at      TEXT,
    created_at      TEXT
)
"""


@pytest.fixture(scope="module", autouse=True)
def _bootstrap_db():
    utils.FACTS_DB = _TEST_DB
    conn = sqlite3.connect(_TEST_DB)
    conn.executescript(_FACTS_DDL)
    conn.commit()
    conn.close()

    from ducky.event_ledger import ensure_ledger_schema
    from ducky.federation.schema import ensure_federation_schema

    ensure_ledger_schema()
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


def _write(category: str, key: str, value: str, user: str, bank: str) -> dict:
    """经联邦写入制造一条被治理的事实（正常内容 → llm_eval 路由 → 候选入队）。"""
    from ducky.federation.writer import write_fact

    res = write_fact(category, key, value, agent_id=LOCAL_AGENT,
                     user_id=user, bank_id=bank, dedup=False)
    assert res["status"] == "ok", res
    assert res["governance"]["candidate_id"], "治理钩子未产出候选——测试前提失效"
    return res


def _cand(cid: int) -> dict:
    conn = sqlite3.connect(_TEST_DB)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM candidate_facts WHERE candidate_id=?", (cid,)
        ).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


# ═══════════════ ② v19 存量表迁移（先跑：会重建 candidate_facts） ═══════════════
def test_migration_adds_scope_columns_to_v19_table():
    from ducky.governance import ensure_governance_schema

    conn = sqlite3.connect(_TEST_DB)
    try:
        conn.execute("DROP TABLE IF EXISTS candidate_facts")
        conn.execute(_V19_CANDIDATE_DDL)
        conn.execute(
            """INSERT INTO candidate_facts (fact_key, fact_value, user_id, status)
               VALUES ('存量键', '升级前就在队列里的候选', 'legacy_writer', 'pending')"""
        )
        conn.commit()
    finally:
        conn.close()

    ensure_governance_schema()

    cols = {r[1] for r in _rows("PRAGMA table_info(candidate_facts)")}
    assert "scope_user_id" in cols, "迁移没补 scope_user_id 列"
    assert "bank_id" in cols, "迁移没补 bank_id 列"

    row = _rows(
        "SELECT scope_user_id, bank_id FROM candidate_facts WHERE fact_key='存量键'"
    )
    assert row and row[0] == ("default", "default"), \
        "v19 历史候选必须归入 default 库（与 facts 作用域回填口径一致）"

    idx = _rows(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_candidate_scope'"
    )
    assert idx, "作用域索引 idx_candidate_scope 未建"


# ═══════════════ ① 候选盖戳：跟着事实归库 ═══════════════
def test_candidate_stamped_with_fact_scope():
    res = _write("盖戳区", "甲库候选键", "用户习惯每天早上先看部署面板",
                 user="u1", bank="bank_a")
    cand = _cand(res["governance"]["candidate_id"])
    assert cand["scope_user_id"] == "u1", "候选没盖事实的 user 作用域戳"
    assert cand["bank_id"] == "bank_a", "候选没盖事实的 bank 作用域戳"
    # 归属列不许被作用域污染：user_id 仍是「谁写入的」，不是作用域
    assert cand["user_id"] != "u1", "候选 user_id 被作用域覆盖——归属语义丢失"

    # 负向对照：不传作用域的 v19 老调用 → 候选落 default 库
    res2 = _write("盖戳区", "默认域候选键", "助手喜欢在周末整理记忆库",
                  user=utils.DEFAULT_USER_ID, bank="default")
    cand2 = _cand(res2["governance"]["candidate_id"])
    # 🔴v20.0（甲9-a）：全套里唯一一条真·测试侧假红。原先断言写死字面量 "default"，
    # 可上面 :188 是以 utils.DEFAULT_USER_ID 的身份写入的 —— 产品把这个身份盖上去
    # 完全正确，改名部署上实测盖的是配置身份而不是 'default'。红的是断言把「配置
    # 身份」和「字面量 default」当成了同一个东西，不是产品盖错戳。断言就该回指测试
    # 自己传进去的那个值，否则它测的是「部署方没改过默认身份」，而不是「盖戳对不对」。
    # 下一行的 "default" 故意保留：bank 轴的 DEFAULT_BANK_ID 是不可被环境覆盖的普通
    # 常量（bank_contract.py:36），字面量在那条轴上永远不会说谎，动它只是洁癖。
    assert cand2["scope_user_id"] == utils.DEFAULT_USER_ID
    assert cand2["bank_id"] == "default"


# ═══════════════ ③ 队列查询作用域过滤 ═══════════════
def test_list_candidates_filters_by_bank():
    from ducky.governance import list_candidates

    a = _write("过滤区", "键甲", "甲库用户偏好深色主题界面",
               user="u1", bank="bank_a")["governance"]["candidate_id"]
    b = _write("过滤区", "键乙", "乙库用户偏好浅色主题界面",
               user="u1", bank="bank_b")["governance"]["candidate_id"]

    only_a = list_candidates(bank_id="bank_a")
    ids_a = {c["candidate_id"] for c in only_a}
    assert a in ids_a, "bank_a 过滤丢了本库候选"
    assert b not in ids_a, "bank_a 过滤看见了 bank_b 的候选——队列越库泄漏"
    assert all(c["bank_id"] == "bank_a" for c in only_a)

    # 负向对照：不传过滤 = v19 管理员全量视图，两库候选都在
    everything = {c["candidate_id"] for c in list_candidates(limit=500)}
    assert a in everything and b in everything, \
        "全量视图丢候选——过滤逻辑污染了默认行为"

    # scope_user_id 过滤同样生效
    only_u1_b = list_candidates(bank_id="bank_b", scope_user_id="u1")
    assert {c["candidate_id"] for c in only_u1_b} >= {b}
    assert all(c["scope_user_id"] == "u1" for c in only_u1_b)


# ═══════════════ ④ 人审越库守卫 ═══════════════
def test_review_refuses_cross_bank():
    from ducky.governance import review_candidate

    res = _write("守卫区", "越库裁决键", "甲库这条事实不该被乙库的审核归档",
                 user="u1", bank="bank_a")
    cid, fid = res["governance"]["candidate_id"], res["fact_id"]

    denied = review_candidate(cid, "reject", reason="越库尝试", bank_id="bank_b")
    assert denied["status"] == "", f"越库裁决竟然生效了: {denied}"
    assert "越库" in denied["detail"] or "其他" in denied["detail"]
    assert _cand(cid)["status"] == "pending", "被拒的裁决改动了候选状态"
    archived = _rows("SELECT archived FROM facts WHERE id=?", (fid,))
    assert archived[0][0] == 0, "越库裁决归档了另一库的事实——P0 数据破坏"

    # v20.2.4（外审 F-08）**契约收紧**：声明了作用域就得**两轴都报对**。
    # 此前只校验 bank，user 轴解包出来只拿去写账本、不参与授权 —— 于是
    # 攻击者用自己的 user_id + 受害者的 bank_id 就能归档受害者的事实。
    # 半个作用域不是作用域，所以正向对照现在必须同时报 user。
    denied_user = review_candidate(cid, "reject", reason="报对库但报错用户",
                                   user_id="someone_else", bank_id="bank_a")
    assert denied_user["status"] == "", f"user 轴没接通: {denied_user}"
    assert "其他用户" in denied_user["detail"]
    assert _cand(cid)["status"] == "pending", "被拒的裁决改动了候选状态"

    # 负向对照：两轴都报对 → 裁决正常走完，证明守卫不是无脑全拒
    ok = review_candidate(cid, "reject", reason="本库正常驳回",
                          user_id="u1", bank_id="bank_a")
    assert ok["status"] == "rejected", f"本库裁决被误拒: {ok}"
    archived = _rows("SELECT archived FROM facts WHERE id=?", (fid,))
    assert archived[0][0] == 1, "本库驳回未归档事实——守卫把正常裁决也废了"


def test_review_without_bank_keeps_admin_authority():
    """不传 bank_id = v19 管理员全权语义，存量调用零改动。"""
    from ducky.governance import APPROVED_TRUST, review_candidate

    res = _write("全权区", "管理员键", "乙库用户偏好在夜间运行整理任务",
                 user="u2", bank="bank_b")
    cid, fid = res["governance"]["candidate_id"], res["fact_id"]

    ok = review_candidate(cid, "approve", reason="管理员直批")
    assert ok["status"] == "committed", f"无 bank 的管理员裁决被拒: {ok}"
    trust = _rows("SELECT trust_score FROM facts WHERE id=?", (fid,))
    assert abs(trust[0][0] - APPROVED_TRUST) < 1e-9


# ═══════════════ ④-HTTP /governance/review 显式声明才启用守卫 ═══════════════
def test_review_endpoint_bank_guard(monkeypatch):
    """HTTP 层全链路：显式传 bank_id 启用守卫；不传时模型缺省值
    （DEFAULT_BANK_ID）不得被误当成作用域声明。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from ducky.hot.crud import register_crud_routes

    app = FastAPI()
    register_crud_routes(app)
    client = TestClient(app)

    # 乙库候选 + 显式声明甲库 → 拒
    res = _write("端点守卫区", "端点键一", "乙库事实等待人审裁决",
                 user="u1", bank="bank_b")
    cid, fid = res["governance"]["candidate_id"], res["fact_id"]
    r = client.post("/governance/review", json={
        "candidate_id": cid, "decision": "reject",
        "reason": "端点越库尝试", "bank_id": "bank_a",
    })
    assert r.status_code == 200, r.text
    assert r.json()["details"]["status"] == "", "端点显式 bank 不符仍放行——守卫没接通"
    assert _cand(cid)["status"] == "pending"
    assert _rows("SELECT archived FROM facts WHERE id=?", (fid,))[0][0] == 0

    # 同一候选、不传 bank_id → 管理员全权，正常裁决（缺省值不算声明）
    r2 = client.post("/governance/review", json={
        "candidate_id": cid, "decision": "reject", "reason": "管理员驳回",
    })
    assert r2.status_code == 200, r2.text
    assert r2.json()["details"]["status"] == "rejected", \
        "不传 bank 被误判成 default 库越权——v19 存量调用全断"

    # 负向对照：显式传对 bank → 正常裁决
    res3 = _write("端点守卫区", "端点键二", "乙库另一条事实等待人审裁决",
                  user="u1", bank="bank_b")
    cid3 = res3["governance"]["candidate_id"]
    r3 = client.post("/governance/review", json={
        "candidate_id": cid3, "decision": "approve",
        # v20.2.4 F-08：显式声明作用域时两轴都要报（user 轴此前完全没校验）
        "reason": "本库正常批准", "bank_id": "bank_b", "user_id": "u1",
    })
    assert r3.status_code == 200, r3.text
    assert r3.json()["details"]["status"] == "committed"

    # /governance/candidates 的作用域过滤参数同样接通
    r4 = client.get("/governance/candidates", params={"bank_id": "bank_b", "limit": 500})
    assert r4.status_code == 200
    got = {c["candidate_id"] for c in r4.json()["results"]}
    assert {cid, cid3} <= got
    assert all(c["bank_id"] == "bank_b" for c in r4.json()["results"])
