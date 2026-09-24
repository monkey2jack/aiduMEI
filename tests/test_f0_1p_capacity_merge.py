"""f0.1+ 容量合并误删整改守卫（外部用户实锤）。

背景：``auto_merge_similar`` 名为「合并相似记忆」，实现却一次相似度计算都没有 ——
只按 ``metadata.source`` 分组，把几百条话题各异的记忆当成同类，只留最新一条，
其余 ``memory.delete()`` 真删。用户环境单次删除 794~864 条，且因绕过 tombstone
而不可恢复。

本文件的守卫分两层：
  · 行为层：真调函数，验「内容不同不删 / 真重复才合 / 默认不删 / 留快照」；
  · 结构层：AST 判据，钉死实现里**真的调用**了相似度函数与快照函数 ——
    注释里写着函数名不算数（字符串 grep 分不清代码和注释，踩过）。
"""
from __future__ import annotations

import ast
import os

from ducky.layer1_selfcheck import (
    DEDUP_THRESHOLD,
    _cluster_by_similarity,
    _text_similarity,
    auto_merge_enabled,
)

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_REPO, "ducky", "layer1_selfcheck.py")


def _func_node(name: str) -> ast.FunctionDef:
    tree = ast.parse(open(_SRC, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} 不在 {_SRC} 里 —— 守卫失去着力点，请同步改判据")


def _called_names(node: ast.AST) -> set:
    """收集函数体内**真实发生的调用**名（属性调用取最后一段）。"""
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


# ── 结构层：AST 判据 ────────────────────────────────────────────────

def test_merge_actually_calls_a_similarity_function():
    """钉死：合并逻辑必须真的调用相似度计算。

    用 AST 而不是 grep —— 注释里出现 `_text_similarity` 字样不算数。
    """
    # 射程说明：只看 auto_merge_similar **自己**调了什么。
    # 第一版把 _cluster_by_similarity 的内部调用也并了进来 —— 于是即便
    # auto_merge_similar 退回「按 source 一刀切」、根本不调聚类，
    # 判据依旧命中（因为那个函数本身还在文件里）。判据太粗＝白护栏，已收紧。
    own = _called_names(_func_node("auto_merge_similar"))
    assert "_cluster_by_similarity" in own or "_text_similarity" in own \
        or "jaccard_sim" in own, (
        "auto_merge_similar 自己没有调用聚类或相似度函数 —— "
        "这正是用户实锤的缺陷：名为『合并相似』却只按 metadata.source 一刀切删记忆"
    )
    # 聚类函数本身也必须真比内容，不能徒有其名
    inner = _called_names(_func_node("_cluster_by_similarity"))
    assert "_text_similarity" in inner or "jaccard_sim" in inner, (
        "_cluster_by_similarity 内部没有相似度计算 —— 聚类是假的"
    )


def test_merge_does_not_call_raw_mem0_delete():
    """钉死：不许再直接调 mem0 原生 delete —— 那条路绕过 tombstone 快照。"""
    node = _func_node("auto_merge_similar")
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            if n.func.attr == "delete" and isinstance(n.func.value, ast.Name) \
                    and n.func.value.id == "memory":
                raise AssertionError(
                    "auto_merge_similar 又出现 memory.delete() —— "
                    "这条路径不写 tombstone，删了找不回来；请走 cascade_delete_memory"
                )


def test_merge_takes_a_tombstone_snapshot_before_deleting():
    """钉死：删除前必须留快照，否则自动行为删掉的东西无法恢复。"""
    names = _called_names(_func_node("auto_merge_similar"))
    assert "snapshot_before_delete" in names, (
        "删除前没有 snapshot_before_delete —— 自动合并删掉的记忆将不可恢复"
    )
    assert "cascade_delete_memory" in names, (
        "没有走 cascade_delete_memory —— 级联清理与墓碑登记都会漏"
    )


# ── 行为层 ──────────────────────────────────────────────────────────

def test_auto_merge_defaults_to_off(monkeypatch):
    """默认必须关闭：宁可库满告警，也不替用户静默删数据。"""
    monkeypatch.delenv("AIDUMEI_AUTO_MERGE", raising=False)
    assert auto_merge_enabled() is False
    # 负向对照：显式开启后确实能打开（否则上面的 False 可能是恒定值）
    monkeypatch.setenv("AIDUMEI_AUTO_MERGE", "on")
    assert auto_merge_enabled() is True
    monkeypatch.setenv("AIDUMEI_AUTO_MERGE", "off")
    assert auto_merge_enabled() is False


def test_cluster_keeps_different_topics_apart():
    """核心回归：同来源但话题不同的记忆，必须各自成簇（=一条都不会被删）。"""
    items = [
        {"id": "1", "memory": "用户喜欢喝美式咖啡，不加糖"},
        {"id": "2", "memory": "下周三要去上海出差，住浦东"},
        {"id": "3", "memory": "项目截止日期推迟到十月底了"},
    ]
    clusters = _cluster_by_similarity(items, DEDUP_THRESHOLD)
    assert len(clusters) == 3, f"话题不同却被聚成同一簇：{clusters}"
    assert all(len(c) == 1 for c in clusters)


def test_cluster_merges_true_duplicates():
    """反向：内容近乎一致的必须聚成一簇（证明判据不是恒不合并）。"""
    items = [
        {"id": "1", "memory": "今天下午三点和张总开会讨论预算问题"},
        {"id": "2", "memory": "今天下午三点和张总开会讨论预算问题。"},
        {"id": "3", "memory": "今天下午三点和张总开会讨论预算问题"},
    ]
    clusters = _cluster_by_similarity(items, DEDUP_THRESHOLD)
    assert len(clusters) == 1 and len(clusters[0]) == 3, (
        f"真重复未收敛，相似度判据失效：{clusters}"
    )


def test_cluster_never_drops_items_without_text():
    """取不到正文的条目必须单独成簇 —— 绝不能被当成谁的重复删掉。"""
    items = [
        {"id": "1", "memory": "今天下午三点和张总开会讨论预算问题"},
        {"id": "2"},                       # 无正文
        {"id": "3", "memory": ""},         # 空正文
    ]
    clusters = _cluster_by_similarity(items, DEDUP_THRESHOLD)
    assert len(clusters) == 3, f"无正文条目被并进了别人的簇：{clusters}"
    # 全部条目一个不少
    ids = {i["id"] for c in clusters for i in c}
    assert ids == {"1", "2", "3"}


def test_similarity_threshold_is_discriminating():
    """判据本身要有区分力：不同话题必须低于阈值，真重复必须高于阈值。"""
    diff = _text_similarity("用户喜欢喝美式咖啡", "下周三要去上海出差")
    same = _text_similarity("今天下午三点开会讨论预算", "今天下午三点开会讨论预算。")
    assert diff < DEDUP_THRESHOLD < same, (
        f"阈值失去区分力：不同话题={diff:.2f} 真重复={same:.2f} 阈值={DEDUP_THRESHOLD}"
    )


def test_capacity_config_rejects_non_finite(monkeypatch):
    """NaN/inf/负数必须回落默认 —— NaN 会让比较式判据恒 False，静默失效。"""
    import importlib

    import ducky.layer1_selfcheck as m

    for bad in ("nan", "inf", "-1", "0", "abc", ""):
        monkeypatch.setenv("AIDUMEI_CAPACITY_THRESHOLD", bad)
        importlib.reload(m)
        assert m.CAPACITY_THRESHOLD == 0.80, f"非法值 {bad!r} 未回落默认"
    # 负向对照：合法值必须被采纳
    monkeypatch.setenv("AIDUMEI_CAPACITY_THRESHOLD", "0.5")
    importlib.reload(m)
    assert m.CAPACITY_THRESHOLD == 0.5
    monkeypatch.delenv("AIDUMEI_CAPACITY_THRESHOLD", raising=False)
    importlib.reload(m)


def test_env_keys_are_registered():
    """三个新环境变量必须进登记册，否则用户拼错不会有任何告警。"""
    from ducky.env_registry import is_known_env_name

    for key in ("AIDUMEI_AUTO_MERGE", "AIDUMEI_MAX_CAPACITY", "AIDUMEI_CAPACITY_THRESHOLD"):
        assert is_known_env_name(key), f"{key} 未登记 —— 用户拼错不会有任何告警"
    # 负向对照：编造的名字必须判未知（否则这条断言恒真）
    assert not is_known_env_name("AIDUMEI_TOTALLY_MADE_UP_KEY_XYZ")
