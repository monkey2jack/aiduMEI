"""Respect the candidate budget across explicit legacy/current SDK signatures."""
from inspect import signature


def search_candidates(memory, query: str, *, filters: dict, limit: int):
    # Current SDKs swallow unknown kwargs, so retry-on-TypeError cannot detect
    # an ignored legacy `limit`. Select the declared keyword before calling.
    try:
        parameters = signature(memory.search).parameters
    except (TypeError, ValueError):
        parameters = {}
    keyword = "limit" if "limit" in parameters and "top_k" not in parameters else "top_k"
    return memory.search(query, filters=filters, **{keyword: min(max(limit, 1), 300)})
