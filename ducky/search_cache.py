"""f0.8+ C: short TTL cache for repeated /search calls (plan item 9).

The same query often arrives twice within seconds (a host's prefetch then its
turn-start recall, a retry after a client timeout).  A full /search costs an
embedding call, up to two rerank calls and the optional decision check, so the
second copy is served from here.

Correctness rules:
* Key = (user_id, bank_id, caller, query, effective limit, before, after,
  session_id).  Authorization runs *before* the lookup, so a cached answer
  is never handed to a caller that would have been refused.
* Every write for a user bumps that user's epoch; a write whose user is unknown
  bumps the global epoch.  An entry is served only while both epochs it was
  stored under are unchanged and it is younger than the TTL.
* Only clean answers are stored: status ok, verdict not degraded, rerank not
  timed out / skipped by the breaker.  A degraded answer must not stick.
* AIDUMEI_SEARCH_CACHE_TTL_S (default 20 s, 0 = off), at most 512 entries.
"""
from __future__ import annotations

import copy
import hashlib
import threading
import time
from typing import Any, Optional

_MAX_ENTRIES = 512
_lock = threading.Lock()
_entries: "dict[str, tuple[float, str, int, int, dict]]" = {}
_user_epoch: "dict[str, int]" = {}
_global_epoch = 0
_stats = {"hits": 0, "misses": 0, "stores": 0, "invalidations": 0}


def ttl_seconds() -> float:
    from ducky.env_config import float_env
    return float_env("AIDUMEI_SEARCH_CACHE_TTL_S", 20.0, minimum=0.0, maximum=3600.0)


def _norm_user(user_id: Any) -> str:
    return str(user_id or "").strip()


def make_key(*, user_id: str, bank_id: str, caller: str, query: str, limit: int,
             before: str, after: str, session_id: str) -> str:
    h = hashlib.sha256()
    # Bind the key to the epoch when the read started. A concurrent write may
    # finish before this read stores its answer; that old answer must not seed
    # the cache used by requests that start after the write.
    with _lock:
        epoch = (_global_epoch, _user_epoch.get(_norm_user(user_id), 0))
    h.update(str(epoch).encode("ascii") + b"\x00")
    for part in (_norm_user(user_id), bank_id or "default", caller or "", query, str(limit),
                 before or "", after or "", session_id or ""):
        h.update(str(part).encode("utf-8", "replace") + b"\x00")
    return h.hexdigest()


def get(key: str, user_id: str) -> Optional[dict]:
    ttl = ttl_seconds()
    if ttl <= 0:
        return None
    user = _norm_user(user_id)
    with _lock:
        hit = _entries.get(key)
        if hit is None:
            _stats["misses"] += 1
            return None
        ts, owner, uep, gep, resp = hit
        if (owner != user or time.monotonic() - ts > ttl or uep != _user_epoch.get(user, 0)
                or gep != _global_epoch):
            _entries.pop(key, None)
            _stats["misses"] += 1
            return None
        _stats["hits"] += 1
        out = copy.deepcopy(resp)
    out["_cache"] = {"hit": True, "age_ms": round((time.monotonic() - ts) * 1000)}
    return out


def cacheable(resp: dict) -> bool:
    if not isinstance(resp, dict) or resp.get("status") != "ok":
        return False
    if resp.get("recall_verdict") in ("degraded", "empty_query"):
        return False
    if resp.get("_rerank_degraded"):
        return False
    return True


def put(key: str, user_id: str, resp: dict) -> None:
    if ttl_seconds() <= 0 or not cacheable(resp):
        return
    user = _norm_user(user_id)
    snapshot = copy.deepcopy(resp)
    snapshot.pop("_cache", None)
    with _lock:
        if len(_entries) >= _MAX_ENTRIES:
            for k in sorted(_entries, key=lambda k: _entries[k][0])[:_MAX_ENTRIES // 4]:
                _entries.pop(k, None)
        _entries[key] = (time.monotonic(), user, _user_epoch.get(user, 0), _global_epoch, snapshot)
        _stats["stores"] += 1


def invalidate_user(user_id: Any = None) -> None:
    """A write landed for ``user_id``; None/empty = unknown user → everything."""
    global _global_epoch
    user = _norm_user(user_id)
    names = {user} if user else set()
    if user:
        # /search keys on the canonical id; a write may carry a raw or legacy id.
        try:
            from ducky.bank_contract import normalize_user_id
            from ducky.mem0_runtime import _normalize_user_id
            names.add(_normalize_user_id(normalize_user_id(user)))
        except (ImportError, OSError, ValueError, TypeError, RuntimeError):
            names = set()
    with _lock:
        _stats["invalidations"] += 1
        if not names:
            _global_epoch += 1
            _entries.clear()
            return
        for name in names:
            _user_epoch[name] = _user_epoch.get(name, 0) + 1
        for k in [k for k, v in _entries.items() if v[1] in names]:
            _entries.pop(k, None)


def clear() -> None:
    global _global_epoch
    with _lock:
        _entries.clear()
        _user_epoch.clear()
        _global_epoch += 1


def stats() -> dict:
    with _lock:
        return dict(_stats, entries=len(_entries), ttl_s=ttl_seconds())


_READ_ONLY_POSTS = frozenset({"/search", "/search_trace"})


def invalidate_for_http(method: str, path: str, query_user: Any = None,
                        body: Optional[bytes] = None) -> None:
    """Called once per HTTP response: any mutating request drops the writer's entries.

    The user comes from the query string or a JSON body the server already
    buffered; when neither names one, everything is dropped (safe default)."""
    if (method or "").upper() in ("GET", "HEAD", "OPTIONS"):
        return
    if (method or "").upper() == "POST" and path in _READ_ONLY_POSTS:
        return
    user = _norm_user(query_user)
    if not user and body:
        try:
            import json
            data = json.loads(body)
            if isinstance(data, dict):
                user = _norm_user(data.get("user_id") or data.get("owner"))
        except (ValueError, TypeError, UnicodeDecodeError):
            user = ""
    invalidate_user(user or None)
