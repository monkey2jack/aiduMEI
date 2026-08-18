"""
ducky.memory_salience — 兼容门面（v11.1 重构 · v19.4.1 补齐）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

真源：
    · 写入/访问钩子 → ducky.pipeline.memory_salience
    · 衰减/统计/冲突/指标/审计/教训闭环 → ducky.salience 包

为什么需要补齐（v19.4.1 实机发现的静默故障）：
    v11.1 重构把显著性能力拆进 ducky.salience 包，本门面却只转发了两个
    写入钩子。scripts/consolidator.py 一直按老接口
    `from ducky.memory_salience import decay_all, get_stats, ...` 导入 ——
    于是它自 2026-07-26 起每天凌晨 2:30 被 cron 拉起、每次都在
    第 23 行 ImportError 退出，日志里累积了 18 次同样的堆栈。

    **整整三周没有人发现**：衰减没跑、每日指标没记、冲突没检测、
    技能结晶没触发、教训闭环没验证。服务本身 /health 全绿，
    因为这些活儿本就不在服务进程里 —— 它们在一个安静死掉的 cron 里。

    这正是「静默失败比报错危险」的教科书案例：
    有日志、有退出码，但没有任何人被通知。

    修复方式是补门面而不是改 consolidator 的 import：
    保持向后兼容，任何按老接口写的外部脚本都不再踩坑。
"""
from ducky.pipeline.memory_salience import (  # noqa: F401
    on_memory_accessed,
    on_memory_added,
)
from ducky.salience import (  # noqa: F401
    audit_health_anomalies,
    decay_all,
    detect_conflicts,
    get_historical_metrics,
    get_stats,
    record_daily_metrics,
    resolve_conflict_salience,
    verify_lessons_closed,
)

__all__ = [
    "on_memory_accessed",
    "on_memory_added",
    "audit_health_anomalies",
    "decay_all",
    "detect_conflicts",
    "get_historical_metrics",
    "get_stats",
    "record_daily_metrics",
    "resolve_conflict_salience",
    "verify_lessons_closed",
]
