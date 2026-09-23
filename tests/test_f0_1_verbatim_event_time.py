"""f0.1 · verbatim 事件时间根因回归守卫。

钉死 2026-09-22 LoCoMo 试跑暴露的 P0：原文库把「事情发生的时间」
写成了「记录存进来的时间」，且不报错不告警（假绿灯）。

三条线各有守卫，且每条都配负向对照——没有负向对照的守卫，
在实现退化时可能仍然全绿（见 feedback_guard_failure_modes）。
"""
from __future__ import annotations

from ducky.verbatim_vault import _normalize_ts

EVENT_TS = "1:56 pm on 8 May, 2023"

def test_f01_event_time_beats_now():
    """写线①：metadata 给了事件时间时，绝不能回落 now()。"""
    got = _normalize_ts(None, fallback=EVENT_TS)
    assert got == EVENT_TS, f"事件时间被丢弃，退回了 {got!r}"
    # 负向对照：不给 fallback 时确实会回落 now()，证明上面那条不是永真
    assert _normalize_ts(None) != EVENT_TS

def test_f01_per_turn_timestamp_wins():
    """写线②：逐条 message.timestamp 比批次级 metadata 更精确，优先级更高。"""
    per_turn = "2023-05-08T13:56:00"
    assert _normalize_ts(per_turn, fallback=EVENT_TS) == per_turn

def test_f01_falls_back_to_now_only_when_nothing_given():
    """写线③：两者都缺才用 now()——兜底仍在，没有被改没。"""
    got = _normalize_ts(None, fallback=None)
    assert got and got != EVENT_TS
    assert got.startswith("20")

def test_f01_build_context_reads_top_level_recorded_at():
    """读线：verbatim 条目没有 metadata，时间戳在**顶层** recorded_at。

    漏读它，时序题的证据就会「无时间戳」进上下文——2026-09-22 实测 39%。
    """
    from benchmarks.locomo_official import build_context

    verbatim_item = {  # 逐字复刻 verbatim_search 的返回形状
        "memory": "我昨天去了互助小组。",
        "recorded_at": EVENT_TS,
        "_verbatim": True,
    }
    ctx = build_context([verbatim_item])
    assert ctx.startswith(f"{EVENT_TS}: "), f"顶层时间戳被漏读: {ctx!r}"

    # 负向对照：真没有时间戳时不硬造（官方口径「不硬造」）
    assert build_context([{"memory": "无时间戳的一句话。"}]) == "无时间戳的一句话。"

def test_f01_time_decay_survives_non_iso_event_time():
    """防回归：事件时间可为任意格式，时间衰减不许因此静默归零。

    recorded_at 改后承载调用方原样的事件时间（LoCoMo 是
    "1:56 pm on 8 May, 2023" 这种非 ISO 串），fromisoformat 解析必失败。
    created_at 恒 ISO 且在 extract_timestamp 的 key 顺序中更靠前，作兜底。
    """
    from ducky.scoring import extract_timestamp

    with_backup = {
        "recorded_at": EVENT_TS,             # 非 ISO 事件时间
        "created_at": "2026-09-23 00:23:33",  # ISO 入库时间
        "_verbatim": True,
    }
    assert extract_timestamp(with_backup) > 0, "补了 created_at 仍取不到时间，兜底失效"

    # 负向对照：不补 created_at 时确实归零——证明这条兜底不是白护栏
    assert extract_timestamp({"recorded_at": EVENT_TS, "_verbatim": True}) == 0.0
