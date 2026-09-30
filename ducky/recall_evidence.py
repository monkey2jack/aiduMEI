"""Query-local relevance evidence; raw and reranker scores keep separate scales."""
from __future__ import annotations

import math

from ducky.env_config import float_env


def rerank_min_relevance() -> float:
    """Deployment-calibrated rejection threshold, independent of vector scores."""
    return float_env("AIDUMEI_RERANK_MIN_RELEVANCE", 0.1, minimum=0.0, maximum=1.0)


def valid_rerank_scores(rows: list, count: int) -> dict:
    """Missing, duplicate or malformed evidence is unknown, never a zero score."""
    scores, seen = {}, set()
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        index, score = row.get("index"), row.get("relevance_score")
        if type(index) is not int or not 0 <= index < count:
            continue
        if index in seen:
            scores.pop(index, None)
            continue
        seen.add(index)
        if (isinstance(score, bool) or not isinstance(score, (int, float))
                or not math.isfinite(score) or not 0 <= score <= 1):
            continue
        scores[index] = float(score)
    return scores


def filter_rerank_relevance(items: list, scores: dict) -> tuple[list, dict]:
    minimum = rerank_min_relevance()
    kept = [item for index, item in enumerate(items)
            if index not in scores or scores[index] >= minimum]
    return kept, {"threshold": minimum, "scored": len(scores),
                  "unscored": len(items) - len(scores), "dropped": len(items) - len(kept)}


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
