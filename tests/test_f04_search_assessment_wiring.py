"""Final evidence observations bind actual HTTP output, including cache hits."""
from copy import deepcopy

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest


@pytest.mark.parametrize("workspace", [False, True])
def test_assessment_sees_only_final_evidence_and_does_not_filter(monkeypatch, workspace):
    from ducky import decision, decision_assessment, memory_workspace, verbatim_vault
    from ducky.hot import search
    rows = [{"id": "strong", "memory": "Synthetic event happened in May", "score": .9},
            {"id": "extra", "memory": "Another relevant event", "score": .8},
            {"id": "weak", "memory": "Unrelated", "score": .001}]
    monkeypatch.setenv("AIDUMEM_RECALL_SCORE_FLOOR", "0.1")
    monkeypatch.setattr(decision, "settings", lambda: {"status": "disabled"})
    monkeypatch.setattr(memory_workspace, "ws_lookup", lambda *a, **k: deepcopy(rows) if workspace else [])
    monkeypatch.setattr(memory_workspace, "ws_feed_from_results", lambda *a, **k: None)
    monkeypatch.setattr(search, "get_memory", lambda: object())
    monkeypatch.setattr(search, "lazy_import_hybrid", lambda: lambda *a, **k: deepcopy(rows))
    monkeypatch.setattr(search, "ensure_bank_registered", lambda *a: None)
    monkeypatch.setattr(search, "boost_salience_for_results", lambda *a: None)
    monkeypatch.setattr(search, "_annotate_memory_types", lambda *a, **k: None)
    monkeypatch.setattr(search, "_log_search_result", lambda *a: None)
    monkeypatch.setattr(verbatim_vault, "verbatim_search", lambda *a, **k: [])
    seen = []
    def assess(query, evidence, user_id, bank_id, *, final):
        assert final and decision.retrieval_budget()["calls"] == 0
        seen.append(deepcopy(evidence))
        return evidence, {"status": "ok", "verdict": "missing_evidence", "applied": False,
                          "evidence_digest": decision_assessment.evidence_digest(query, evidence, user_id, bank_id)}
    monkeypatch.setattr(decision_assessment, "assess_evidence", assess)
    app = FastAPI()
    search.register_search_routes(app)
    body = TestClient(app).post('/search', json={"query": "when", "user_id": "assessment-owner", "limit": 1}).json()
    assert body['status'] == 'ok'
    assert [r['id'] for r in body['results']] == ['strong']
    assert seen == [body['results']]
    assert body['_evidence_assessment']['verdict'] == 'missing_evidence'
    assert body['recall_verdict'] == 'found'


def test_observer_failure_preserves_recall(monkeypatch):
    from ducky import decision_assessment
    from ducky.hot.search import _observe_final_evidence
    def fail(*a, **k):
        raise RuntimeError('synthetic')
    monkeypatch.setattr(decision_assessment, 'assess_evidence', fail)
    result = _observe_final_evidence('q', [{'memory': 'valid'}], 'u', 'default')
    assert result['status'] == 'unknown' and result['applied'] is False
