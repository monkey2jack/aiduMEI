"""f0.7 定向复核回归(外审 sesame / haiku 第二轮,2026-10-28/29)。先红后绿:探针在复核前代码上逐项复现。

探针 tests/_f07_reaudit_probe.py 在独立子进程里起进程内 app(api_server 导入期读环境变量)。
"""
import json
import os
import subprocess
import sys

import pytest

from ducky.memory_kernel.impl_native import NativeMemory

_PROBE = os.path.join(os.path.dirname(__file__), "_f07_reaudit_probe.py")


def _run(kernel: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AIDUMEM_", "AIDUMEI_"))}
    p = subprocess.run([sys.executable, _PROBE, kernel], capture_output=True, text=True, timeout=300, env=env)
    assert p.returncode == 0, p.stderr[-2000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module", params=["native", "mem0"])
def probe(request):
    return request.param, _run(request.param)


def test_same_text_in_two_sessions_one_memory_all_verbatim_go(probe):
    """haiku N1:删一条记忆,它的原话(两个会话都说过、去重成一条记忆)全部删掉,全库零残留。"""
    _, out = probe
    assert out["A_verbatim_before"] == ["sA", "sB"]
    assert out["A_delete"] == "committed" and out["A_verbatim_after"] == [] and out["A_residue"] == {}


def test_independent_same_text_memory_keeps_its_verbatim(probe):
    """haiku N1+N3(native):native 档按内核正文定位原文(此前一行都删不到);同一句话另一条活记忆
    所在会话的原文不连坐(此前按 hash 全删、跨会话)。"""
    kernel, out = probe
    if kernel != "native":
        return                       # 两条同文独立记忆只在 native 内核可直接构造(不用 skip,免得新增跳过轴)
    assert out["B_verbatim_before"] == ["sX", "sY"]
    assert out["B_delete"] == "committed" and out["B_verbatim_after"] == ["sY"] and out["B_other_alive"]


def test_pending_cleanup_is_exact_not_substring(probe):
    """haiku N2:删「好的呀」不再连带删掉别的记忆的待补任务(此前 instr 子串匹配,三条全删)。"""
    _, out = probe
    assert out["C_delete"] == "committed"
    assert out["C_pending_left"] == ["add", "m-other", "m-third"]


def test_update_then_delete_leaves_no_old_text(probe):
    """sesame F-03 变体:改写前原文不留在欠账账本;evolve_queries 不再存检索原文(sesame §4.2)。"""
    _, out = probe
    assert out["D_update"] == 200 and out["D_delete"] == "committed"
    assert out["D_residue_old"] == {} and out["D_residue_new"] == {}


def test_tombstone_ttl_erases_every_snapshot_column(probe):
    """sesame N-1:墓碑到期后 content/facts/vector/layer 四列快照全擦,全库(含墓碑)不留原文。"""
    _, out = probe
    assert out["E_erase"]["erased"] > 0
    assert out["E_residue"] == {}


def test_graph_routes_enforce_scope_like_facts(probe):
    """sesame N-2:graph 族四个端点与 /api/facts 同口径,没配绑定时也拒绝跨租户读;本人照常。"""
    _, out = probe
    assert out["F_status"] == {"nebula": 403, "checkup": 403, "stats": 403, "neighbors": 403, "facts": 403}
    assert out["F_owner_ok"] == 200


def test_restore_brings_back_collateral_verbatim_copies(probe):
    """P5 自查(实验机生产副本上发现):native 删除连带清掉的原文副本,/tombstone/restore 时一并放回;
    同文独立记忆还在时,只带走被删记忆所在会话的副本。"""
    kernel, out = probe
    if kernel != "native":
        return
    assert out["H_copy_after_delete"] is False
    assert out["H_restore"] == "ok" and out["H_main_back"] and out["H_copy_back"]
    assert out["H_copies_left"] == ["verbatim:9101"]


def test_facts_single_source_by_behaviour(probe):
    """haiku N7:事实单一真源用行为验证 —— /api/facts 读得到,native 的 memory.db 不派生第二份。"""
    kernel, out = probe
    assert out["G_facts_api"] is True
    if kernel == "native":
        assert out["G_memdb_facts"] == 0


class _Emb:
    def __init__(self, fail=False):
        self.fail = fail

    def embed(self, text, action=None):
        if self.fail:
            raise RuntimeError("embedding down")
        v = [0.0] * 8
        for ch in str(text):
            v[ord(ch) % 8] += 1.0
        return v


def test_stale_vector_awaiting_reembed_is_ignored_by_semantic_leg(tmp_path):
    """haiku N4:改写时嵌入失败,旧向量暂留待补 —— 语义腿不认它,搜旧值不再命中已改成新值的记忆。"""
    m = NativeMemory(path=str(tmp_path / "m.db"), embedder=_Emb())
    a = m.add("Orion", user_id="u")["results"][0]["id"]
    m.embedding_model = _Emb(fail=True)
    m.update(a, "Nova")
    assert m.get(a)["metadata"].get("_embed_pending") is True
    m.embedding_model = _Emb()
    hits = m.search("Orion", filters={"user_id": "u"}, threshold=0.0)["results"]
    assert [h["id"] for h in hits] == []                    # FTS 正文已是 Nova,旧向量不再把它带出来
    assert [h["id"] for h in m.search("Nova", filters={"user_id": "u"}, threshold=0.0)["results"]] == [a]


def test_every_native_memory_table_has_a_delete_chain_decision(tmp_path):
    """sesame F-05 第 2 点:memory.db 的表也自动枚举 —— 新增一张表没在删除链矩阵裁决就红。"""
    from ducky.wal_engine import DELETE_CHAIN_MATRIX, NATIVE_MEMORY_TABLES
    m = NativeMemory(path=str(tmp_path / "m.db"), embedder=None)
    tables = {r[0] for r in m.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
              if not r[0].startswith("sqlite_") and not r[0].endswith(("_config", "_data", "_docsize", "_idx"))}
    assert tables - set(NATIVE_MEMORY_TABLES) == set(), "native memory.db 新表未裁决"
    assert {v for v in NATIVE_MEMORY_TABLES.values()} <= set(DELETE_CHAIN_MATRIX)
