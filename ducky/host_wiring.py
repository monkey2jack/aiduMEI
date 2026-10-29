"""宿主接线状态（f0.7 P3-4，网友反馈「装完不挂载」）。

/health 全绿不代表宿主接上了：服务好好的，宿主一句话都没往里写。这里记下
「最近一次带宿主溯源的写入」（钩子/插件写入都带 metadata._origin_agent），
/health 授权视图据此报 host_wiring；服务起来 30 分钟还一次没收到，进 warnings。

只记时间与来源名，不记内容；落盘节流 60 秒，重启后仍可读。
"""
from __future__ import annotations

import json
import os
import threading
import time

STALE_AFTER_S = 30 * 60
_FLUSH_EVERY_S = 60
_lock = threading.Lock()
_state: dict = {"last_at": None, "agents": {}, "flushed_at": 0.0}


def _path() -> str:
    from ducky.utils import DATA_DIR
    return os.path.join(DATA_DIR, "host_wiring.json")


def note(agent: str) -> None:
    """记一次宿主写入。任何异常吞掉：绝不拖累写路径。"""
    agent = (agent or "").strip()[:64]
    if not agent:
        return
    now = time.time()
    try:
        with _lock:
            _state["last_at"] = now
            _state["agents"][agent] = now
            if now - _state["flushed_at"] < _FLUSH_EVERY_S:
                return
            _state["flushed_at"] = now
            snap = {"last_at": now, "agents": dict(_state["agents"])}
        tmp = _path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f)
        os.replace(tmp, _path())
    except (OSError, TypeError, ValueError, ImportError):
        pass


def status(started_at: float, now: float | None = None) -> tuple[dict, str | None]:
    """返回 (probe, warning 或 None)。"""
    now = time.time() if now is None else now
    last, agents = _state["last_at"], dict(_state["agents"])
    if last is None:
        try:
            with open(_path(), encoding="utf-8") as f:
                d = json.load(f)
            last, agents = d.get("last_at"), d.get("agents") or {}
        except (OSError, ValueError):
            pass
    probe = {"last_host_write_at": last, "agents": sorted(agents),
             "age_s": round(now - last) if last else None}
    warn = None
    if last is None and now - started_at > STALE_AFTER_S:
        warn = ("服务已运行超过 30 分钟，却从没收到宿主钩子/插件的写入："
                "宿主多半没接上，跑 python scripts/connect_host.py")
    return probe, warn
