"""Query-local relevance evidence; raw and reranker scores keep separate scales."""
from __future__ import annotations

import math

from ducky.env_config import float_env


def rerank_rescues(item: dict, floor: float, *, enabled: bool) -> bool:
    """A strong rerank signal AND adequate fused relevance can rescue a weak vector.

    The reranker threshold is a deployment calibration parameter, not a
    probability. Call only for freshly scored results, never for cache payloads.
    """
    if not enabled or floor <= 0:
        return False
    minimum = float_env("AIDUMEI_RERANK_RESCUE_THRESHOLD", 0.9, minimum=0.0, maximum=1.0)
    values = [item.get(k) for k in ("score", "_rerank_score", "_hybrid_score")]
    if any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v)
           for v in values):
        return False
    raw, rerank, fused = values
    return 0 <= raw < floor and minimum <= rerank <= 1 and fused >= floor
