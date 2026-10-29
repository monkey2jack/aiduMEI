"""f0.7 P5 对抗自审回归(2026-10-29,不再请外部专家,自审报告 S-1..S-9)。先红后绿:探针在 66c8642 上逐项复现。"""
import json
import os
import subprocess
import sys

import pytest

from ducky.memory_kernel.impl_native import NativeMemory
from ducky.verbatim_vault import allocate_verbatim_claim

_PROBE = os.path.join(os.path.dirname(__file__), "_f07_selfaudit_probe.py")


@pytest.fixture(scope="module", params=["native", "mem0"])
def probe(request):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AIDUMEM_", "AIDUMEI_"))}
    p = subprocess.run([sys.executable, _PROBE, request.param], capture_output=True, text=True, timeout=300, env=env)
    assert p.returncode == 0, p.stderr[-2000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


def test_s1_retry_deletes_exactly_the_first_claim(probe):
    """S-1:原文层首轮失败、内核已删;重放用检查点里首轮认领的那批行(sX),不再退化成一行不删。"""
    assert probe["S1_first"] == "failed" and probe["S1_tomb"] == ["sX"]
    assert probe["S1_reconcile"] == [1, 0] and probe["S1_after"] == ["sY"]


def test_s2_merged_memory_takes_all_its_sessions(probe):
    """S-2:去重合并的记忆在同文共用时也带走全部出处会话(此前只认最后一次会话 sB,sA 残留)。"""
    assert probe["S2_delete"] == "committed" and probe["S2_after"] == []


def test_s4_update_keeps_other_memorys_verbatim(probe):
    """S-4:/update 与单删同一认领规则——另一条活记忆所在会话的原文不跟着删(此前按 hash 全删、无墓碑)。"""
    assert probe["S4_update"] == 200 and probe["S4_after"] == ["sY"]


def test_s5_cloud_pending_strips_only_matching_message(probe):
    """S-5:云侧 add 欠账只剔掉逐字相等的那条消息,同批其它消息的蒸馏不丢。"""
    assert probe["S5_delete"] == "committed" and probe["S5_left"] == [["S5我的车牌是浙A12345"]]


def test_s7_expired_restore_is_not_stamped(probe):
    """S-7:过期擦除的墓碑恢复返回 expired,不盖 restored_at(此前列表里显示成「已恢复」)。"""
    assert probe["S7_status"] == "expired" and probe["S7_restored_at"] is None


def test_s8_search_queries_not_persisted(probe):
    """S-8:evolve_queries 不再存问句(无盐短摘要可穷举还原)。"""
    assert probe["S8_nonempty_queries"] == 0


@pytest.mark.parametrize("rows,others,claimed", [
    ([(1, "sX"), (2, "sY")], ["sY"], [1]),                 # N3 主场景
    ([(1, "sA"), (2, "sB")], [""], [2]),                   # 别的记忆会话不明:从严留一行
    ([(1, ""), (2, "")], [""], [2]),                       # S-3:未记会话的重复行,留一行给另一条
    ([(1, "")], [""], []),                                 # 只有一行且另一条也在用:不动
    ([(1, "sA"), (2, "sB")], [], [1, 2]),                  # 没有别的记忆:全认领
    ([(1, "sA"), (2, "sA")], ["sA"], [2]),                 # 同会话两条独立记忆:各一行
    ([(1, "sA")], ["sZ"], [1]),                            # 别的记忆的会话没有原文行:不占本行
])
def test_allocate_verbatim_claim(rows, others, claimed):
    assert allocate_verbatim_claim(rows, others) == claimed


def test_s6_fresh_vec_filter_tolerates_bad_metadata(tmp_path):
    """S-6:metadata 为空串/非 JSON 时语义检索不再整租户报 malformed JSON(本轮回归)。"""
    class E:
        def embed(self, t, action=None):
            v = [0.0] * 8
            for ch in str(t):
                v[ord(ch) % 8] += 1.0
            return v
    m = NativeMemory(path=str(tmp_path / "m.db"), embedder=E())
    m._sqlite_vec = False
    m.add("Orion 星座", user_id="u")
    b = m.add("Nova 新星", user_id="u")["results"][0]["id"]
    for md in ("", "not-json", "[1,2]"):
        m.conn.execute("UPDATE memories SET metadata=? WHERE id=?", (md, b))
        m.conn.commit()
        assert m.search("Orion", filters={"user_id": "u"}, threshold=0.0)["results"]


def test_p6b_joined_turn_memory_takes_its_verbatim_turns(probe):
    """P6b(生产部署验收实测):用户句+助手句拼成一条记忆,原文逐条存,整段 hash 对不上 → 删后原文残留。
    现在认领本会话里属于这条记忆的逐条原文;同会话别的活记忆也说着的那句留着,等它也删了再走。"""
    assert probe["J_delete"] == "committed"
    assert probe["J_before"] == ["P6b再问一次自行车", "P6b我的自行车叫什么", "P6b白鹭号"]
    assert probe["J_after"] == ["P6b再问一次自行车", "P6b白鹭号"]
    assert probe["J2_delete"] == "committed" and probe["J2_after"] == []
