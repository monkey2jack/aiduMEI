"""ducky.agent_keys — one key per agent, bound to the identities it may act as
(f0.7+; closes ext-audit S3 together with the binding check below).

Before: the service accepted exactly one Bearer token (AIDUMEM_API_TOKEN) and
believed whatever ``caller_user_id`` / ``user_id`` the holder declared, so two
agents sharing a service could read and write each other's memories.

Now:
  * ``<DATA_DIR>/agent_keys.json`` (0600) lists extra keys by **sha256 only**
    — the server never stores a key in plain text:

        {"version": 1,
         "main": {"agent": "<name>", "callers": ["<user_id>", ...]},
         "keys": [{"agent": "<name>", "sha256": "<hex>", "callers": [...],
                   "created_at": "..."}]}

    ``main`` optionally binds the existing AIDUMEM_API_TOKEN to an identity.
  * A request authenticated with one of these keys is bound to that agent.
    Each scoped access (target user_id + declared caller) is checked against
    the key's ``callers``.
  * Backward compatible by default: AIDUMEI_AGENT_KEY_ENFORCE=0 (shadow) only
    counts mismatches in write_policy.db ``key_events``; =1 refuses them (403).
    The shared main key keeps working until the operator moves the second
    agent onto its own key and turns enforcement on.

Manage keys with ``scripts/agent_keys.py`` (create/list/revoke/bind-main).

f0.7.3: the binding is checked on every classified route (``check_route``, called
from the route registrar), not only where a handler calls the Pantheon policy
point; an agent key never performs instance-wide operations.  Hooks pick the
key file ``<AIDUMEI_KEYS_DIR>/<user_id>.key`` for the identity they act as.
"""
from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger("aiduMEM.agent_keys")

_CURRENT: contextvars.ContextVar[dict | None] = contextvars.ContextVar("aidumei_agent_key", default=None)
# (target, caller, action, outcome) already recorded for this request — the route
# guard and the handler's own Pantheon check may see the same access twice.
_SEEN: contextvars.ContextVar[set | None] = contextvars.ContextVar("aidumei_agent_key_seen", default=None)
_cache: dict = {"path": "", "mtime": -1.0, "data": None}
_cache_lock = threading.Lock()


def keys_path() -> str:
    explicit = os.environ.get("AIDUMEI_AGENT_KEYS_FILE", "").strip()
    if explicit:
        return explicit
    from ducky.utils import DATA_DIR
    return os.path.join(os.environ.get("AIDUMEM_DATA_DIR") or DATA_DIR, "agent_keys.json")


def enforce_on() -> bool:
    return os.environ.get("AIDUMEI_AGENT_KEY_ENFORCE", "0").strip().lower() in ("1", "true", "yes", "on")


def sha256_hex(token: str) -> str:
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def load() -> dict:
    """Parsed key file (cached by mtime).  Missing file → empty table.
    A malformed file is treated as empty *and* logged — it can only remove
    extra keys, never widen access (the main token is unaffected)."""
    path = keys_path()
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return {"version": 1, "keys": []}
    with _cache_lock:
        if _cache["path"] == path and _cache["mtime"] == mtime and _cache["data"] is not None:
            return _cache["data"]
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict) or not isinstance(data.get("keys", []), list):
                raise ValueError("agent_keys.json must be an object with a 'keys' list")
        except (OSError, ValueError) as exc:
            logger.error("agent_keys.json unreadable (%s); extra agent keys disabled", type(exc).__name__)
            data = {"version": 1, "keys": []}
        _cache.update(path=path, mtime=mtime, data=data)
        return data


def save(data: dict) -> None:
    path = keys_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    with _cache_lock:
        _cache.update(path="", mtime=-1.0, data=None)


def configured() -> bool:
    return bool(load().get("keys"))


def match(token: str) -> dict | None:
    """The agent entry whose sha256 equals sha256(token), else None."""
    if not token:
        return None
    digest = sha256_hex(token)
    for entry in load().get("keys", []):
        if isinstance(entry, dict) and hmac.compare_digest(str(entry.get("sha256", "")), digest):
            return entry
    return None


def set_current(entry: dict | None, fingerprint: str) -> None:
    _CURRENT.set({"agent": str((entry or {}).get("agent") or ""), "fp": fingerprint,
                  "callers": [str(c) for c in (entry or {}).get("callers") or []],
                  "main": entry is None} if fingerprint else None)
    _SEEN.set(set())


def clear_current() -> None:
    _CURRENT.set(None)
    _SEEN.set(set())


def current() -> dict | None:
    """Identity bound to this request's key: extra agent key, or the main key
    when ``main`` is declared in agent_keys.json.  None = unbound."""
    cur = _CURRENT.get()
    if not cur:
        return None
    if cur.get("main"):
        main = load().get("main")
        if not isinstance(main, dict) or not main.get("callers"):
            return None
        return {"agent": str(main.get("agent") or "main"), "fp": cur["fp"],
                "callers": [str(c) for c in main.get("callers") or []], "main": True}
    return cur


def _record(cur: dict, target: str, caller: str, action: str, outcome: str) -> None:
    try:
        from ducky.write_policy import connect
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        conn = connect()
        try:
            conn.execute(
                "INSERT INTO key_events (day,key_agent,key_fp,target_user,caller,action,outcome,n,last_ts)"
                " VALUES (?,?,?,?,?,?,?,1,?) ON CONFLICT(day,key_fp,target_user,caller,action,outcome)"
                " DO UPDATE SET n=n+1, last_ts=excluded.last_ts",
                (day, cur.get("agent") or "", cur.get("fp") or "", target or "", caller or "", action or "",
                 outcome, datetime.now(timezone.utc).isoformat(timespec="seconds")))
            conn.commit()
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ImportError) as exc:   # observation must never break a request
        logger.debug("key_events record failed: %s", type(exc).__name__)


def check_scope(target_user: str, caller: str, action: str) -> None:
    """Called on every scoped access.  Unbound requests pass untouched."""
    cur = current()
    if not cur:
        return
    allowed = set(cur.get("callers") or [])
    tgt, clr = str(target_user or "").strip(), str(caller or "").strip()
    ok = tgt in allowed and (not clr or clr in allowed)
    if ok:
        return
    outcome = "refused" if enforce_on() else "mismatch_shadow"
    seen = _SEEN.get()
    mark = (tgt, clr, action, outcome)
    if seen is None or mark not in seen:
        _record(cur, tgt, clr, action, outcome)
        if seen is not None:
            seen.add(mark)
    if outcome == "refused":
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail={
            "error": "agent_key_identity_mismatch", "agent": cur.get("agent"),
            "hint": "this key is bound to other identities; use the key issued to this agent"})
    logger.info("agent key %s used for %s/%s (%s) — shadow, allowed", cur.get("agent"), tgt, clr, action)


GLOBAL_TARGET = "*"


def check_route(resource: str, action: str, data: dict, method: str, path: str) -> None:
    """f0.7.3: the key binding is checked on **every** classified route, not only
    where a handler happens to call the Pantheon policy point.

    Before 0.7.3 a bound key reached instance-wide routes (``/stats`` without a
    user_id, ``/metrics``, ``/crystals`` …) and any scoped route whose handler
    skipped ``authorize_cross_hall`` untouched — with enforcement on, a second
    agent could still list or count the first agent's memories there.

    * ``public`` routes: nothing to check.
    * ``admin`` routes (instance-wide state): an agent key never acts for the
      whole instance — owners use the console session.  Target recorded as ``*``.
    * scoped routes: the target is the user_id/owner the handler will actually
      use; an empty one means "all tenants" to several legacy readers, so it is
      treated as instance-wide too.
    """
    cur = current()
    if not cur or resource == "public":
        return
    if resource == "admin":
        check_scope(GLOBAL_TARGET, "", f"admin:{method.upper()} {path}")
        return
    from ducky.utils import DEFAULT_USER_ID
    if resource == "manual":
        target = data.get("scope_user_id") or data.get("user_id") or ""
    elif resource == "default_owner":
        target = DEFAULT_USER_ID
    elif "user_id" in data or "owner" in data:
        target = data.get("user_id", data.get("owner"))
    else:
        target = DEFAULT_USER_ID
    target = str(target or "").strip() or GLOBAL_TARGET
    caller = str(data.get("caller_user_id") or data.get("caller_agent_id") or data.get("caller") or "")
    check_scope(target, caller, action)


def new_token() -> str:
    import secrets
    return "amk_" + secrets.token_urlsafe(32)


def add_key(agent: str, callers: list[str], token: str) -> dict:
    data = load()
    data = {"version": 1, **{k: v for k, v in data.items() if k != "keys"},
            "keys": [k for k in data.get("keys", []) if k.get("agent") != agent]}
    entry = {"agent": agent, "sha256": sha256_hex(token), "callers": sorted(set(callers)),
             "fp": hashlib.sha256(token.encode()).hexdigest()[:16],
             "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    data["keys"].append(entry)
    save(data)
    return {k: v for k, v in entry.items() if k != "sha256"}
