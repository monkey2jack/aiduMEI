"""Score supplemental originals before they can consume the original-text quota."""
from ducky.recall_evidence import filter_rerank_relevance, valid_rerank_scores


def original_lookup_query(query: str) -> str:
    """Remove explicit quote request boilerplate, retaining the requested topic."""
    import re
    if not re.search(r"原话|原文|逐字|一字不差", query):
        return query
    cleaned = re.sub(r"(?:的)?(?:原话|原文)(?:是什么|怎么说的|是怎样的)?(?:[，,]?(?:并|及|和)(?:总结|概括)(?:一下)?)?|逐字|一字不差", "", query)
    cleaned = re.sub(r"^(?:请|关于|请给我|告诉我)\s*", "", cleaned).strip(" ，。？！?！:")
    return cleaned if len(cleaned) >= 2 else query


def merge_originals(main: list, hits: list, query: str, limit: int,
                    user_id: str, bank_id: str) -> list:
    from ducky.verbatim_vault import fuse_verbatim
    from ducky.query_aliases import resolve_query_aliases
    relevance_query = original_lookup_query(query)
    preview = fuse_verbatim(main, hits, limit=limit, query=relevance_query, intent_query=query)
    def is_new(row):
        return not any(row is item for item in main)
    if not any(is_new(row) for row in preview):
        return preview
    pool = fuse_verbatim(main, hits, limit=0, query=relevance_query, intent_query=query)
    originals = rank_originals(resolve_query_aliases(relevance_query, user_id, bank_id),
                               [row for row in pool if is_new(row)], user_id, bank_id,
                               decision_query=query)
    return fuse_verbatim(main, originals, limit=limit, query=relevance_query, intent_query=query)


def rank_originals(query: str, hits: list, user_id: str = "default", bank_id: str = "default", *, decision_query: str | None = None) -> list:
    from ducky.mem0_runtime import rerank, last_rerank_telemetry, restore_rerank_telemetry
    main = last_rerank_telemetry()
    rows = [dict(hit) for hit in hits]
    for row in rows:
        row.pop("_rerank_score", None)
        row.pop("_rerank_original_verified", None)
    try:
        response = rerank(query, [str(row.get("memory") or row.get("content") or "")
                                  for row in rows], top_n=len(rows))
        telemetry = last_rerank_telemetry() or {"status": "not_invoked"}
        scores = valid_rerank_scores(response, len(rows)) if telemetry.get("status") == "ok" else {}
        for index, score in scores.items():
            rows[index]["_rerank_score"] = score
            rows[index]["_rerank_original_verified"] = True
        rows, gate = filter_rerank_relevance(rows, scores)
        telemetry.update(applied=bool(scores), relevance=gate)
        # Unknown scores retain fallback ordering and are never interpreted as 0.
        rows.sort(key=lambda row: row.get("_rerank_score", -1), reverse=True)
        from ducky.decision import filter_evidence
        return filter_evidence(decision_query or query, rows, user_id, bank_id)
    finally:
        supplemental = dict(last_rerank_telemetry() or {"status": "not_invoked"})
        if main is not None:
            main["verbatim"] = supplemental
            restore_rerank_telemetry(main)
