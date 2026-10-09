"""Contract tests with frozen typed answers, not a claim of live model accuracy."""
from copy import deepcopy
import json

import pytest

from ducky import decision as d
from ducky import decision_assessment as a
from test_f04_decision_budget import channel as channel


def rows():
    return [{"id": "one", "memory": "Mara's tutor is Lin.", "user_id": "u", "bank_id": "b"},
            {"id": "two", "memory": "Lin studied in Kyoto.", "user_id": "u", "bank_id": "b"}]


def assess(source, cfg, query="Where did Mara's tutor study?"):
    return a.assess_evidence(query, source, "u", "b", final=True, cfg=cfg)


def response(monkeypatch, values):
    answers = {key: {"type": "noul", "noul": value} for key, value in values.items()}
    monkeypatch.setitem(d.PROVIDERS, "nace", lambda *args: {"model": d.MODEL, "answers": deepcopy(answers)})


def test_complete_multi_hop_is_observational_and_questions_are_independent(channel):
    cfg, _, calls, _ = channel
    baseline = rows()
    before = deepcopy(baseline)
    actual, info = assess(baseline, cfg)
    assert actual is baseline and actual == before
    assert info["status"] == "observed" and info["verdict"] == "sufficient"
    assert info["action"] == "observe_only" and info["applied"] is False
    assert info["schema_version"] == a.SCHEMA_VERSION and info["policy_version"] == a.POLICY_VERSION
    assert info["coverage"]["kind"] == "full" and info["evidence_digest"]
    assert a.matches_evidence(info, "Where did Mara's tutor study?", baseline, "u", "b")
    state, questions = calls[0][1:]
    assert set(questions) == set(a.FIELDS)
    assert all(q["type"] == "noul" and "independently" in q["instructions"] for q in questions.values())
    assert [json.loads(item["record"]) for item in state["evidence"]] == baseline
    assert set(info["probabilities"]) == set(a.FIELDS)


@pytest.mark.parametrize("values,reason", [
    ({"sufficient": .5, "missing": .9, "contradiction": .01, "continue": .95}, "missing_evidence"),
    ({"sufficient": .99, "missing": .01, "contradiction": .9, "continue": .8}, "contradiction"),
    ({"sufficient": .01, "missing": .99, "contradiction": .01, "continue": .01}, "missing_evidence"),
])
def test_partial_multi_hop_conflict_and_no_answer_never_mean_answered_or_not_found(channel, monkeypatch, values, reason):
    cfg, _, _, _ = channel
    if values["contradiction"] > .5:
        source = rows() + [{"memory": "Lin studied in Osaka, not Kyoto, during the same period."}]
    elif values["sufficient"] < .1:
        source = [{"memory": "Unrelated weather notes: today is sunny."}]
    else:
        source = rows()[:1]  # Known missing second hop in this deterministic fixture.
    response(monkeypatch, values)
    actual, info = assess(source, cfg)
    assert actual is source and info["verdict"] == "unresolved" and info["reason"] == reason
    assert "answered" not in info and "not_found" not in info.values()
    if values["continue"] < .15:
        assert "further_retrieval_unhelpful" in info["reasons"]


@pytest.mark.parametrize("sufficient,missing,contradiction,expected", [
    (.95, .1499, .1499, "sufficient"), (.9499, .01, .01, "unresolved"),
    (.99, .15, .01, "unresolved"), (.99, .01, .15, "unresolved"),
])
def test_strict_policy_thresholds(channel, monkeypatch, sufficient, missing, contradiction, expected):
    response(monkeypatch, {"sufficient": sufficient, "missing": missing,
                           "contradiction": contradiction, "continue": .99})
    assert assess(rows(), channel[0])[1]["verdict"] == expected


@pytest.mark.parametrize("source,query", [
    ([{"memory": "record " + str(i)} for i in range(13)], "question"),
    ([{"memory": "x" * 6000}], "question"),
    ([{"memory": "x" * 3500} for _ in range(5)], "question"),
    ([{"memory": "short"}], "q" * 2001),
])
def test_truncated_content_count_or_query_cannot_claim_sufficiency(channel, source, query):
    cfg, _, calls, _ = channel
    actual, info = assess(source, cfg, query)
    assert actual is source
    assert info["status"] == "observed" and info["verdict"] == "unresolved"
    assert info["reason"] == "partial_coverage" and not info["coverage"]["complete"]
    state = calls[0][1]
    assert len(state["evidence"]) <= 12 and len(state["query"]) <= 2000
    assert sum(len(item["record"]) for item in state["evidence"]) <= 12000
    assert all(len(item["record"]) <= 4000 for item in state["evidence"])


def test_digest_covers_omitted_tail_metadata_order_scope_and_query(channel):
    cfg, _, calls, _ = channel
    source = [{"memory": "x" * 6000 + "one"}]
    _, first = assess(source, cfg)
    source[0]["memory"] = "x" * 6000 + "two"
    _, second = assess(source, cfg)
    assert first["evidence_digest"] != second["evidence_digest"] and len(calls) == 2
    assert calls[0][1]["evidence"] == calls[1][1]["evidence"]  # Same truncated view; different exact evidence.
    baseline = rows()
    digests = {a.evidence_digest("q", baseline, "u", "b")}
    digests.add(a.evidence_digest("q2", baseline, "u", "b"))
    digests.add(a.evidence_digest("q", baseline[::-1], "u", "b"))
    digests.add(a.evidence_digest("q", baseline, "u", "another"))
    baseline[0]["recorded_at"] = "a different event time"
    digests.add(a.evidence_digest("q", baseline, "u", "b"))
    assert len(digests) == 5


def test_changed_evidence_during_provider_is_unknown_and_preserves_rows(channel, monkeypatch):
    source = rows()
    original = d.PROVIDERS["nace"]
    def mutate(*args):
        result = original(*args)
        source[1]["memory"] = "Lin studied in another city."
        return result
    monkeypatch.setitem(d.PROVIDERS, "nace", mutate)
    actual, info = assess(source, channel[0])
    assert actual is source and info["status"] == "unknown" and info["reason"] == "evidence_changed"
    assert not info["probabilities"] and not a.matches_evidence(info, "Where did Mara's tutor study?", source, "u", "b")


def test_expiry_during_post_provider_digest_check_stays_unknown(channel, monkeypatch):
    cfg, now, _, _ = channel
    original = a.matches_evidence
    def check(*args):
        now[0] += 4
        return original(*args)
    monkeypatch.setattr(a, "matches_evidence", check)
    info = assess(rows(), cfg)[1]
    assert info["status"] == "unknown" and info["reason"] == "deadline_fallback"


@pytest.mark.parametrize("field", a.FIELDS)
@pytest.mark.parametrize("bad", [True, float("nan"), float("inf"), -.01, 1.01, "0.99", None])
def test_every_noul_field_is_independently_validated(channel, monkeypatch, field, bad):
    values = {"sufficient": .99, "missing": .01, "contradiction": .01, "continue": .01, field: bad}
    response(monkeypatch, values)
    source = rows()
    actual, info = assess(source, channel[0])
    assert actual is source and info["status"] == "unknown" and not info["probabilities"]
    assert not d._cache


@pytest.mark.parametrize("bad", [None, {"answers": []}, {"answers": {}},
                                {"answers": {"sufficient": {"type": "choice", "choice": "yes", "confidence": .99}}}])
def test_missing_or_wrong_type_answers_do_not_turn_into_success(channel, monkeypatch, bad):
    monkeypatch.setitem(d.PROVIDERS, "nace", lambda *args: bad)
    source = rows()
    actual, info = assess(source, channel[0])
    assert actual is source and info["status"] == "unknown" and info["verdict"] == "unknown"


@pytest.mark.parametrize("foreign", [
    {"user_id": "other"}, {"bank_id": "other"}, {"metadata": {"user_id": "other"}},
    {"user_id": "u", "metadata": {"user_id": "other"}},
    {"bank_id": "b", "metadata": {"bank_id": "other"}}, {"metadata": "invalid"},
])
def test_all_scope_surfaces_checked_before_provider_even_beyond_projection(channel, foreign):
    cfg, _, calls, _ = channel
    source = rows() * 7 + [{"memory": "foreign-secret", **foreign}]
    actual, info = assess(source, cfg)
    assert actual is source and info["reason"] == "scope_mismatch" and not calls
    assert info["evidence_digest"] is None


@pytest.mark.parametrize("reason", ["busy", "timeout", "deadline", "count"])
def test_operational_fallbacks_preserve_same_baseline(channel, monkeypatch, reason):
    cfg, now, calls, _ = channel
    held = reason == "busy"
    if held:
        d._slots.acquire()
        d._slots.acquire()
    elif reason == "timeout":
        def fail(*args):
            raise TimeoutError("private-provider-error")
        monkeypatch.setitem(d.PROVIDERS, "nace", fail)
    elif reason == "deadline":
        now[0] += 3
    else:
        for i in range(3):
            d.decide("retrieval", {"q": i}, {"p0": {"type": "noul"}}, "u", "b", cfg)
    source = rows()
    try:
        actual, info = assess(source, cfg)
    finally:
        if held:
            d._slots.release()
            d._slots.release()
    assert actual is source and info["status"] == "unknown"
    assert info["reason"] == {"busy": "busy_fallback", "timeout": "error_fallback",
                              "deadline": "deadline_fallback", "count": "call_budget_fallback"}[reason]
    assert "private-provider-error" not in str(info)
    if reason in {"busy", "deadline"}:
        assert not calls


def test_timestamps_are_not_reinterpreted_and_cache_preserves_observation_provenance(channel):
    cfg, _, calls, _ = channel
    source = [{"memory": "The event happened last spring.", "created_at": "2026-10-08T00:00:00Z",
               "recorded_at": "spring 2023", "event_time": "April 2023"}]
    _, first = assess(source, cfg)
    _, cached = assess(source, cfg)
    record = json.loads(calls[0][1]["evidence"][0]["record"])
    assert record["recorded_at"] == "spring 2023" and record["created_at"].startswith("2026")
    assert "Never substitute" in calls[0][1]["time_semantics"]
    assert first["decision_observed_at"] == cached["decision_observed_at"]
    assert first["observed_at"] != source[0]["recorded_at"]
    assert cached["decision_status"] == "cached" and len(calls) == 1
    assert cached["budget"]["calls"] == 1 and cached["budget"]["cache_hits"] == 1


def test_assessor_requires_final_flag_and_explicit_opt_in(channel):
    cfg, _, calls, _ = channel
    source = rows()
    assert a.assess_evidence("q", source, "u", "b", cfg=cfg)[1]["reason"] == "not_final"
    cfg["tasks"].pop("evidence_assessment")
    assert assess(source, cfg)[1]["reason"] == "assessment_disabled" and not calls
    assert d.DEFAULT_TASKS["evidence_assessment"] is False
    assert d.validate({"config": {"tasks": {"evidence_assessment": True}}}) is None
    assert d.validate({"config": {"tasks": {"evidence_assessment": "true"}}}) is not None


def test_real_settings_default_assessment_off(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"decision": {"enabled": True, "config": {"api_key": "synthetic"}}}))
    monkeypatch.setenv("AIDUMEM_CONFIG_FILE", str(path))
    monkeypatch.setenv("AIDUMEI_ENGINE_MODE", "auto")
    monkeypatch.delenv("AIDUMEI_DECISION_API_KEY", raising=False)
    assert d.settings()["tasks"] == {"memory_type": True, "retrieval": True, "evidence_assessment": False}


@pytest.mark.parametrize("source", [[], [{"memory": "x" * (1024 * 1024)}],
                                   [{"memory": "x"}] * 101, [{"score": float("nan")}], [None]])
def test_empty_or_invalid_or_oversize_evidence_does_not_call(channel, source):
    actual, info = assess(source, channel[0])
    assert actual is source and info["status"] == "unknown" and not channel[2]


def test_existing_direct_support_prompt_is_not_replaced_by_set_assessment(channel):
    cfg, _, calls, _ = channel
    source = [{"memory": "Lin studied in Kyoto.", "_rerank_score": .5}]
    d.filter_evidence("Where did Mara's tutor study?", source, "u", "b")
    assert "只检查 candidates[0]" in calls[0][2]["p0"]["instructions"]
    assess(source, cfg)
    assert set(calls[1][2]) == set(a.FIELDS)
    assert d.retrieval_budget()["calls"] == 2
