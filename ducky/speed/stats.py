"""aiduMEM speed · 潮浪命中统计（Mnemosyne）"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from ducky.utils import DATA_DIR as _DATA_DIR

logger = logging.getLogger("aiduMEM.speed")

_STATS_PATH = os.path.join(_DATA_DIR, "coalesce_stats.json")
_STATS_MAX_LAST = 30
_stats_lock = threading.Lock()
_stats_cache: Optional[dict] = None


def _day_key(ts: Optional[float] = None) -> str:
    t = ts if ts is not None else time.time()
    return datetime.fromtimestamp(t, tz=timezone.utc).astimezone().strftime("%Y-%m-%d")


def _empty_bucket() -> dict:
    return {
        "waves": 0,
        "messages": 0,
        "saved_llm": 0,
        "enqueued": 0,
        "by_reason": {},
    }


def _empty_stats() -> dict:
    now = time.time()
    return {
        "since": now,
        "updated_at": now,
        "total": _empty_bucket(),
        "by_profile": {},
        "by_day": {},
        "last_waves": [],
    }


def _load_stats_unlocked() -> dict:
    global _stats_cache
    if _stats_cache is not None:
        return _stats_cache
    try:
        if os.path.isfile(_STATS_PATH):
            with open(_STATS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and "total" in data:
                _stats_cache = data
                return _stats_cache
    except Exception as e:
        logger.debug(f"coalesce stats load skip: {e}")
    _stats_cache = _empty_stats()
    return _stats_cache


def _save_stats_unlocked(data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_STATS_PATH), exist_ok=True)
        tmp = _STATS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _STATS_PATH)
    except Exception as e:
        logger.warning(f"coalesce stats save failed: {e}")


def _bump_bucket(bucket: dict, *, waves: int = 0, messages: int = 0, saved: int = 0, enqueued: int = 0, reason: str = "") -> None:
    bucket["waves"] = int(bucket.get("waves") or 0) + waves
    bucket["messages"] = int(bucket.get("messages") or 0) + messages
    bucket["saved_llm"] = int(bucket.get("saved_llm") or 0) + saved
    bucket["enqueued"] = int(bucket.get("enqueued") or 0) + enqueued
    if reason and waves:
        br = bucket.setdefault("by_reason", {})
        br[reason] = int(br.get(reason) or 0) + waves


def record_coalesce_enqueue(profile: str = "default") -> None:
    """短句入合并缓冲时 +1（用于算命中率）。"""
    with _stats_lock:
        st = _load_stats_unlocked()
        now = time.time()
        st["updated_at"] = now
        day = _day_key(now)
        _bump_bucket(st["total"], enqueued=1)
        prof = st.setdefault("by_profile", {}).setdefault(profile or "default", _empty_bucket())
        _bump_bucket(prof, enqueued=1)
        day_b = st.setdefault("by_day", {}).setdefault(day, _empty_bucket())
        _bump_bucket(day_b, enqueued=1)
        day_p = day_b.setdefault("by_profile", {}).setdefault(profile or "default", _empty_bucket())
        _bump_bucket(day_p, enqueued=1)
        # 只保留最近 14 天
        days = sorted(st.get("by_day") or {})
        if len(days) > 14:
            for d in days[:-14]:
                st["by_day"].pop(d, None)
        _save_stats_unlocked(st)


def record_coalesce_wave(
    *,
    profile: str = "default",
    count: int = 1,
    reason: str = "idle",
    key: str = "",
    user_id: str = "",
) -> dict:
    """
    每次潮浪冲刷记一笔：
      waves += 1
      messages += count
      saved_llm += max(0, count - 1)   # 本可 N 次 LLM，实际 1 次
    """
    count = max(1, int(count or 1))
    saved = max(0, count - 1)
    prof = (profile or "default").strip() or "default"
    reason = (reason or "unknown").strip() or "unknown"
    with _stats_lock:
        st = _load_stats_unlocked()
        now = time.time()
        st["updated_at"] = now
        day = _day_key(now)
        _bump_bucket(st["total"], waves=1, messages=count, saved=saved, reason=reason)
        p_b = st.setdefault("by_profile", {}).setdefault(prof, _empty_bucket())
        _bump_bucket(p_b, waves=1, messages=count, saved=saved, reason=reason)
        day_b = st.setdefault("by_day", {}).setdefault(day, _empty_bucket())
        _bump_bucket(day_b, waves=1, messages=count, saved=saved, reason=reason)
        day_p = day_b.setdefault("by_profile", {}).setdefault(prof, _empty_bucket())
        _bump_bucket(day_p, waves=1, messages=count, saved=saved, reason=reason)
        wave = {
            "ts": now,
            "day": day,
            "profile": prof,
            "count": count,
            "saved_llm": saved,
            "reason": reason,
            "key": (key or "")[:80],
            "user_id": (user_id or "")[:40],
        }
        last = list(st.get("last_waves") or [])
        last.append(wave)
        st["last_waves"] = last[-_STATS_MAX_LAST:]
        days = sorted(st.get("by_day") or {})
        if len(days) > 14:
            for d in days[:-14]:
                st["by_day"].pop(d, None)
        _save_stats_unlocked(st)
        return wave


def coalesce_stats_snapshot(reset: bool = False) -> dict:
    """运维：命中统计快照；reset=True 清零（保留 since 新起点）。"""
    with _stats_lock:
        if reset:
            st = _empty_stats()
            _stats_cache = st
            _save_stats_unlocked(st)
        else:
            st = _load_stats_unlocked()
        # 浅拷贝，避免调用方改内存
        out = json.loads(json.dumps(st, ensure_ascii=False))
    total = out.get("total") or {}
    waves = int(total.get("waves") or 0)
    msgs = int(total.get("messages") or 0)
    saved = int(total.get("saved_llm") or 0)
    enq = int(total.get("enqueued") or 0)
    out["summary"] = {
        "waves": waves,
        "messages": msgs,
        "saved_llm": saved,
        "enqueued": enq,
        # 合并率：被并进潮浪的消息 / 入队消息
        "merge_rate": round((msgs / enq), 3) if enq > 0 else None,
        # 平均每潮浪条数
        "avg_per_wave": round((msgs / waves), 2) if waves > 0 else None,
        # 相对「每句一次」节省的 LLM 比例
        "llm_save_rate": round((saved / msgs), 3) if msgs > 0 else None,
        "today": _day_key(),
    }
    today = out["summary"]["today"]
    day_b = (out.get("by_day") or {}).get(today) or _empty_bucket()
    tw, tm, ts, te = (
        int(day_b.get("waves") or 0),
        int(day_b.get("messages") or 0),
        int(day_b.get("saved_llm") or 0),
        int(day_b.get("enqueued") or 0),
    )
    out["summary"]["today_waves"] = tw
    out["summary"]["today_messages"] = tm
    out["summary"]["today_saved_llm"] = ts
    out["summary"]["today_enqueued"] = te
    out["summary"]["today_merge_rate"] = round((tm / te), 3) if te > 0 else None
    out["summary"]["today_llm_save_rate"] = round((ts / tm), 3) if tm > 0 else None
    return out


def _record_wave_from_batch(batch: dict) -> None:
    """从 flush/enqueue 摘出的 batch 记潮浪。"""
    if not batch:
        return
    md = batch.get("metadata") or {}
    count = int(batch.get("count") or md.get("coalesce_count") or len(batch.get("parts") or []) or 1)
    profile = (
        batch.get("profile")
        or md.get("coalesce_profile")
        or "default"
    )
    reason = batch.get("reason") or md.get("coalesce_reason") or batch.get("flush_reason") or "unknown"
    try:
        record_coalesce_wave(
            profile=str(profile),
            count=count,
            reason=str(reason),
            key=str(batch.get("key") or ""),
            user_id=str(batch.get("user_id") or ""),
        )
    except Exception as e:
        logger.debug(f"record coalesce wave skip: {e}")
