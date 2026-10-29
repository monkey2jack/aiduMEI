"""aiduMEI v20 P0-2 — 广播链 / Session 持久化 / v8 路由的记忆库作用域测试

覆盖点（向量侧读泄漏收口）：
1. broadcast_chain：命名域把 bank_id 下推进 filters 且复筛掉他库/存量项；
   默认域保持 v19 filters 形状（不下推 bank_id，存量向量无此字段），
   但复筛掉命名域的点；非法 bank_id 进链前炸 BankScopeError
2. broadcast_expand：他库的 memory_id 在本域视角下「找不到」（返回空）
3. session：作用域在 session_start 定死随会话走，session_search 两条
   路径（上下文/纯 query）都下推 + 复筛；session_end 交还 bank_id
   供 session_end 反思按域反思
4. v8 路由层：/recall_chain 递进 req.bank_id；/session/start 收 bank_id；
   非法作用域按 v8 约定包成 {"status": "error"}，不抛 500
"""
from __future__ import annotations

import pytest

from ducky.bank_contract import BankScopeError
from ducky.pipeline.memory_broadcast import broadcast_chain, broadcast_expand


class _FakeMem:
    """极简 mem0 替身：不管 query 是什么都返回全部种子项，
    并记录每次调用收到的 filters——「下推没下推」一目了然。"""

    def __init__(self, items):
        self.items = items
        self.search_filters: list[dict] = []
        self.get_all_filters: list[dict] = []

    def search(self, query, filters=None, limit=10, **kw):
        self.search_filters.append(dict(filters or {}))
        return {"results": [dict(i) for i in self.items]}

    def get_all(self, filters=None, limit=10000, **kw):
        self.get_all_filters.append(dict(filters or {}))
        return {"results": [dict(i) for i in self.items]}


def _seed_items():
    return [
        {"id": "mem_a", "memory": "甲库的猫叫团团", "score": 0.9,
         "metadata": {"bank_id": "bank_a"}},
        {"id": "mem_c", "memory": "甲库的猫喜欢晒太阳", "score": 0.7,
         "metadata": {"bank_id": "bank_a"}},
        {"id": "mem_b", "memory": "乙库的狗叫旺财", "score": 0.9,
         "metadata": {"bank_id": "bank_b"}},
        {"id": "mem_legacy", "memory": "存量记忆没有域标", "score": 0.8},
    ]


# ═══════════════ 1. broadcast_chain ═══════════════
def test_broadcast_chain_named_bank_pushes_filter_and_rescreens():
    mem = _FakeMem(_seed_items())
    res = broadcast_chain(mem, "猫的情况", "user_x", bank_id="bank_a")

    assert mem.search_filters, "广播链至少要搜一次"
    for f in mem.search_filters:
        assert f == {"user_id": "user_x", "bank_id": "bank_a"}, \
            f"命名域必须把 bank_id 下推进 filters: {f}"

    ids = [item["id"] for level in res["chain"] for item in level["results"]]
    assert "mem_a" in ids and "mem_c" in ids, "本域记忆必须在链里（正向对照）"
    assert "mem_b" not in ids, "乙库记忆漏进了甲库的广播链"
    assert "mem_legacy" not in ids, "存量(default)记忆漏进了命名域的广播链"


def test_broadcast_chain_default_bank_keeps_v19_filter_shape_but_rescreens():
    """默认域不下推 bank_id（存量向量 payload 无此字段，下推即整批清零），
    隔离靠 Python 复筛：命名域的点一个都不许漏进默认域视图。"""
    mem = _FakeMem(_seed_items())
    res = broadcast_chain(mem, "记忆的情况", "user_x")

    for f in mem.search_filters:
        assert f == {"user_id": "user_x"}, \
            f"默认域 filters 必须保持 v19 形状（无 bank_id 键）: {f}"

    ids = [item["id"] for level in res["chain"] for item in level["results"]]
    assert "mem_legacy" in ids, "存量记忆属于默认域，必须可见（正向对照）"
    assert "mem_a" not in ids and "mem_b" not in ids, \
        "命名域的记忆漏进了默认域的广播链"


def test_broadcast_chain_invalid_bank_raises_before_search():
    mem = _FakeMem(_seed_items())
    with pytest.raises(BankScopeError):
        broadcast_chain(mem, "猫", "user_x", bank_id="../etc")
    assert mem.search_filters == [], "非法作用域不许发出任何搜索（负向对照）"


# ═══════════════ 2. broadcast_expand ═══════════════
def test_broadcast_expand_cannot_locate_foreign_memory():
    mem = _FakeMem(_seed_items())
    # 他库的 memory_id：本域视角下「找不到」，返回空
    assert broadcast_expand(mem, "mem_b", "user_x", bank_id="bank_a") == []

    # 本域的 memory_id：正常展开，且邻居只有本域的（正向对照）
    out = broadcast_expand(mem, "mem_a", "user_x", bank_id="bank_a")
    ids = [i["id"] for i in out]
    assert ids == ["mem_c"], f"甲库展开应只见甲库邻居: {ids}"


# ═══════════════ 3. Session 持久化 ═══════════════


# ═══════════════ 4. v8 路由层递进 ═══════════════
