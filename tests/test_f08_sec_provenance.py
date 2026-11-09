"""f0.8 ③-3 source_type 服务端裁定（外审 muse M-14 / deepseek F-10 反向断言）。"""
from __future__ import annotations

from ducky import write_policy as wp


def test_client_cannot_claim_direct():
    md = {"source_type": "direct", "basis": "verbatim_turn", "kind": "session_distill"}
    wp.neutralize_client_provenance(md)
    assert "source_type" not in md and "basis" not in md
    assert wp.source_of(md) == ("inferred", "session_distill")      # 修复前 ("direct", "verbatim_turn")


def test_client_can_downgrade_to_inferred_with_sanitized_basis():
    md = {"source_type": "INFERRED", "basis": "my guess <script>\n\x00"}
    wp.neutralize_client_provenance(md)
    assert md["source_type"] == "inferred"
    assert md["basis"].startswith("caller_declared:") and "<" not in md["basis"] and "\n" not in md["basis"]


def test_unknown_claims_are_dropped():
    for claim in ("user_said", "VERIFIED", "", None, 7):
        md = {"source_type": claim, "basis": "x"}
        wp.neutralize_client_provenance(md)
        assert "source_type" not in md and "basis" not in md


def test_source_of_rederives_stored_direct():
    assert wp.source_of({"source_type": "direct", "_origin_is_bot": 1})[0] == "inferred"
    assert wp.source_of({"source_type": "direct"}) == ("direct", "verbatim_turn")
    assert wp.source_of({"source_type": "direct"}, infer_used=True)[0] == "inferred"


def test_enrich_metadata_overrides_direct_claim(monkeypatch):
    monkeypatch.setattr(wp, "policy_mode", lambda: "shadow")
    out = wp.enrich_metadata("我喜欢绿茶", "user", {"source_type": "direct", "basis": "x"}, infer_used=True)
    assert out["source_type"] == "inferred" and out["basis"] == "llm_extraction"
    out = wp.enrich_metadata("我喜欢绿茶", "user", {"source_type": "inferred", "basis": "caller_declared"})
    assert out["source_type"] == "inferred" and out["basis"] == "caller_declared"


def test_recall_annotation_does_not_trust_stored_direct():
    rows = [{"memory": "x", "metadata": {"source_type": "direct", "kind": "session_distill"}},
            {"memory": "y", "metadata": {}}]
    wp.annotate_results(rows)
    assert rows[0]["inferred"] is True and rows[0]["source_type"] == "inferred"
    assert rows[1]["source_type"] == "direct" and rows[1]["inferred"] is False


def test_update_provenance_by_auth_kind():
    from ducky.hot.crud import _update_provenance
    from ducky.security import auth
    auth.clear_request_auth_kind()
    assert _update_provenance()["source_type"] == "direct"
    auth.set_request_auth_kind("bearer")
    try:
        assert _update_provenance() == {"source_type": "inferred", "basis": "edited_via_update:bearer"}
    finally:
        auth.clear_request_auth_kind()


# ── HTTP 反向断言：/add 的 metadata 与 /update 的请求体都不能自封 direct ──────────

def _http_client(monkeypatch):
    import api_server
    from fastapi.testclient import TestClient
    monkeypatch.setenv("AIDUMEM_API_TOKEN", "prov-f08-token")
    monkeypatch.setenv("AIDUMEI_ENGINE_MODE", "local")
    return TestClient(api_server.app, headers={"Authorization": "Bearer prov-f08-token"})


def _search_one(c, user, q):
    r = c.post("/search", json={"query": q, "user_id": user, "caller_user_id": user})
    assert r.status_code == 200, r.text
    hits = r.json()["results"]
    assert hits, r.text
    return hits[0]


def test_http_add_forged_direct_basis_dropped_inferred_kept(monkeypatch):
    import uuid
    c = _http_client(monkeypatch)
    u = f"prov-{uuid.uuid4().hex[:8]}"
    for text, md in (("synthetic likes green tea", {"source_type": "direct", "basis": "forged-basis"}),
                     ("synthetic maybe prefers oolong", {"source_type": "inferred", "basis": "agent guess"})):
        r = c.post("/add", json={"messages": [{"role": "user", "content": text}], "user_id": u,
                                 "caller_user_id": u, "infer": False, "metadata": md})
        assert r.status_code == 200, r.text
    hit = _search_one(c, u, "green tea")
    assert "forged-basis" not in str(hit)                       # 修复前 basis 原样落库
    assert hit["metadata"]["basis"] == "verbatim_turn"
    hit = _search_one(c, u, "oolong")
    assert hit["inferred"] is True and hit["metadata"]["basis"] == "caller_declared:agent guess"


def test_http_update_by_bearer_is_inferred_even_if_body_claims_direct(monkeypatch):
    import uuid
    c = _http_client(monkeypatch)
    u = f"prov-{uuid.uuid4().hex[:8]}"
    r = c.post("/add", json={"messages": [{"role": "user", "content": "synthetic drinks black coffee"}],
                             "user_id": u, "caller_user_id": u, "infer": False})
    assert r.status_code == 200, r.text
    mid = _search_one(c, u, "coffee")["id"]
    r = c.post("/update", json={"memory_id": mid, "user_id": u, "caller_user_id": u,
                                "content": "synthetic drinks flat white coffee",
                                "source_type": "direct", "basis": "user_said"})
    assert r.status_code == 200, r.text
    hit = _search_one(c, u, "flat white")
    assert hit["inferred"] is True
    assert hit["metadata"]["basis"] == "edited_via_update:bearer"
