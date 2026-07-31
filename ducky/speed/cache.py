"""aiduMEM speed · 抽取结果缓存"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from collections import OrderedDict
from typing import Any

from ducky.speed.config import load_speed_cfg

_cache_lock = threading.Lock()
_extract_cache: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()


def _norm_for_cache(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def cache_key(user_id: str, text: str, mode: str = "infer") -> str:
    raw = f"{user_id}|{mode}|{_norm_for_cache(text)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def cache_get(key: str):
    ttl = float(load_speed_cfg().get("extract_cache_ttl_sec", 3600))
    now = time.time()
    with _cache_lock:
        item = _extract_cache.get(key)
        if not item:
            return None
        ts, val = item
        if now - ts > ttl:
            _extract_cache.pop(key, None)
            return None
        _extract_cache.move_to_end(key)
        return val


def cache_set(key: str, value) -> None:
    maxn = int(load_speed_cfg().get("extract_cache_max", 256))
    with _cache_lock:
        _extract_cache[key] = (time.time(), value)
        _extract_cache.move_to_end(key)
        while len(_extract_cache) > maxn:
            _extract_cache.popitem(last=False)
