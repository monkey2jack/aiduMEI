"""
ducky.pipeline — 记忆处理管道
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
七层管道，按处理顺序：
  ignition    → 记忆点火（触发/粗筛）
  gate        → 相关性闸门
  workspace   → 活跃记忆工作区（L1 缓存 + SQLite）
  broadcast   → 记忆广播链
  jlens       → 可审计性增强
  persistence → 跨查询 Session 持久化
  salience    → 显著性评估
"""

from ducky.pipeline.memory_ignition import ignition_filter, ignition_boost_sort
from ducky.pipeline.memory_gate import relevance_check
from ducky.pipeline.memory_workspace import ws_status, ws_clear, ws_lookup, ws_feed_from_results
from ducky.pipeline.memory_broadcast import broadcast_chain, broadcast_expand
from ducky.pipeline.memory_jlens import collect_jlens_report
from ducky.pipeline.memory_persistence import (
    session_start, session_search, session_pin, session_unpin,
    session_report, session_end, session_list,
)
from ducky.pipeline.memory_salience import on_memory_accessed, on_memory_added

__all__ = [
    "ignition_filter", "ignition_boost_sort",
    "relevance_check",
    "ws_status", "ws_clear", "ws_lookup", "ws_feed_from_results",
    "broadcast_chain", "broadcast_expand",
    "collect_jlens_report",
    "session_start", "session_search", "session_pin", "session_unpin",
    "session_report", "session_end", "session_list",
    "on_memory_accessed", "on_memory_added",
]
