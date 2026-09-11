"""tests/test_v20_5_1_wal_replay.py — v20.5.1 T-14：WAL 崩溃/重启幂等矩阵
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
钉住应用层 WAL 的核心不变量：**同一逻辑 job 重放 N 次，最终状态 == 重放 1 次**。

状态机（ducky/wal_engine.py）：
    append(pending) → [副作用各层写入] → mark_status(committed/failed)
    崩溃发生在任何一环，重启后 reconcile_startup() 只对 pending 条目重放；
    终态（committed/failed）以追加状态行形式落账，永不回到 pending。

矩阵：
    ① append 后未 commit 崩溃 → 重启（新实例）pending 可见；
    ①b 追加写被撕断（commit 前半程崩溃的 WAL 文件形态）→ 坏行不掩盖欠账；
    ② commit 前半程崩溃（副作用只落了一部分）→ replay 收敛到完整终态；
    ③ 重复 replay 不产生重复副作用（调用计数与仓终态双重断言）；
    ④ committed/failed 终态不被 replay 复活；replay 自身失败闭合为 failed
       后同样不复活。

隔离：每条用例用 tmp_path 下独立的 WALEngine 实例（构造器接受 wal_dir），
绝不碰真实 data/；reconcile 走 get_instance 时把单例指到该实例
（tests/test_v20_delete_all_and_wal_replay.py 同款做法）。cascade 用分层
副作用替身 —— 真实 cascade 的幂等性来源正是「删除＝集合丢弃」，替身把这
一性质照实建模。

跑法：cd <仓库根> && .venv/bin/pytest tests/test_v20_5_1_wal_replay.py -v
"""
from __future__ import annotations

import pytest

import ducky.wal_engine as we


@pytest.fixture()
def wal(tmp_path):
    """独立 WAL 目录的引擎实例 —— 重启语义靠「同一目录再开一个新实例」表达。"""
    return we.WALEngine(wal_dir=str(tmp_path / "wal"))


def _reopen(wal):
    """模拟重启：同一 WAL 目录换一个全新实例（崩溃不丢已 fsync 的账）。"""
    return we.WALEngine(wal_dir=str(wal.wal_dir))


class _LayerStore:
    """多仓副作用替身：每层一个 set，cascade 从各层丢弃 memory_id（幂等）。"""

    def __init__(self):
        self.layers: dict[str, set] = {
            "mem0_vector": set(), "fts": set(), "facts": set(), "salience": set(),
        }
        self.delete_calls: list[tuple] = []
        self.delete_all_calls: list[tuple] = []

    def seed(self, memory_id, layers=None):
        for layer in (layers or self.layers):
            self.layers[layer].add(memory_id)

    def snapshot(self):
        return {k: frozenset(v) for k, v in self.layers.items()}

    # 签名逐参数对齐 ducky.wal_engine.cascade_delete_memory
    def cascade_delete_memory(self, memory_id, user_id="default", bank_id="default"):
        self.delete_calls.append((memory_id, user_id, bank_id))
        for layer in self.layers.values():
            layer.discard(memory_id)  # 幂等：已删再删是 no-op
        return {"status": "committed", "details": {}}

    # 签名逐参数对齐 ducky.wal_engine.cascade_delete_all
    def cascade_delete_all(self, user_id, confirm=False, bank_id="default"):
        self.delete_all_calls.append((user_id, confirm, bank_id))
        for layer in self.layers.values():
            layer.clear()
        return {"status": "committed", "details": {}}


@pytest.fixture()
def replay_env(wal, monkeypatch):
    """把 reconcile 的单例与 cascade 出口都指到本用例的独立世界。"""
    store = _LayerStore()
    monkeypatch.setattr(we.WALEngine, "get_instance", classmethod(lambda cls: wal))
    monkeypatch.setattr(we, "cascade_delete_memory", store.cascade_delete_memory)
    monkeypatch.setattr(we, "cascade_delete_all", store.cascade_delete_all)
    return wal, store


def _delete_entry(wal_id, mid, user_id="u1", bank_id="default"):
    return we.WALEntry(
        wal_id=wal_id, operation="delete", user_id=user_id, bank_id=bank_id,
        payload={"memory_id": mid, "user_id": user_id, "bank_id": bank_id},
        status="pending")


# ══════════════════════════════════════════════════════════
# ① append 后未 commit 崩溃 → 重启 pending 可见
# ══════════════════════════════════════════════════════════

def test_append_then_crash_pending_visible_after_restart(wal):
    wal.append(_delete_entry("wal-t14-1a", "m1"))
    # 进程在这里「死掉」：没有 mark_status，实例直接废弃
    restarted = _reopen(wal)
    pending = restarted.get_pending_entries()
    assert [e.wal_id for e in pending] == ["wal-t14-1a"], (
        "已 fsync 的 pending 条目重启后必须可见 —— 这是 WAL 存在的全部意义"
    )
    assert pending[0].payload["memory_id"] == "m1"
    assert pending[0].payload["bank_id"] == "default", "重放要用的作用域必须随条目幸存"


def test_torn_tail_line_does_not_hide_pending(wal):
    """①b：崩溃撕断正在追加的那一行 —— 坏行不许掩盖前面的欠账。

    WAL 是只追加账本：崩溃时最后一行只写出去一半是标准形态。读侧跳过
    解析不动的行（v20.4.0 Codex P2-03：compact 时坏行原样保留，绝不把
    损坏翻译成「没有待处理」）。
    """
    wal.append(_delete_entry("wal-t14-1b", "m2"))
    with open(wal.wal_file, "a", encoding="utf-8") as f:
        f.write('{"wal_id": "wal-t14-torn", "ope')  # 半个 JSON 行，无换行

    restarted = _reopen(wal)
    assert [e.wal_id for e in restarted.get_pending_entries()] == ["wal-t14-1b"]

    report = restarted.compact()
    assert report["unparsable_kept"] == 1, "坏行必须原样保留（丢弃即静默数据丢失）"
    assert [e.wal_id for e in restarted.get_pending_entries()] == ["wal-t14-1b"], (
        "compact 后欠账一条不许少"
    )


# ══════════════════════════════════════════════════════════
# ② commit 前半程崩溃（部分写入）→ replay 幂等收敛
# ══════════════════════════════════════════════════════════

def test_partial_commit_crash_replay_converges_to_single_run(replay_env):
    """原始请求在副作用落到一半时死掉（fts 层已删、其余层还在，条目仍 pending）。

    重启重放把剩下的层补齐；因为删除幂等，终态必须与「一次跑完」逐层相等。
    """
    wal, store = replay_env
    wal.append(_delete_entry("wal-t14-2a", "m1"))
    # 崩溃现场：fts 层已删除，mem0/facts/salience 层还没轮到
    store.seed("m1", layers=["mem0_vector", "facts", "salience"])
    half_crashed = store.snapshot()

    report = we.reconcile_startup()
    assert report["recovered"] == 1 and report["failed"] == 0
    assert store.delete_calls == [("m1", "u1", "default")]

    # 对照组：同一逻辑 job 一次跑完的终态
    once = _LayerStore()
    once.seed("m1")
    once.cascade_delete_memory("m1", user_id="u1", bank_id="default")
    assert store.snapshot() == once.snapshot(), (
        "半程崩溃 + 重放的终态必须 == 一次跑完；重放前的半成品: "
        f"{ {k: sorted(v) for k, v in half_crashed.items()} }"
    )

    # 对照组自身也是幂等的：再调一次终态不变（替身性质自检，防假绿）
    once.cascade_delete_memory("m1", user_id="u1", bank_id="default")
    assert store.snapshot() == once.snapshot()


# ══════════════════════════════════════════════════════════
# ③ 重复 replay 不产生重复副作用
# ══════════════════════════════════════════════════════════

def test_repeated_replay_n_times_equals_once(replay_env):
    """核心不变量：同一逻辑 job 重放 N 次，最终状态 == 重放 1 次。"""
    wal, store = replay_env
    store.seed("m1")
    store.seed("m2")
    wal.append(_delete_entry("wal-t14-3a", "m1"))
    wal.append(we.WALEntry(
        wal_id="wal-t14-3b", operation="delete_all", user_id="u9", bank_id="default",
        payload={"user_id": "u9", "bank_id": "default"}, status="pending"))

    first = we.reconcile_startup()
    state_after_first = store.snapshot()
    assert first["recovered"] == 2 and first["failed"] == 0
    # delete_all 重放必须补 confirm=True（条目存在即原调用已过闸的证明）
    assert store.delete_all_calls == [("u9", True, "default")]

    for round_no in (2, 3, 4):
        rep = we.reconcile_startup()
        assert rep["pending_count"] == 0, f"第 {round_no} 轮仍有未决 —— 无限重放形态"
        assert rep["recovered"] == 0

    assert store.delete_calls == [("m1", "u1", "default")], (
        "副作用调用计数必须 == 1，重放不许重复执行"
    )
    assert store.snapshot() == state_after_first, "重放 N 次的终态必须 == 重放 1 次"
    assert not wal.get_pending_entries(), "账本必须收敛，不许留 pending"


# ══════════════════════════════════════════════════════════
# ④ committed/failed 终态不被 replay 复活
# ══════════════════════════════════════════════════════════

def test_terminal_status_never_resurrected_by_replay(replay_env):
    wal, store = replay_env
    wal.append(_delete_entry("wal-t14-4a", "m-done"))
    wal.mark_status("wal-t14-4a", "committed")
    wal.append(_delete_entry("wal-t14-4b", "m-dead"))
    wal.mark_status("wal-t14-4b", "failed", error="上轮已判死")

    restarted = _reopen(wal)
    assert restarted.get_pending_entries() == [], (
        "终态条目（committed/failed）重启后不许回到 pending"
    )

    # compact 把终态折叠进条目行之后（时间新鲜所以保留），同样不许复活
    restarted.compact(keep_recent_seconds=86400)
    assert restarted.get_pending_entries() == []

    rep = we.reconcile_startup()
    assert rep["pending_count"] == 0 and rep["recovered"] == 0 and rep["failed"] == 0
    assert store.delete_calls == [] and store.delete_all_calls == [], (
        "终态条目一次都不许被重放"
    )


def test_replay_failure_closes_failed_and_stays_failed(replay_env, monkeypatch):
    """重放自身炸了的条目闭合为 failed（不留 pending 下轮再炸），且此后不复活。"""
    wal, store = replay_env
    wal.append(_delete_entry("wal-t14-4c", "m-boom"))

    def _boom(memory_id, user_id="default", bank_id="default"):
        raise RuntimeError("删除面炸了（模拟）")

    monkeypatch.setattr(we, "cascade_delete_memory", _boom)
    first = we.reconcile_startup()
    assert first["failed"] == 1 and first["recovered"] == 0
    assert wal.get_pending_entries() == [], "失败也要闭合 —— 留 pending 等于下次重启再炸一遍"

    # 故障修好之后重启：failed 终态不许被复活重放（与 committed 同等待遇）
    monkeypatch.setattr(we, "cascade_delete_memory", store.cascade_delete_memory)
    restarted_rep = we.reconcile_startup()
    assert restarted_rep["pending_count"] == 0 and restarted_rep["recovered"] == 0
    assert store.delete_calls == [], "failed 终态被 replay 复活了"


def test_status_flip_line_does_not_turn_back_to_pending(wal):
    """边界：同一 wal_id 的 committed 状态行之后，账本里不许有任何形态把它拉回 pending。

    mark_status 是追加写 —— 旧 pending 基行永远留在文件里，终态由状态行覆盖。
    这条用例把「覆盖语义」直接钉在原始账本上（不过 reconcile，纯引擎层）。
    """
    wal.append(_delete_entry("wal-t14-5a", "m-flip"))
    wal.mark_status("wal-t14-5a", "committed")
    # 手写一行指向同一 id 的 failed 覆盖（终态翻转同样不许回弹 pending）
    wal.append(we.WALEntry(
        wal_id="status-flip", operation="update", status="failed",
        payload={"target_wal_id": "wal-t14-5a", "updated_status": "failed"}))

    restarted = _reopen(wal)
    assert restarted.get_pending_entries() == []
    # compact 折叠后从磁盘再读一遍，语义必须不变
    restarted.compact(keep_recent_seconds=0)
    assert _reopen(wal).get_pending_entries() == []
