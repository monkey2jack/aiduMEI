"""Bounded, process-local protection for repeated failing tool calls.

Keys contain hashes only. Successful calls are unlimited; already-running calls
are never cancelled. Hosts must still enforce their own turn and budget limits.
"""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
from functools import wraps
import asyncio
import hashlib
import inspect
import json
import logging
import math
import os
import threading
import time

from .env_config import float_env, int_env

RETRY_ADVICE = "Repeated failure: change the arguments or strategy instead of repeating this call."
default_logger = logging.getLogger(__name__)

# Fields that change between transport attempts without changing the business
# operation. Business pagination, timestamps and nested payloads retain their
# meaning and must not be collapsed into the same operation.
_VOLATILE_ARGUMENT_KEYS = frozenset({
    "request_id", "trace_id", "tracking_id", "correlation_id",
})


def canonicalize_arguments(value):
    """Remove top-level tracing noise; preserve nested business inputs."""
    if isinstance(value, dict):
        return {
            str(key): item
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key).lower() not in _VOLATILE_ARGUMENT_KEYS
        }
    return value


def retry_hint(count):
    hint = {"retry_count": count} if count >= 2 else {}
    if count >= 3:
        hint["loop_warning"] = RETRY_ADVICE
    return hint


def fingerprint(tool, arguments, scope="stdio"):
    raw = json.dumps([str(scope), str(tool), canonicalize_arguments(arguments)],
                     sort_keys=True, ensure_ascii=False,
                     separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def failed_result(result):
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (ValueError, TypeError):
            return False
    return isinstance(result, dict) and (
        bool(result.get("error")) or result.get("status") in ("error", "failed")
    )


@dataclass
class _State:
    failures: deque = field(default_factory=deque)
    open_until: float = 0.0
    probing: bool = False


class LoopGuard:
    def __init__(self, *, enabled=True, threshold=5, window_s=60, cooldown_s=30,
                 capacity=256, probe_timeout_s=30, clock=time.monotonic, logger=default_logger):
        if threshold < 2 or capacity < 1 or not all(
            math.isfinite(v) and v > 0 for v in (window_s, cooldown_s, probe_timeout_s)
        ):
            raise ValueError("Invalid loop guard configuration")
        self.enabled, self.threshold = enabled, threshold
        self.window_s, self.cooldown_s = window_s, cooldown_s
        self.capacity, self.clock = capacity, clock
        self.probe_timeout_s = probe_timeout_s
        self.logger = logger
        self._states = OrderedDict()
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls, *, logger=default_logger):
        try:
            return cls(
                enabled=os.getenv("AIDUMEI_MCP_LOOP_GUARD", "1").lower() not in
                {"0", "false", "off", "no"},
                threshold=int_env("AIDUMEI_MCP_LOOP_GUARD_THRESHOLD", 5, minimum=2),
                window_s=float_env("AIDUMEI_MCP_LOOP_GUARD_WINDOW_S", 60, exclusive_minimum=0),
                cooldown_s=float_env("AIDUMEI_MCP_LOOP_GUARD_COOLDOWN_S", 30, exclusive_minimum=0),
                logger=logger,
            )
        except (ValueError, OverflowError):
            logger.warning("[loop-guard] invalid configuration; using defaults")
            return cls(logger=logger)

    def begin(self, key):
        """Return an admission token or a structured rejection."""
        with self._lock:
            now = self.clock()
            state = self._states.setdefault(key, _State())
            self._states.move_to_end(key)
            while len(self._states) > self.capacity:
                self._states.popitem(last=False)
            if state.open_until:
                if now < state.open_until or state.probing:
                    return None, {"error": "circuit_open", "retry_count": len(state.failures),
                                  "loop_warning": RETRY_ADVICE,
                                  "retry_after": max(1, math.ceil(state.open_until - now))}
                state.probing = True
            return (key, state, state.probing), None

    def finish(self, token, failed):
        with self._lock:
            key, state, probe = token
            if self._states.get(key) is not state:
                return {}  # Evicted while in flight; do not recreate stale state.
            if not failed:
                state.failures.clear()
                state.open_until = 0.0
                state.probing = False
                return {}
            now = self.clock()
            while state.failures and state.failures[0] <= now - self.window_s:
                state.failures.popleft()
            state.failures.append(now)
            # Concurrent calls may finish after the circuit opens. Keep bounded.
            while len(state.failures) > self.threshold:
                state.failures.popleft()
            if probe or len(state.failures) >= self.threshold:
                state.open_until = now + self.cooldown_s
                state.probing = False
            return retry_hint(len(state.failures))

    def cancel(self, token):
        """Release a cancelled probe without counting an uncompleted call."""
        if token is None:
            return
        with self._lock:
            key, state, probe = token
            if probe and self._states.get(key) is state:
                state.probing = False

    def wrap(self, fn, scope=lambda: "stdio"):
        signature = inspect.signature(fn)

        def prepare(args, kwargs):
            if not self.enabled:
                return None, None
            try:
                bound = signature.bind(*args, **kwargs)
                bound.apply_defaults()
                token, rejection = self.begin(fingerprint(fn.__name__, bound.arguments, scope()))
                if rejection:
                    self.logger.warning("[loop-guard] %s circuit_open retry_after=%s",
                                   fn.__name__, rejection["retry_after"])
                return token, rejection
            except Exception:
                self.logger.warning("[loop-guard] admission unavailable; allowing call")
                return None, None

        def finish(token, result=None, exception=None):
            if token is None:
                return result, {}
            try:
                hint = self.finish(token, exception is not None or failed_result(result))
                if hint:
                    self.logger.warning("[loop-guard] %s retry_count=%s", fn.__name__, hint["retry_count"])
                    if exception is None:
                        value = json.loads(result) if isinstance(result, str) else dict(result)
                        value.update(hint)
                        result = json.dumps(value, ensure_ascii=False) if isinstance(result, str) else value
                return result, hint
            except Exception:
                self.logger.warning("[loop-guard] accounting unavailable; preserving result")
                return result, {}

        def raise_failure(exc, hint):
            if hint:
                raise RuntimeError(json.dumps({"error": type(exc).__name__, **hint})) from exc
            raise exc

        @wraps(fn)
        def sync(*args, **kwargs):
            token, rejection = prepare(args, kwargs)
            if rejection:
                return json.dumps(rejection)
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:
                if not isinstance(exc, Exception):
                    self.cancel(token)
                    raise
                _, hint = finish(token, exception=exc)
                raise_failure(exc, hint)
            else:
                return finish(token, result)[0]

        @wraps(fn)
        async def asynchronous(*args, **kwargs):
            token, rejection = prepare(args, kwargs)
            if rejection:
                return json.dumps(rejection)
            try:
                if token is not None and token[2]:
                    result = await asyncio.wait_for(fn(*args, **kwargs), timeout=self.probe_timeout_s)
                else:
                    result = await fn(*args, **kwargs)
            except BaseException as exc:
                if not isinstance(exc, Exception):
                    # Cancellation and process control exceptions must retain
                    # their native semantics after the guard has cleaned up.
                    self.cancel(token)
                    raise
                _, hint = finish(token, exception=exc)
                raise_failure(exc, hint)
            else:
                return finish(token, result)[0]

        return asynchronous if inspect.iscoroutinefunction(fn) else sync
