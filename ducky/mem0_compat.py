"""Compatibility for mem0 enumeration without changing the caller's scope."""
from __future__ import annotations

import inspect
from typing import Any


def get_all_memories(memory: Any, *, filters: dict, limit: int) -> Any:
    """Use the SDK's declared size parameter, preserving filters and response.

    New mem0 releases name it ``top_k`` and silently ignore ``limit`` in
    ``**kwargs``. Older releases use ``limit`` and can ignore ``top_k`` the
    same way. Inspect before calling rather than retrying a backend TypeError.
    Opaque wrappers use the current SDK spelling; no unscoped retry is made.
    """
    method = memory.get_all
    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):
        parameters = {}
    size_key = "limit" if "limit" in parameters and "top_k" not in parameters else "top_k"
    return method(filters=filters, **{size_key: limit})
