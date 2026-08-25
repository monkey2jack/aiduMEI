"""tests/test_v20_1_delete_chain_closure.py — v20.1 整改轮 R-01 删除链收口

外审共识的最重整改：删除链此前漏掉 workspace 缓存（z P1-01：已删内容以
found/workspace_hit 复活）、core_memory 正本（w P0 / 自报 4.1：删除后仍被
inject_context 注入）、refined_memories 与墓碑全文快照、治理候选全文。

四层断言：
1. **覆盖矩阵元守卫**：实建全部 schema 后枚举 facts.db 的 sqlite_master，
   每张表必须在 DELETE_CHAIN_MATRIX 有显式裁决 —— 新账本出现而矩阵沉默
   立刻红。这是「漏一张账本不会红」这个方法论洞的补法（z 的矩阵方法论）。
2. **删后不复活探针**：workspace 先证明能命中（错的路走得通），删除后
   同查询必须 None —— 区分力成立的不复活。
3. **组合拳断言（w 的回填复活链）**：delete_all 之后跑 backfill --apply，
   不得复活任何已删租户数据 —— 单点各自全对、组合即病，验收必须测组合。
4. **跨租户负向**：删甲不动乙，全账本逐一核对。
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ducky.core_memory as cm  # noqa: E402
import ducky.refine_memory as refine_memory  # noqa: E402
from ducky.wal_engine import DELETE_CHAIN_MATRIX, cascade_delete_all, cascade_delete_memory  # noqa: E402


class _RecordingVectorStore:
    def __init__(self):
        self.points: dict = {}

    def insert(self, vectors, payloads=None, ids=None):
        for i, pid in enumerate(ids or []):
            self.points[pid] = (payloads or [{}])[i]

    def get(self, vector_id):
        return self.points.get(vector_id)


class _FakeMemory:
    def __init__(self):
        self.embedding_model = type(
            "E", (), {"embed": staticmethod(lambda text, action: [0.1, 0.2])})()
        self.vector_store = _RecordingVectorStore()

    def get_all(self, *a, **kw):
        return {"results": []}

    def search(self, *a, **kw):
        return {"results": []}


@pytest.fixture()
def sandbox(monkeypatch, tmp_path):
    """facts 库钉到用例专属临时文件 + mem0 换录音替身 + 全 schema 就绪。"""
    import ducky.mem0_runtime as runtime
    import ducky.utils as utils

    data_dir = os.environ.get("AIDUMEM_DATA_DIR", "")
    assert "aidumei_test_data_" in data_dir, "测试没跑在沙箱 DATA_DIR 里，立刻停"

    db_path = str(tmp_path / "facts.db")
    monkeypatch.setattr(utils, "FACTS_DB", db_path)
    monkeypatch.setattr(refine_memory, "_checked", False)
    monkeypatch.setattr(cm, "_initialized", False)
    cm._initialized_scopes.clear()

    fake = _FakeMemory()
    monkeypatch.setattr(runtime, "get_memory", lambda: fake)

    from ducky.schema_bootstrap import ensure_core_schema
    ensure_core_schema(force=True)
    refine_memory.ensure_refine_schema()
    from ducky.governance import ensure_governance_schema
    ensure_governance_schema()
    from ducky.tombstone import ensure_tombstone_schema
    ensure_tombstone_schema()
    from ducky.event_ledger import ensure_ledger_schema
    ensure_ledger_schema()
    from ducky.utils import ensure_evolution_tables
    ensure_evolution_tables()
    import ducky.checkpoint as checkpoint
    monkeypatch.setattr(checkpoint, "_table_checked", False)
    checkpoint._ensure_table()
    cm._ensure_table()  # core_memory 表由本模块自管，schema_bootstrap 不建它

    def query(sql: str, params=()):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    return query, fake, db_path


def _seed_tenant(user: str, fake):
    """给租户播一套全账本数据：core 块 + facts + refined + workspace + 墓碑 + 候选。"""
    from ducky.federation.writer import write_fact
    from ducky.memory_workspace import ws_push
    from ducky.utils import get_facts_conn

    cm.put_block("core_current_project", f"{user} 正在推进的项目内容一二三",
                 user_id=user)
    for i in range(4):
        write_fact("dc_cat", f"{user}_键{i}", f"{user}_取值{i}·编号{100 + i}",
                   source=user, user_id=user, dedup=False)
    res = refine_memory.refine_group(user, "dc_cat", use_llm=False)
    assert res["status"] == "ok", res
    ws_push(user_id=user, memory_id=f"{user}_m1",
            text=f"{user} 的工作区缓存正文", score=0.9)
    conn = get_facts_conn()
    try:
        conn.execute(
            "INSERT INTO tombstones (target_id, user_id, bank_id, content_snapshot) "
            "VALUES (?,?,?,?)",
            (f"{user}_dead1", user, "default", f"{user} 已删内容的全文快照"))
        conn.execute(
            "INSERT INTO candidate_facts (fact_key, fact_value, user_id, scope_user_id, bank_id) "
            "VALUES (?,?,?,?,?)",
            (f"{user}_cand", f"{user} 被拒的候选全文", user, user, "default"))
        conn.commit()
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════
# 1. 覆盖矩阵元守卫
# ══════════════════════════════════════════════════════════════════

def test_every_facts_table_has_an_explicit_matrix_verdict(sandbox):
    """新账本出现而矩阵未裁决 → 本条红。漏一张表不会红，正是外审揪出
    两张漏网表的方法论根因 —— 矩阵让沉默结构性不可能。"""
    query, _, _ = sandbox
    tables = {r["name"] for r in query(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'")}
    undecided = tables - set(DELETE_CHAIN_MATRIX)
    assert not undecided, (
        f"以下账本在删除链覆盖矩阵中没有显式裁决：{sorted(undecided)} —— "
        "请在 DELETE_CHAIN_MATRIX 里给出 clean/exempt 判决与理由，不得沉默"
    )


def test_matrix_verdicts_are_well_formed():
    for name, verdict in DELETE_CHAIN_MATRIX.items():
        assert verdict[0] in ("clean", "exempt"), f"{name} 裁决非法: {verdict[0]}"
        assert len(str(verdict[1])) >= 8, f"{name} 的裁决理由太短，不构成显式裁决"


def test_checkpoints_exemption_is_explicit_not_silent():
    """checkpoints 的豁免是显式裁决（会话轴遗产子系统），矩阵必须写明。"""
    action, reason = DELETE_CHAIN_MATRIX["checkpoints"]
    assert action == "exempt" and "会话轴" in reason


# ══════════════════════════════════════════════════════════════════
# 2. 删后全账本清空 + 不复活探针
# ══════════════════════════════════════════════════════════════════

def test_delete_all_clears_every_scoped_ledger(sandbox):
    query, fake, _ = sandbox
    _seed_tenant("dc_alice", fake)

    out = cascade_delete_all("dc_alice")
    det = out["details"]
    assert det.get("core_memory_deleted", 0) >= 1, "core_memory 正本没清（w P0/自报 4.1）"
    assert det.get("refined_deleted", 0) >= 1, "refined_memories 没清（w P0）"
    assert det.get("workspace_cleared", 0) >= 1, "workspace 没清（z P1-01）"
    assert det.get("tombstones_deleted", 0) >= 1, "墓碑全文快照没清"
    assert det.get("governance_candidates_deleted", 0) >= 1, "治理候选全文没清"

    for sql, label in (
        ("SELECT 1 FROM core_memory WHERE user_id='dc_alice'", "core_memory"),
        ("SELECT 1 FROM refined_memories WHERE user_id='dc_alice'", "refined_memories"),
        ("SELECT 1 FROM facts WHERE user_id='dc_alice'", "facts"),
        ("SELECT 1 FROM tombstones WHERE user_id='dc_alice'", "tombstones"),
        ("SELECT 1 FROM candidate_facts WHERE scope_user_id='dc_alice'", "candidate_facts"),
    ):
        assert query(sql) == [], f"{label} 有残留 —— 账本级清空失败"


def test_deleted_content_cannot_resurrect_via_workspace(sandbox):
    """不复活探针（z P1-01 的靶心）。先证明缓存**能**命中（错的路走得通），
    删除后同查询必须 None —— 没有第一步，第二步的绿毫无区分力。"""
    query, fake, _ = sandbox
    from ducky.memory_workspace import ws_lookup, ws_push

    text = "dc_ghost 的机密项目备忘正文内容"
    ws_push(user_id="dc_ghost", memory_id="g1", text=text, score=0.9)
    pre = ws_lookup("dc_ghost", text)
    assert pre, "前置失败：缓存本该命中（同文查询 Jaccard=1）—— 探针失去区分力"

    cascade_delete_all("dc_ghost")
    post = ws_lookup("dc_ghost", text)
    assert not post, "已删内容从 workspace 复活 —— found/workspace_hit 幽灵（z P1-01）"
    assert query("SELECT 1 FROM core_memory WHERE user_id='dc_ghost'") == []


def test_single_delete_evicts_workspace_entry(sandbox):
    """/delete 单条链的配套：只清全域不清单条，同一条还能搜出来。"""
    _, fake, _ = sandbox
    from ducky.memory_workspace import ws_lookup, ws_push

    ws_push(user_id="dc_one", memory_id="m_dead", text="将被单删的正文甲", score=0.9)
    ws_push(user_id="dc_one", memory_id="m_live", text="继续存活的正文乙", score=0.9)

    out = cascade_delete_memory("m_dead", user_id="dc_one")
    assert out["details"].get("workspace_evicted") is True

    assert not ws_lookup("dc_one", "将被单删的正文甲"), "单删后缓存仍命中"
    assert ws_lookup("dc_one", "继续存活的正文乙"), "误伤了同租户的其他缓存条目"


# ══════════════════════════════════════════════════════════════════
# 3. 组合拳：删除 × 回填 不得复活（w 的回填复活链）
# ══════════════════════════════════════════════════════════════════

def test_backfill_after_delete_all_does_not_resurrect(sandbox):
    query, fake, _ = sandbox
    _seed_tenant("dc_refill", fake)
    fake.vector_store.points.clear()  # 只观察回填产物

    cascade_delete_all("dc_refill")
    report = cm.backfill_core_vectors(apply=True)  # 全作用域回填

    resurrected = [t for t in report.get("indexed", [])
                   if t.startswith("dc_refill/")]
    assert resurrected == [], (
        f"回填把已删租户写回了向量库：{resurrected} —— 删除被后台动作静默撤销（w 回填复活链）"
    )
    ghost_points = [pid for pid, payload in fake.vector_store.points.items()
                    if payload.get("user_id") == "dc_refill"]
    assert ghost_points == [], "向量库出现已删租户的点位"


# ══════════════════════════════════════════════════════════════════
# 4. 跨租户负向：删甲不动乙
# ══════════════════════════════════════════════════════════════════

def test_cross_tenant_negative_control(sandbox):
    query, fake, _ = sandbox
    from ducky.memory_workspace import ws_lookup

    _seed_tenant("dc_victim", fake)
    _seed_tenant("dc_bystander", fake)

    cascade_delete_all("dc_victim")

    assert query("SELECT 1 FROM core_memory WHERE user_id='dc_bystander'"), \
        "旁观者的 core_memory 被连坐"
    assert query("SELECT 1 FROM refined_memories WHERE user_id='dc_bystander'"), \
        "旁观者的 refined_memories 被连坐"
    assert query("SELECT 1 FROM tombstones WHERE user_id='dc_bystander'"), \
        "旁观者的墓碑被连坐"
    assert ws_lookup("dc_bystander", "dc_bystander 的工作区缓存正文"), \
        "旁观者的 workspace 被连坐"
