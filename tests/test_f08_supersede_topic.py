"""f0.8 ② principles 4/5 (still shadow): "same subject" needs topical overlap.

The shadow week paired 8/8 unrelated memories on judgement words alone (是/不是, 既然,
放行/未放行, 工具/没工具).  Generic synthetic equivalents only — no real chat text."""
import pytest

from ducky import write_policy as wp

# 8 false pairs (new, old) of the shape the shadow week flagged: they share only a judgement
# word (是/不是, 既然, 放行/未放行, 工具/没工具) and are about different things.
FALSE_PAIRS = [
    ("按时间排好的口令清单整理成表格了，不是缓存", "页面一直显示旧版本，是缓存"),
    ("既然这事是助手的活，那就先列清单", "既然这事是首页的改动，那就把间距拉大"),
    ("编辑器右边那个窗格，不是终端", "刚才那几条回复，是终端"),
    ("认不认这个称呼，没工具", "这个模型挺聪明，工具"),
    ("发消息，放行", "群历史，未放行"),
    ("能不能直接派活给它，不是", "两个代理怎么通信，是"),
    ("你太狡猾了，不是", "没法比，是"),
    ("哈哈你在调侃我，不是吧", "上次部署挺顺，是吧"),
]


@pytest.mark.parametrize("new,old", FALSE_PAIRS)
def test_trigger_words_alone_do_not_pair_memories(new, old):
    assert wp.same_topic_change(new, old) is None
    rows = [{"id": "old-1", "content": old, "metadata": {}, "created_at": "2026-11-01T00:00:00+00:00"}]
    assert wp.supersede_candidates(new, {}, rows) == []


@pytest.mark.parametrize("new,old,kind", [
    ("默认端口改成 8780 了", "默认端口是 8767", "value_change"),
    ("我现在住在东京", "我住在杭州", "value_change"),
    ("我不喜欢喝冰美式了，胃受不了", "我喜欢喝冰美式", "negation"),
    ("我的常用语言是中文", "用户的常用语言可能是英语", "value_change"),
    ("测试机的数据盘不是 /data 了", "测试机的数据盘是 /data", "negation"),
])
def test_real_changes_still_proposed(new, old, kind):
    assert wp.same_topic_change(new, old)["kind"] == kind


def test_topic_overlap_by_vector_similarity():
    new, old = FALSE_PAIRS[0]
    assert wp.topical_overlap(new, old) is False
    assert wp.topical_overlap(new, old, vec_sim=0.95) is True
    assert wp.topical_overlap(new, old, vec_sim=0.2) is False


def test_common_grams_do_not_count_as_topic():
    a, b = "阿甲乙来了", "阿甲乙走了"           # only a name in common
    assert wp.topical_overlap(a, b) is True
    assert wp.topical_overlap(a, b, common=frozenset({"阿甲", "甲乙"})) is False
