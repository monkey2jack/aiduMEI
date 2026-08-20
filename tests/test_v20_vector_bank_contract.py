"""v20 向量侧域契约：默认域不下推 + 结果复筛 + 写入盖戳。

为什么需要这一组用例（不是补形式覆盖率）：
    v20 在五个向量读取点加了 ``filters={"user_id":…, "bank_id":…}``，但写入侧
    从未把 bank_id 盖进 mem0 metadata。而 Qdrant 的 payload 过滤是 **must**
    语义 —— payload 里**没有** bank_id 这个字段的点，会被 ``bank_id=?`` 条件
    直接判为不匹配。后果不是「命名域搜不到」，而是**所有域、所有租户的向量
    召回全部归零**，且 mem0 只返回空列表、不抛异常，日志一行不留。

    ``test_qdrant_must_filter_excludes_missing_field`` 就是把这条 Qdrant 语义
    钉死在测试里 —— 它是整套设计的地基，哪天 qdrant-client 换了语义，这条会
    先红，而不是等生产上记忆凭空消失。
"""
from __future__ import annotations

import pytest

from ducky.bank_contract import (
    DEFAULT_BANK_ID,
    stamp_bank_metadata,
    vector_item_bank,
    vector_item_in_bank,
    vector_scope_filters,
)


# ══════════════════════════════════════════════════════════════════
# 一、地基：Qdrant must 语义（缺字段 = 不匹配）
# ══════════════════════════════════════════════════════════════════

def test_qdrant_must_filter_excludes_missing_field():
    """缺字段的点会被 must 条件滤掉 —— v20 向量归零的根因，实测钉死。"""
    qdrant_client = pytest.importorskip("qdrant_client")
    from qdrant_client.models import (
        Distance, FieldCondition, Filter, MatchValue, PointStruct, VectorParams,
    )

    client = qdrant_client.QdrantClient(":memory:")
    client.create_collection(
        "t", vectors_config=VectorParams(size=4, distance=Distance.COSINE)
    )
    client.upsert("t", points=[
        # 存量点（v19 写入）：payload 里压根没有 bank_id 这个字段
        PointStruct(id=1, vector=[0.1, 0.2, 0.3, 0.4], payload={"user_id": "u"}),
        # v20 新写入点：带 bank_id
        PointStruct(id=2, vector=[0.1, 0.2, 0.3, 0.4],
                    payload={"user_id": "u", "bank_id": "default"}),
    ])

    def _ids(conditions):
        hits = client.query_points(
            "t", query=[0.1, 0.2, 0.3, 0.4],
            query_filter=Filter(must=conditions), limit=10,
        ).points
        return sorted(p.id for p in hits)

    only_user = [FieldCondition(key="user_id", match=MatchValue(value="u"))]
    with_bank = only_user + [
        FieldCondition(key="bank_id", match=MatchValue(value="default"))
    ]

    assert _ids(only_user) == [1, 2], "只按 user_id 过滤应召回两条"
    # ↓ 这一行就是灾难本身：加上 bank_id=default，存量点 1 消失了
    assert _ids(with_bank) == [2], (
        "缺 bank_id 字段的存量点被 must 条件滤掉 —— "
        "所以默认域绝不能下推 bank_id"
    )


# ══════════════════════════════════════════════════════════════════
# 二、过滤下推：默认域保持 v19 裸过滤，命名域才下推
# ══════════════════════════════════════════════════════════════════

def test_scope_filters_default_bank_omits_bank_id():
    """默认域**不能**下推 bank_id，否则全部存量向量当场清零。"""
    assert vector_scope_filters("alice", DEFAULT_BANK_ID) == {"user_id": "alice"}
    assert vector_scope_filters("alice", None) == {"user_id": "alice"}
    assert vector_scope_filters("alice", "") == {"user_id": "alice"}


def test_scope_filters_named_bank_pushes_bank_id():
    """命名域下推 bank_id —— 新写入的点都带这个字段，能被精确命中。"""
    assert vector_scope_filters("alice", "work") == {
        "user_id": "alice", "bank_id": "work",
    }


def test_scope_filters_normalizes_user_and_bank():
    got = vector_scope_filters("  alice  ", "  Work  ")
    assert got["user_id"] == "alice"
    assert got["bank_id"] == "Work"


# ══════════════════════════════════════════════════════════════════
# 三、结果复筛：缺字段一律算默认域（存量语义）
# ══════════════════════════════════════════════════════════════════

def test_item_bank_missing_field_is_default():
    """v19 存量点没有 bank_id，它们**就是**默认域的数据，不是「未知」。"""
    assert vector_item_bank({"id": "m1", "memory": "x"}) == DEFAULT_BANK_ID
    assert vector_item_bank({"id": "m1", "bank_id": None}) == DEFAULT_BANK_ID
    assert vector_item_bank({"id": "m1", "bank_id": ""}) == DEFAULT_BANK_ID
    assert vector_item_bank(None) == DEFAULT_BANK_ID
    assert vector_item_bank("not a dict") == DEFAULT_BANK_ID


def test_item_bank_reads_toplevel_then_metadata_then_payload():
    assert vector_item_bank({"bank_id": "work"}) == "work"
    assert vector_item_bank({"metadata": {"bank_id": "work"}}) == "work"
    assert vector_item_bank({"payload": {"bank_id": "work"}}) == "work"
    # 顶层优先
    assert vector_item_bank(
        {"bank_id": "top", "metadata": {"bank_id": "nested"}}
    ) == "top"


def test_item_in_bank_legacy_visible_in_default_hidden_from_named():
    """一条存量向量：默认域看得见，命名域看不见。两个方向都要成立。"""
    legacy = {"id": "old", "memory": "v19 存量记忆"}
    assert vector_item_in_bank(legacy, DEFAULT_BANK_ID) is True
    assert vector_item_in_bank(legacy, None) is True
    assert vector_item_in_bank(legacy, "work") is False


def test_item_in_bank_named_point_does_not_leak_into_default():
    """命名域的点不能漏进默认域结果 —— 默认域没下推，全靠这层复筛。"""
    work_item = {"id": "w1", "metadata": {"bank_id": "work"}}
    assert vector_item_in_bank(work_item, "work") is True
    assert vector_item_in_bank(work_item, DEFAULT_BANK_ID) is False


# ══════════════════════════════════════════════════════════════════
# 四、写入盖戳：metadata 是 bank_id 进向量 payload 的唯一通道
# ══════════════════════════════════════════════════════════════════

def test_stamp_bank_metadata_stamps_and_does_not_mutate_caller():
    original = {"source": "chat"}
    got = stamp_bank_metadata(original, "work")
    assert got == {"source": "chat", "bank_id": "work"}
    assert original == {"source": "chat"}, "不得改调用方的原对象"


def test_stamp_bank_metadata_handles_none_and_default():
    assert stamp_bank_metadata(None, None) == {"bank_id": DEFAULT_BANK_ID}
    assert stamp_bank_metadata({}, "")["bank_id"] == DEFAULT_BANK_ID


def test_stamp_bank_metadata_overwrites_stale_bank():
    """调用方带进来的旧 bank_id 必须以本次入参为准，不能残留。"""
    got = stamp_bank_metadata({"bank_id": "home"}, "work")
    assert got["bank_id"] == "work"


# ══════════════════════════════════════════════════════════════════
# 五、端到端：两半合起来才等价于 FTS 侧的严格隔离
# ══════════════════════════════════════════════════════════════════

class _FakeMemory:
    """按 Qdrant must 语义模拟 mem0.search：缺字段的点被 bank_id 条件滤掉。"""

    def __init__(self, points):
        self.points = points
        self.last_filters = None

    def _match(self, point, filters):
        for key, want in (filters or {}).items():
            md = point.get("metadata") or {}
            have = point.get(key, md.get(key))
            if have != want:          # 缺字段 → have is None → 不匹配
                return False
        return True

    def search(self, query, filters=None, **kw):
        self.last_filters = filters
        return {"results": [p for p in self.points if self._match(p, filters)]}


def _points():
    return [
        {"id": "legacy", "memory": "v19 存量", "score": 0.9,
         "user_id": "alice"},
        {"id": "dflt", "memory": "v20 默认域", "score": 0.8,
         "user_id": "alice", "metadata": {"bank_id": "default"}},
        {"id": "work", "memory": "v20 work 域", "score": 0.7,
         "user_id": "alice", "metadata": {"bank_id": "work"}},
    ]


def _recall(bank_id):
    """复现真实读取点的两步：下推过滤 → Python 复筛。"""
    mem = _FakeMemory(_points())
    raw = mem.search("q", filters=vector_scope_filters("alice", bank_id))
    kept = [c for c in raw["results"] if vector_item_in_bank(c, bank_id)]
    return mem.last_filters, [c["id"] for c in kept]


def test_default_bank_recall_keeps_legacy_and_excludes_named():
    filters, ids = _recall(DEFAULT_BANK_ID)
    assert "bank_id" not in filters, "默认域不许下推 bank_id"
    assert ids == ["legacy", "dflt"], "存量可见，work 域的点被复筛剔除"


def test_named_bank_recall_returns_only_that_bank():
    filters, ids = _recall("work")
    assert filters["bank_id"] == "work"
    assert ids == ["work"]


def test_negative_control_pushing_bank_id_on_default_zeroes_legacy():
    """负向对照：把 v20 原来那种「默认域也下推」的写法跑一遍，存量必然消失。

    这条用例存在的意义是证明上面那条 assert 不是恒真 —— 换回旧写法就红。
    """
    mem = _FakeMemory(_points())
    naive = {"user_id": "alice", "bank_id": DEFAULT_BANK_ID}   # v20 原始写法
    ids = [c["id"] for c in mem.search("q", filters=naive)["results"]]
    assert "legacy" not in ids, "旧写法下存量点被滤掉 —— 这正是要修的灾难"
    assert ids == ["dflt"]
