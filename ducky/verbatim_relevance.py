"""Score supplemental originals before they can consume the original-text quota."""
from ducky.recall_evidence import filter_rerank_relevance, valid_rerank_scores


def merge_originals(main: list, hits: list, query: str, limit: int,
                    user_id: str, bank_id: str) -> list:
    from ducky.verbatim_vault import fuse_verbatim
    from ducky.query_aliases import resolve_query_aliases
    preview = fuse_verbatim(main, hits, limit=limit, query=query)
    def is_new(row):
        return not any(row is item for item in main)
    if not any(is_new(row) for row in preview):
        return preview
    pool = fuse_verbatim(main, hits, limit=0, query=query)
    originals = rank_originals(resolve_query_aliases(query, user_id, bank_id),
                               [row for row in pool if is_new(row)])
    return fuse_verbatim(main, originals, limit=limit, query=query)


def rank_originals(query: str, hits: list) -> list:
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
        return rows
    finally:
        supplemental = dict(last_rerank_telemetry() or {"status": "not_invoked"})
        if main is not None:
            main["verbatim"] = supplemental
            restore_rerank_telemetry(main)
