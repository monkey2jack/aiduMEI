"""v20 写入链上三条跨域泄漏 —— 都是 codex 那版遗漏的，都不抛异常。

v20 给写入链加了 ``bank_id`` 形参，但链上三个「按 user_id 取数」的老函数没跟着
改。它们一个比一个重：

┌───────────────────────┬──────────────────────────────────────────────────┐
│ speed/cache.cache_key │ 键里没有 bank_id → 同一句话写第二个域时**直接命中  │
│                       │ 上一个域的缓存返回**：这条记忆一个字都没写进去，   │
│                       │ 接口回 ok，返回体里还带着**另一个域**的 memory_id │
├───────────────────────┼──────────────────────────────────────────────────┤
│ dedup_check           │ 跨域判重 → 调用方 update 掉**别的域**那条记忆：    │
│                       │ 正文被改写、bank_id 被改盖，源域少一条目标域多一条 │
├───────────────────────┼──────────────────────────────────────────────────┤
│ auto_merge_similar    │ 跨域分组后 ``memory.delete()`` **真删**：往 home   │
│                       │ 域写一条，能把 work 域的旧记忆永久删掉             │
└───────────────────────┴──────────────────────────────────────────────────┘

三条的共同点是**不报错**：没有异常、没有告警日志、HTTP 全是 200。所以只能靠
用例把它们钉住。每条都同时断言两个方向：同域该命中的必须命中（否则等于把功能
关掉），跨域不该碰的一根汗毛都不许碰。
"""
from __future__ import annotations

import pytest

from ducky.bank_contract import DEFAULT_BANK_ID
from ducky.layer1_selfcheck import auto_merge_similar, check_capacity, dedup_check
from ducky.speed.cache import cache_key


@pytest.fixture(autouse=True)
def _clear_extract_cache():
    from ducky.speed.cache import _extract_cache
    _extract_cache.clear()
    yield
    _extract_cache.clear()


# ══════════════════════════════════════════════════════════════════
# 一、抽取缓存：bank_id 必须进键
# ══════════════════════════════════════════════════════════════════

def test_cache_key_differs_across_banks():
    """同用户同文本、不同域 —— 必须是两个键。"""
    k_work = cache_key("alice", "季度目标是跑分", "infer", bank_id="work")
    k_home = cache_key("alice", "季度目标是跑分", "infer", bank_id="home")
    assert k_work != k_home, (
        "两个域共用一个缓存键 —— 第二个域的写入会被缓存直接短路掉"
    )


def test_cache_key_default_bank_aliases_are_one_key():
    """None / "" / "default" 是同一个域，不能因为写法不同就各占一个键。"""
    base = cache_key("alice", "x", "infer", bank_id=DEFAULT_BANK_ID)
    assert cache_key("alice", "x", "infer") == base
    assert cache_key("alice", "x", "infer", bank_id=None) == base
    assert cache_key("alice", "x", "infer", bank_id="") == base


def test_cache_key_still_separates_users_and_modes():
    """加了域之后，原有的 user/mode 维度不能被挤掉。"""
    base = cache_key("alice", "x", "infer", bank_id="work")
    assert cache_key("bob", "x", "infer", bank_id="work") != base
    assert cache_key("alice", "x", "search", bank_id="work") != base
    assert cache_key("alice", "y", "infer", bank_id="work") != base


def test_second_bank_write_is_not_swallowed_by_cache(monkeypatch):
    """端到端：同一句话依次写两个域，两次都必须真的落库。

    修复前第二次会命中第一次的缓存 → mem0 一次 add 都收不到，接口却回 ok，
    且返回体里带的是 work 域的 memory_id（跨域信息泄漏）。
    """
    import ducky.mem0_runtime as rt
    import ducky.speed.pipeline as sp

    class _Mem:
        def __init__(self):
            self.adds: list[dict] = []

        def search(self, q, filters=None, **kw):
            return {"results": []}

        def get_all(self, filters=None, **kw):
            return {"results": []}

        def add(self, msgs, user_id=None, metadata=None, **kw):
            self.adds.append(dict(metadata or {}))
            return {"results": [{"id": f"m{len(self.adds)}",
                                 "memory": "季度目标是跑分", "event": "ADD"}]}

    monkeypatch.setattr(sp, "load_speed_cfg", lambda: {})
    monkeypatch.setattr(sp, "try_fastpath_text", lambda t: None)
    monkeypatch.setattr(rt, "register_salience_for_add", lambda *a, **k: None)

    mem = _Mem()
    msg = [{"role": "user", "content": "季度目标是跑分"}]
    sp.run_add_pipeline(mem, msg, "alice", {}, bank_id="work")
    out = sp.run_add_pipeline(mem, msg, "alice", {}, bank_id="home")

    assert len(mem.adds) == 2, "第二个域的写入被缓存吞了 —— 记忆凭空消失"
    assert [m.get("bank_id") for m in mem.adds] == ["work", "home"]
    assert not out["details"].get("cache_hit"), "跨域不该命中缓存"
    assert [m["id"] for m in out["memories"]] == ["m2"], (
        "返回体里带的是另一个域的 memory_id —— 跨域信息泄漏"
    )


def test_same_bank_repeat_still_hits_cache(monkeypatch):
    """反向对照：同一个域重复写，缓存必须照常命中，不能把加速能力改没了。"""
    import ducky.mem0_runtime as rt
    import ducky.speed.pipeline as sp

    class _Mem:
        def __init__(self):
            self.adds = 0

        def search(self, q, filters=None, **kw):
            return {"results": []}

        def get_all(self, filters=None, **kw):
            return {"results": []}

        def add(self, msgs, user_id=None, metadata=None, **kw):
            self.adds += 1
            return {"results": [{"id": "m1", "memory": "x", "event": "ADD"}]}

    monkeypatch.setattr(sp, "load_speed_cfg", lambda: {})
    monkeypatch.setattr(sp, "try_fastpath_text", lambda t: None)
    monkeypatch.setattr(rt, "register_salience_for_add", lambda *a, **k: None)

    mem = _Mem()
    msg = [{"role": "user", "content": "季度目标是跑分"}]
    sp.run_add_pipeline(mem, msg, "alice", {}, bank_id="work")
    out = sp.run_add_pipeline(mem, msg, "alice", {}, bank_id="work")

    assert mem.adds == 1, "同域重复写没走缓存 —— 白烧一次 LLM"
    assert out["details"].get("cache_hit") is True


# ══════════════════════════════════════════════════════════════════
# 二、dedup_check：跨域不得判重
# ══════════════════════════════════════════════════════════════════

class _DedupMemory:
    """库里只有一条记忆，域可配；按 filters 做 must 过滤。"""

    def __init__(self, candidate_bank: str | None):
        self.candidate_bank = candidate_bank
        self.filters = None

    def search(self, query, filters=None, **kw):
        self.filters = filters
        item = {"id": "old-1", "memory": query, "score": 0.99, "user_id": "alice"}
        if self.candidate_bank:
            item["metadata"] = {"bank_id": self.candidate_bank}
        md = item.get("metadata") or {}
        if not all(item.get(k, md.get(k)) == v for k, v in (filters or {}).items()):
            return {"results": []}
        return {"results": [item]}


def test_dedup_same_bank_still_hits():
    """同域重复必须照常判重 —— 否则等于把去重功能关掉。"""
    mem = _DedupMemory(candidate_bank="work")
    assert dedup_check(mem, "alice", "季度目标是跑分", bank_id="work") == "old-1"


def test_dedup_cross_bank_does_not_hit():
    """work 域的旧记忆，不得被 home 域的写入判成重复。

    判重了，调用方就会 ``update`` 掉它：work 域凭空少一条。
    """
    mem = _DedupMemory(candidate_bank="work")
    assert dedup_check(mem, "alice", "季度目标是跑分", bank_id="home") is None


def test_dedup_legacy_row_visible_in_default_bank():
    """v19 存量（无 bank_id）在默认域里必须还能判重，不能因为 v20 就漏重。"""
    mem = _DedupMemory(candidate_bank=None)
    assert dedup_check(mem, "alice", "季度目标是跑分") == "old-1"


def test_dedup_legacy_row_not_stolen_by_named_bank():
    """反过来：命名域的写入不得把存量记忆认领走。"""
    mem = _DedupMemory(candidate_bank=None)
    assert dedup_check(mem, "alice", "季度目标是跑分", bank_id="work") is None


def test_dedup_default_bank_does_not_push_bank_id():
    """默认域下推 bank_id 会被 Qdrant must 语义滤掉所有存量点 → 去重恒不命中。"""
    mem = _DedupMemory(candidate_bank=None)
    dedup_check(mem, "alice", "季度目标是跑分")
    assert "bank_id" not in (mem.filters or {})


# ══════════════════════════════════════════════════════════════════
# 三、auto_merge_similar / check_capacity：删除必须锁在本域
# ══════════════════════════════════════════════════════════════════

class _MergeMemory:
    """work 域 3 条 + home 域 2 条 + v19 存量 1 条，source 全是 chat。"""

    def __init__(self):
        self.deleted: list[str] = []

    @staticmethod
    def _points():
        pts = [
            {"id": f"work-{i}", "memory": f"work{i}", "created_at": f"2025-01-0{i}",
             "user_id": "alice", "metadata": {"source": "chat", "bank_id": "work"}}
            for i in range(1, 4)
        ]
        pts += [
            {"id": f"home-{i}", "memory": f"home{i}", "created_at": f"2025-02-0{i}",
             "user_id": "alice", "metadata": {"source": "chat", "bank_id": "home"}}
            for i in range(1, 3)
        ]
        pts.append(
            {"id": "legacy-1", "memory": "v19 存量", "created_at": "2024-01-01",
             "user_id": "alice", "metadata": {"source": "chat"}}
        )
        return pts

    def get_all(self, filters=None, limit=None, **kw):
        out = []
        for p in self._points():
            md = p.get("metadata") or {}
            if all(p.get(k, md.get(k)) == v for k, v in (filters or {}).items()):
                out.append(dict(p))
        return {"results": out}

    def delete(self, memory_id):
        self.deleted.append(memory_id)


def test_merge_never_deletes_other_banks():
    """往 home 域触发合并，一条 work 域记忆都不许删。

    这是本文件里唯一一条**真删数据**的路径 —— 越界一次就是永久性丢失。
    """
    mem = _MergeMemory()
    auto_merge_similar(mem, "alice", bank_id="home")
    assert not [d for d in mem.deleted if d.startswith("work")], (
        f"跨域删除发生了：{mem.deleted}"
    )


def test_merge_still_works_inside_its_own_bank():
    """反向对照：本域内 3 条同 source，仍要留最新删其余，功能不能被改没。"""
    mem = _MergeMemory()
    out = auto_merge_similar(mem, "alice", bank_id="work")
    assert out["deleted"] == 2
    assert set(mem.deleted) == {"work-1", "work-2"}, "应保留最新的 work-3"


def test_merge_in_default_bank_only_touches_legacy_and_default():
    """默认域只有 1 条存量，不够 MERGE_MIN_GROUP，不该删任何东西。"""
    mem = _MergeMemory()
    out = auto_merge_similar(mem, "alice", bank_id=DEFAULT_BANK_ID)
    assert out["deleted"] == 0
    assert mem.deleted == []


def test_capacity_counts_per_bank():
    mem = _MergeMemory()
    assert check_capacity(mem, "alice", bank_id="work")["total"] == 3
    assert check_capacity(mem, "alice", bank_id="home")["total"] == 2


def test_capacity_default_bank_sees_legacy_rows():
    """存量点没有 bank_id 字段，它们属于默认域 —— 不能数成 0。"""
    mem = _MergeMemory()
    assert check_capacity(mem, "alice", bank_id=DEFAULT_BANK_ID)["total"] == 1


def test_capacity_default_bank_does_not_push_bank_id():
    mem = _MergeMemory()
    captured = {}
    orig = mem.get_all

    def _spy(filters=None, **kw):
        captured["filters"] = filters
        return orig(filters=filters, **kw)

    mem.get_all = _spy
    check_capacity(mem, "alice")
    assert "bank_id" not in (captured.get("filters") or {})
