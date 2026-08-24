"""tests/test_v20_recall_strength.py — v20：召回强度必须随响应下发

用户视角审计 🟡-3 说「无相关记忆时仍返回结果（score=0.59）」。生产实测把这件事查清了，
结论和第一眼相反，所以先把测量摆出来（8 个探针，生产实例）：

    真问题  top = 0.558 / 0.560 / 0.662 / 0.651
    纯噪声  top = 0.457 / 0.423 / 0.466

**两组不重叠 —— 嵌入是有区分力的。** 我第一次测时拿「zzz9x9x9x 不存在的话题」当噪声，
它拿到 0.589，于是我一度得出「分数毫无区分力、阈值救不了」的结论并当成 🔴 报了出去。
错在探针：那句里带着「不存在的话题」四个真词，压根不是噪声。**一个选坏了的探针，
足以让人得出完全相反的结论。**

修正后的真实缺陷小一号，但仍然在：**纯噪声照样返回满额结果，且响应里没有任何
「这批很弱」的信号。** 5 条 0.42 和 5 条 0.66 在调用方看来一模一样。

整改口径：**只标注，不默认过滤。**
8 个数据点定不出一个生产阈值 —— 那正是「拍脑袋常数」。而默认过滤一旦把阈值定高，
丢掉的是真记忆，比多返回几条噪声严重得多。所以默认 `floor=0`（行为与整改前逐字节
一致），部署方显式设 `AIDUMEM_RECALL_SCORE_FLOOR` 才启用过滤。

跑法：cd <仓库根> && .venv/bin/pytest tests/test_v20_recall_strength.py -v
"""
from __future__ import annotations

import os
import sys

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import ducky.hot.search as search_mod  # noqa: E402

_ENV = "AIDUMEM_RECALL_SCORE_FLOOR"

# 生产实测值，原样搬进来当夹具 —— 断言直接踩在真实分布上，不是编出来的数
_REAL_TOPS = (0.558, 0.560, 0.662, 0.651)
_NOISE_TOPS = (0.457, 0.423, 0.466)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(_ENV, raising=False)


def _rows(*scores):
    return [{"id": f"m{i}", "score": s} for i, s in enumerate(scores)]


# ═══════════════ ① 默认不改变任何行为 ═══════════════

def test_default_floor_is_zero_and_drops_nothing():
    """★ 默认必须**一条都不丢**，`weak` 恒 False。

    这条是整改的安全边界：默认路径上的行为要与整改前逐字节一致，
    否则这次「加个探针」就变成了一次没人要求的召回策略变更。
    """
    rows = _rows(0.42, 0.61, 0.50)
    before = list(rows)
    info = search_mod.annotate_recall_strength(rows)
    assert info["floor"] == 0.0
    assert info["dropped"] == 0
    assert info["weak"] is False
    assert rows == before, "默认路径动了结果列表"
    assert info["top_score"] == 0.61


def test_empty_results_report_none_top_score_not_zero():
    """无结果时 `top_score` 必须是 None，不许是 0.0。

    0.0 会被读成「有结果，但都是 0 分」—— 那是另一件事（例如全部被时间窗口过滤掉）。
    把「没有」渲染成「有但很差」，运维会照错误的方向去查。
    """
    info = search_mod.annotate_recall_strength([], floor=0.5)
    assert info["top_score"] is None
    assert info["dropped"] == 0 and info["weak"] is False


# ═══════════════ ② 显式设了才过滤 ═══════════════

def test_explicit_floor_filters_and_counts_what_it_dropped():
    rows = _rows(0.42, 0.61, 0.50, 0.31)
    info = search_mod.annotate_recall_strength(rows, floor=0.5)
    assert [r["score"] for r in rows] == [0.61, 0.50], "过滤结果不对"
    assert info["dropped"] == 2, "丢了几条必须报出来 —— 静默丢弃就是静默失败"
    assert info["weak"] is False, "最高分 0.61 高于 0.5，不该判 weak"


def test_weak_is_flagged_when_even_the_top_hit_is_below_the_floor():
    """★ 纯噪声形态：整批都低于下限 → `weak: True`。

    用的是生产实测的噪声 top（0.457/0.423/0.466），下限取两组之间的 0.50。
    """
    for top in _NOISE_TOPS:
        rows = _rows(top, top - 0.02, top - 0.03)
        info = search_mod.annotate_recall_strength(rows, floor=0.50)
        assert info["weak"] is True, f"噪声 top={top} 没被标 weak：{info}"


def test_real_queries_survive_a_floor_between_the_two_clusters():
    """★ 负向对照（有区分力的那一半）：真问题在同一个下限下**必须活下来**。

    只测「噪声被拦住」是不够的 —— 把下限设成 0.99 也能让噪声全被拦住，
    代价是真记忆一起没了。两侧都测，这个下限才算被证明过。
    """
    for top in _REAL_TOPS:
        rows = _rows(top, top - 0.01)
        info = search_mod.annotate_recall_strength(rows, floor=0.50)
        assert info["weak"] is False, f"真问题 top={top} 被误判 weak：{info}"
        assert rows, f"真问题 top={top} 被下限清空了：{info}"


def test_the_two_measured_clusters_really_do_not_overlap():
    """★ 把「两组不重叠」这个前提本身焊进测试。

    上面两条断言的全部效力都建立在这个前提上。哪天嵌入模型或数据变了、两组开始重叠，
    这条会先红 —— 而不是让上面两条继续绿着、却已经守不住任何东西。
    """
    assert max(_NOISE_TOPS) < min(_REAL_TOPS), (
        f"实测分布已经重叠（噪声最高 {max(_NOISE_TOPS)} ≥ 真问题最低 {min(_REAL_TOPS)}）"
        " —— 单一分数阈值在这种分布下无法区分，本文件的整改口径需要重新论证"
    )


# ═══════════════ ③ 阈值配置的坏值不许静默当 0 ═══════════════

@pytest.mark.parametrize("bad", ["abc", "", "  ", "-0.5", "1.5", "0.5.5"])
def test_malformed_floor_falls_back_to_no_filtering(monkeypatch, bad):
    """坏阈值一律降级成「不过滤」，且非空的坏值要有 warning。

    「设了一个打错的阈值」和「没设」在行为上一样，但在意图上完全不同（铁律 13）。
    降级是对的（不敢拿一个看不懂的数去丢用户的记忆），但必须留痕。
    """
    monkeypatch.setenv(_ENV, bad)
    assert search_mod._score_floor() == 0.0


def test_malformed_floor_logs_a_warning(monkeypatch, caplog):
    import logging
    monkeypatch.setenv(_ENV, "zero-point-five")
    with caplog.at_level(logging.WARNING):
        assert search_mod._score_floor() == 0.0
    assert any(_ENV in r.getMessage() for r in caplog.records), (
        f"坏阈值静默当 0 —— 部署方以为过滤开着，实际没开："
        f"{[r.getMessage() for r in caplog.records]}"
    )


def test_valid_floor_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv(_ENV, "0.55")
    assert search_mod._score_floor() == 0.55


# ═══════════════ ④ 响应里必须带上这个字段 ═══════════════

def test_search_response_carries_recall_strength():
    """判据落在结构上：`/search` 的返回字典里要有 `_recall_strength`。"""
    import ast
    src = open(os.path.join(_REPO_ROOT, "ducky/hot/search.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = [k.value for k in node.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)]
            if "results" in keys and "_recall_path" in keys:
                if "_recall_strength" in keys:
                    found = True
    assert found, (
        "/search 的成功响应里没有 _recall_strength —— "
        "「5 条 0.42」和「5 条 0.66」对调用方仍然长得一模一样"
    )
