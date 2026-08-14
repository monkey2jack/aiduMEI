"""ducky.degradation — 系统降级追踪器（反静默降级核心基础设施）

记录并追踪系统中发生的所有降级事件（如 Reranker 异常、FTS 分词降级、LLM 超时规则降级），
并在 /health 端点透明暴露，告别「永远假装 200 OK，背后故障全靠猜」的静默降级设计债。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("aiduMEM.degradation")


class DegradationTracker:
    _lock = threading.Lock()
    _degraded_map: Dict[str, Dict[str, Any]] = {}

    @classmethod
    def record_degradation(cls, component: str, reason: str, *, severity: str = "warning") -> None:
        """记录一个组件的降级状态。"""
        with cls._lock:
            cls._degraded_map[component] = {
                "component": component,
                "reason": str(reason)[:200],
                "severity": severity,
                "timestamp": time.time(),
                "time_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            }
        logger.warning("⚠️ [Degradation] 组件 '%s' 发生降级: %s", component, reason)

    @classmethod
    def clear_degradation(cls, component: str) -> None:
        """清除组件的降级状态（自愈后调用）。"""
        with cls._lock:
            cls._degraded_map.pop(component, None)

    @classmethod
    def get_degraded_summary(cls) -> List[str]:
        """获取当前处于降级状态的组件名称列表。"""
        with cls._lock:
            # 5分钟内无新降级的瞬态事件可视为自愈
            now = time.time()
            active = [
                comp for comp, info in cls._degraded_map.items()
                if (now - info["timestamp"]) < 300
            ]
            return active

    @classmethod
    def get_degraded_details(cls) -> List[Dict[str, Any]]:
        """获取当前降级详细记录。"""
        with cls._lock:
            now = time.time()
            return [
                info for comp, info in cls._degraded_map.items()
                if (now - info["timestamp"]) < 300
            ]
