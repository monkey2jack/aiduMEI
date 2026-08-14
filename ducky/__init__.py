"""
ducky — aiduMEM 思想引擎智能模块包
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
v19.2.0 Athena (雅典娜) — Production Hardened AI Memory & Wisdom Engine
"""

from .layer1_selfcheck import (
    layer1_add_wrapper, check_capacity, dedup_check, auto_merge_similar,
)
from .recall_funnel import funnel_search
from .hybrid_recall import hybrid_search
from .instinct_graduation import auto_graduate, scan_instincts

# v8 新增 — J-space 五脉
from .memory_ignition import (
    ignition_filter, ignition_boost_sort,
)
from .memory_workspace import (
    ws_lookup, ws_push, ws_feed_from_results, ws_status, ws_clear,
)
from .memory_broadcast import (
    broadcast_chain, broadcast_expand,
)
from .memory_jlens import (
    collect_jlens_report, enhance_funnel_trace,
)
from .memory_persistence import (
    session_start, session_search, session_pin, session_unpin,
    session_report, session_end, session_list,
)

# v8 重构 — 统一工具函数
from .utils import (
    quick_sim, tokenize, jaccard_sim,
    normalize_score, parse_iso_timestamp,
)

__all__ = [
    # v7
    "layer1_add_wrapper", "check_capacity", "dedup_check", "auto_merge_similar",
    "funnel_search", "hybrid_search",
    "auto_graduate", "scan_instincts",
    # v8 — Ignition
    "ignition_filter", "ignition_boost_sort",
    # v8 — Workspace
    "ws_lookup", "ws_push", "ws_feed_from_results", "ws_status", "ws_clear",
    # v8 — Broadcast
    "broadcast_chain", "broadcast_expand",
    # v8 — J-lens
    "collect_jlens_report", "enhance_funnel_trace",
    # v8 — Persistence
    "session_start", "session_search", "session_pin", "session_unpin",
    "session_report", "session_end", "session_list",
    # v8 — Utils
    "quick_sim", "tokenize", "jaccard_sim",
    "normalize_score", "parse_iso_timestamp",
]
