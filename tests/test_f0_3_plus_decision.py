"""Decision controls, fresh evidence, actual HTTP wiring and safe fallbacks."""
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from ducky import decision as d, routes_config as routes


@pytest.fixture
def channel(tmp_path, monkeypatch):
    path = tmp_path / "models.json"
    monkeypatch.setenv("AIDUMEM_CONFIG_FILE", str(path))
    monkeypatch.setenv("AIDUMEI_ENGINE_MODE", "auto")
    monkeypatch.delenv("AIDUMEI_DECISION_API_KEY", raising=False)
    monkeypatch.setattr(routes, "_CFG_PATH", str(path))
    d._cache.clear()
    d._circuits.clear()
    d._metrics.clear()
    d.reset_telemetry()
    calls = []
    def provider(cfg, state, questions):
        calls.append((dict(cfg), state, questions))
        answers = {name: {"type": "noul", "noul": 0.9 if "June 18" in state.get("candidates", [{}])[i].get("text", "") else 0.01}
                   for i, name in enumerate(questions)} if "candidates" in state else {"type": {"type": "choice", "choice": "DECISIONS", "confidence": 0.95}}
        return {"model": d.MODEL, "answers": answers}
    monkeypatch.setitem(d.PROVIDERS, "nace", provider)
    from ducky.federation import routes as federation
    monkeypatch.setattr(federation, "_is_admin_caller", lambda caller: caller == "test-admin")
    raw = {"decision": {"enabled": True, "provider": "nace", "config": {
        "model": d.MODEL, "api_key": "synthetic-decision-key", "users": ["test-user-one"],
        "tasks": {"memory_type": True, "retrieval": True}}}, "unrelated": {"keep": True}}
    path.write_text(json.dumps(raw))
    app = FastAPI()
    routes.register_config_routes(app)
    return path, calls, TestClient(app)


def rows():
    return [{"id": "noise", "memory": "A diary about weather", "_rerank_score": 0.5},
            {"id": "answer", "memory": "Example Person birthday is June 18", "_rerank_score": 0.7}]


def test_automatic_scoped_filter_and_cache(channel):
    _, calls, _ = channel
    assert [r["id"] for r in d.filter_evidence("Example Person birthday?", rows(), "test-user-one", "work")] == ["answer"]
    assert len(calls) == 1
    assert d.telemetry()["stages"][-1]["dropped"] == 1
    d.filter_evidence("Example Person birthday?", rows(), "test-user-one", "work")
    assert len(calls) == 1 and d.telemetry()["stages"][-1]["status"] == "cached"
    d.filter_evidence("Example Person birthday?", rows(), "test-user-one", "other")
    assert len(calls) == 2


def test_specific_unknown_attribute_is_checked_despite_high_rerank(channel):
    candidates = [{"id": "noise", "memory": "Birthday is in July", "_rerank_score": .99}]
    assert d.filter_evidence("出生时间精确几点？", candidates, "test-user-one", "default") == []
    assert d.telemetry()["stages"][-1]["dropped"] == 1


def test_quote_boilerplate_is_removed_without_bypassing_original_gate(channel, monkeypatch):
    from ducky.verbatim_relevance import merge_originals
    from ducky import mem0_runtime as runtime
    def rerank(query, docs, top_n):
        runtime.restore_rerank_telemetry({"status": "ok"})
        assert query in {"合成人物", "不存在人物"}
        return [{"index": 0, "relevance_score": .95 if query == "合成人物" else .001}]
    monkeypatch.setattr(runtime, "rerank", rerank)
    hits = [{"id": "verbatim:1", "memory": "合成人物于周一完成课程", "memory_type": "VERBATIM"}]
    actual = merge_originals([], hits, "关于合成人物的原话是什么？", 3, "test-user-one", "default")
    assert len(actual) == 1 and actual[0]["_rerank_original_verified"] is True
    assert d.telemetry()["stages"][-1]["status"] == "original_bypass"
    assert merge_originals([], hits, "关于不存在人物的原话是什么？", 3, "test-user-one", "default") == []


def test_scope_mismatch_never_reaches_provider(channel):
    _, calls, _ = channel
    candidates = rows() + [{"user_id": "bob", "memory": "foreign-secret", "_rerank_score": 0.5},
                           {"metadata": {"bank_id": "other"}, "memory": "foreign-bank", "_rerank_score": 0.5}]
    assert len(d.filter_evidence("birthday", candidates, "test-user-one", "work")) == 1
    assert "foreign" not in json.dumps(calls)


def test_original_bypass_still_rejects_explicit_foreign_scope(channel):
    _, calls, _ = channel
    candidates = rows() + [{"user_id": "bob", "memory": "foreign-secret"}]
    assert len(d.filter_evidence("原话是什么", candidates, "test-user-one", "work")) == 2
    assert not calls and d.telemetry()["stages"][-1]["scope_dropped"] == 1


def test_clean_original_query_preserves_quote_quota_and_mixed_topic():
    from ducky.verbatim_relevance import original_lookup_query
    from ducky.verbatim_vault import fuse_verbatim
    assert original_lookup_query("关于合成项目的原话及总结？") == "合成项目"
    main = [{"memory": "合成项目决定迁移", "memory_type": "FACTS"}] * 3
    original = [{"memory": "合成项目决定备份以后迁移", "memory_type": "VERBATIM"}]
    result = fuse_verbatim(main, original, limit=3, query="合成项目的决定", intent_query="合成项目决定的原话")
    assert len(result) == 3 and result[-1]["memory_type"] == "VERBATIM"


@pytest.mark.parametrize("query", ["原话是什么", "逐字说一下", "原文及总结", "exact wording", "verbatim quote"])
def test_original_and_mixed_intent_bypass_new_rejection(channel, query):
    _, calls, _ = channel
    assert len(d.filter_evidence(query, rows(), "test-user-one", "work")) == 2
    assert not calls and d.telemetry()["stages"][-1]["status"] == "original_bypass"


@pytest.mark.parametrize("value", [None, True, "0.1", float("nan"), float("inf"), -1, 2])
def test_unknown_support_never_rejects(value):
    assert d.probability({"type": "noul", "noul": value}) is None


def test_partial_response_preserves_unjudged(channel, monkeypatch):
    def provider(*args):
        return {"answers": {"p0": {"type": "noul", "noul": 0.01}}}
    monkeypatch.setitem(d.PROVIDERS, "nace", provider)
    assert [r["id"] for r in d.filter_evidence("birthday", rows(), "test-user-one", "work")] == ["answer"]
    assert d.telemetry()["stages"][-1]["unknown"] == 1


def test_timeout_circuit_and_reset_telemetry(channel, monkeypatch):
    _, calls, _ = channel
    def broken(*args):
        calls.append(1)
        raise TimeoutError("synthetic-decision-key must not be echoed")
    monkeypatch.setitem(d.PROVIDERS, "nace", broken)
    for i in range(4):
        assert len(d.filter_evidence(f"q{i}", rows(), "test-user-one", "work")) == 2
    assert len(calls) == 3 and d.telemetry()["stages"][-1]["status"] == "circuit_open"
    assert "synthetic-decision-key" not in json.dumps(d.telemetry())
    d.reset_telemetry()
    assert d.telemetry() == {"stages": []}


def test_concurrency_busy_is_immediate_baseline_fallback(channel):
    _, calls, _ = channel
    d._slots.acquire()
    d._slots.acquire()
    try:
        assert len(d.filter_evidence("birthday", rows(), "test-user-one", "work")) == 2
        assert not calls and d.telemetry()["stages"][-1]["status"] == "busy_fallback"
    finally:
        d._slots.release()
        d._slots.release()


def test_local_mode_and_other_user_do_not_call(channel, monkeypatch):
    _, calls, _ = channel
    assert d.classify("private memory", "bob", "default")[0] is None
    monkeypatch.setenv("AIDUMEI_ENGINE_MODE", "local")
    assert len(d.filter_evidence("birthday", rows(), "test-user-one", "work")) == 2
    assert d.classify("private memory", "test-user-one", "work")[0] is None
    assert not calls and d.health()["decision_status"] == "blocked_by_engine_mode"


def test_config_hot_save_disables_and_rotates_cache(channel):
    path, calls, client = channel
    d.classify("Use the new release", "test-user-one", "work")
    response = client.put("/config/decision?caller=test-admin", json={"config": {"api_key": "rotated-synthetic-key"}})
    assert response.status_code == 200 and "rotated-synthetic-key" not in response.text
    assert d.classify("Use the new release", "test-user-one", "work")[0] == "DECISIONS"
    assert len(calls) == 2 and calls[-1][0]["api_key"] == "rotated-synthetic-key"
    assert client.put("/config/decision?caller=test-admin", json={"enabled": False}).status_code == 200
    assert d.classify("Use the new release", "test-user-one", "work")[0] is None
    assert len(calls) == 2 and json.loads(path.read_text())["unrelated"] == {"keep": True}


@pytest.mark.parametrize("body", [{"enabled": "true"}, {"provider": "unsupported"}, {"provider": []}, {"config": {"mode": []}},
    {"config": {"model": ""}}, {"config": {"tasks": {"delete": True}}},
    {"config": {"tasks": {"retrieval": "true"}}}, {"config": {"timeout_ms": 99999}},
    {"config": {"threshold": True}}, {"config": {"users": "test-user-one"}},
    {"config": {"openai_base_url": "http://example.invalid"}},
    {"config": {"openai_base_url": "https://user:password@example.invalid"}},
    {"config": {"api_key": ["bad"]}}, {"delete_all": True}])
def test_invalid_config_keeps_file_unchanged(channel, body):
    path, _, client = channel
    before = path.read_bytes()
    assert client.put("/config/decision?caller=test-admin", json=body).status_code == 400
    assert path.read_bytes() == before


def test_unauthorized_config_write_and_health_do_not_call(channel):
    path, calls, client = channel
    before = path.read_bytes()
    assert client.put("/config/decision", json={"enabled": False}).status_code == 403
    assert path.read_bytes() == before
    assert d.health()["decision_enabled"] is True and not calls
    assert "synthetic-decision-key" not in client.get("/config").text


def test_removed_or_corrupt_config_never_reuses_key(channel):
    path, calls, _ = channel
    d.classify("deployment", "test-user-one", "work")
    path.write_text("{bad json")
    assert d.classify("deployment", "test-user-one", "work")[0] is None
    path.unlink()
    assert d.classify("deployment", "test-user-one", "work")[0] is None
    assert len(calls) == 1


def test_type_classification_ledger_is_scoped_and_source_truthful(channel, tmp_path, monkeypatch):
    import sqlite3
    from ducky import memory_types as mt
    path = tmp_path / "facts.db"
    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn
    monkeypatch.setattr(mt, "get_facts_conn", connect)
    monkeypatch.setattr(mt, "_checked", False)
    result = mt.classify_and_record("shared-id", "Deploy in zone A", user_id="test-user-one", bank_id="work")
    assert result["memory_type"] == "DECISIONS" and result["source"] == "decision"
    assert result["confidence"] == 0.95
    other = mt.classify_and_record("shared-id", "Known fact", user_id="bob", bank_id="work")
    assert other["source"] == "rule"
    assert mt.get_memory_type("shared-id", user_id="test-user-one", bank_id="work") == "DECISIONS"


@pytest.mark.parametrize("confidence", [None, 0.69, True, float("nan")])
def test_low_or_unknown_classification_confidence_falls_back(channel, monkeypatch, confidence):
    monkeypatch.setitem(d.PROVIDERS, "nace", lambda *a: {"answers": {
        "type": {"type": "choice", "choice": "DECISIONS", "confidence": confidence}}})
    assert d.classify("deployment", "test-user-one", "work")[0] is None


@pytest.fixture
def clef_channel(channel, monkeypatch):
    path, calls, client = channel
    raw = json.loads(path.read_text())
    raw["decision"]["provider"] = "cloudflare"
    raw["decision"]["config"].update(model="clef-flash", account_id="0" * 32)
    path.write_text(json.dumps(raw))
    answers = {}

    def provider(cfg, state, questions):
        calls.append((dict(cfg), state, questions))
        return {"model": cfg["model"], "answers": dict(answers)}

    monkeypatch.setitem(d.PROVIDERS, "cloudflare", provider)
    return path, calls, answers


@pytest.mark.parametrize("model", ["clef", "clef-flash"])
def test_clef_classification_uses_registry_and_highest_noul(clef_channel, monkeypatch, model):
    from ducky import memory_types as mt
    path, calls, answers = clef_channel
    raw = json.loads(path.read_text())
    raw["decision"]["config"]["model"] = model
    path.write_text(json.dumps(raw))
    monkeypatch.setitem(mt.TYPE_LABELS, "EXTRA_TEST_LABEL", "合成扩展类别")
    answers.update({label: {"type": "noul", "noul": .71} for label in mt.TYPE_LABELS})
    answers["EXTRA_TEST_LABEL"]["noul"] = .96
    assert d.classify("x" * 4001, "test-user-one", "work") == ("EXTRA_TEST_LABEL", .96)
    cfg, state, questions = calls[0]
    assert cfg["model"] == model and state == {"text": "x" * 4000}
    assert set(questions) == set(mt.TYPE_LABELS)
    assert all(q["type"] == "noul" for q in questions.values())
    assert all(label in questions[label]["instructions"] and description in questions[label]["instructions"]
               for label, description in mt.TYPE_LABELS.items())
    assert d.telemetry()["stages"][-1]["status"] == "ok"


@pytest.mark.parametrize("score,expected", [(.6999, (None, None)), (.7, ("FACTS", .7)), (1, ("FACTS", 1.0))])
def test_clef_classification_threshold(clef_channel, score, expected):
    _, _, answers = clef_channel
    answers["FACTS"] = {"type": "noul", "noul": score}
    assert d.classify("synthetic fact", "test-user-one", "work") == expected


@pytest.mark.parametrize("invalid", [None, True, "0.99", float("nan"), float("inf"), -1, 1.1])
def test_clef_classification_rejects_invalid_scores(clef_channel, invalid):
    _, _, answers = clef_channel
    answers.update({"FACTS": {"type": "noul", "noul": invalid},
                    "PREFERENCES": {"type": "noul", "noul": .8},
                    "UNKNOWN": {"type": "noul", "noul": 1}})
    assert d.classify("synthetic preference", "test-user-one", "work") == ("PREFERENCES", .8)


@pytest.mark.parametrize("answer", [None, {}, {"type": "choice", "choice": "FACTS", "confidence": .99}])
def test_clef_classification_missing_or_wrong_type_falls_back(clef_channel, answer):
    _, _, answers = clef_channel
    answers["FACTS"] = answer
    assert d.classify("synthetic fact", "test-user-one", "work") == (None, None)


def test_clef_classification_tie_uses_registry_order(clef_channel):
    from ducky.memory_types import TYPE_LABELS
    _, _, answers = clef_channel
    answers.update({label: {"type": "noul", "noul": .9} for label in reversed(TYPE_LABELS)})
    assert d.classify("synthetic tie", "test-user-one", "work") == (next(iter(TYPE_LABELS)), .9)


def test_clef_classification_scope_and_task_switch(clef_channel):
    path, calls, _ = clef_channel
    assert d.classify("private fact", "other-user", "work") == (None, None)
    raw = json.loads(path.read_text())
    raw["decision"]["config"]["tasks"]["memory_type"] = False
    path.write_text(json.dumps(raw))
    assert d.classify("private fact", "test-user-one", "work") == (None, None)
    assert not calls


def test_clef_retrieval_retains_noul_protocol(clef_channel):
    _, calls, answers = clef_channel
    answers.update({"p0": {"type": "noul", "noul": .01}, "p1": {"type": "noul", "noul": .95}})
    assert [row["id"] for row in d.filter_evidence("birthday?", rows(), "test-user-one", "work")] == ["answer"]
    assert set(calls[0][2]) == {"p0", "p1"}
    assert all(q["type"] == "noul" for q in calls[0][2].values())
    assert d.telemetry()["stages"][-1]["status"] == "ok"


@pytest.mark.parametrize("choice", [[], {}], ids=["list", "object"])
def test_invalid_classification_choice_preserves_llm_fallback_and_ledger(channel, tmp_path, monkeypatch, choice):
    import sqlite3
    from ducky import memory_types as mt

    path = tmp_path / "facts.db"

    def connect():
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    monkeypatch.setattr(mt, "get_facts_conn", connect)
    monkeypatch.setattr(mt, "_checked", False)
    fallback_calls = []

    def fallback(text):
        fallback_calls.append(text)
        return "FACTS"

    monkeypatch.setattr(mt, "_llm_classify", fallback)
    monkeypatch.setitem(d.PROVIDERS, "nace", lambda *a: {"model": d.MODEL, "answers": {
        "type": {"type": "choice", "choice": choice, "confidence": .99}}})

    result = mt.classify_and_record("synthetic-ref", "Known synthetic fact", use_llm=True,
                                    user_id="test-user-one", bank_id="work")
    assert result["memory_type"] == "FACTS" and result["source"] == "llm"
    assert fallback_calls == ["Known synthetic fact"]
    assert mt.get_memory_type("synthetic-ref", user_id="test-user-one", bank_id="work") == "FACTS"


def test_filter_precedes_final_limit_and_retains_rerank_order(channel, monkeypatch):
    from ducky import scoring, mem0_runtime as runtime
    monkeypatch.setattr(scoring, "get_batch_salience_records", lambda *a: {})
    monkeypatch.setattr(scoring, "_load_type_map", lambda *a: {})
    monkeypatch.setattr(scoring, "_load_epi_map", lambda *a: {})
    monkeypatch.setattr(scoring, "_load_credit_map", lambda *a: {})
    monkeypatch.setattr(runtime, "rerank", lambda *a, **k: [{"index": 0, "relevance_score": .7}, {"index": 1, "relevance_score": .6}])
    candidates = [{"id": "noise", "memory": "Weather diary", "score": .8},
                  {"id": "answer", "memory": "Example Person birthday is June 18", "score": .7}]
    actual = scoring.score_and_rank_candidates("Example Person birthday", candidates, user_id="test-user-one", bank_id="work", limit=1)
    assert len(actual) == 1 and actual[0]["id"] == "answer"


def test_actual_search_http_cannot_bypass_changed_policy_in_workspace(channel, monkeypatch):
    from ducky.hot import search as route
    from ducky import memory_workspace as ws, verbatim_vault as vault
    monkeypatch.setattr(route, "get_memory", lambda: object())
    monkeypatch.setattr(route, "ensure_bank_registered", lambda *a: None)
    monkeypatch.setattr(ws, "ws_lookup", lambda *a, **k: [{"id": "stale", "memory": "Weather", "score": .9}])
    monkeypatch.setattr(ws, "ws_feed_from_results", lambda *a, **k: None)
    monkeypatch.setattr(route, "boost_salience_for_results", lambda *a: None)
    monkeypatch.setattr(route, "_annotate_memory_types", lambda *a, **k: None)
    def hybrid(*a, **k):
        return d.filter_evidence("birthday", rows(), "test-user-one", "work")
    monkeypatch.setattr(route, "lazy_import_hybrid", lambda: hybrid)
    monkeypatch.setattr(vault, "verbatim_search", lambda *a, **k: [])
    app = FastAPI()
    route.register_search_routes(app)
    response = TestClient(app).post("/search", json={"query": "birthday", "user_id": "test-user-one", "bank_id": "work"}).json()
    assert response["status"] == "ok" and response.get("_workspace_hit") is not True
    assert response["_decision"]["stages"][-1]["dropped"] == 1


def test_adapter_rejects_redirect_model_change_and_invalid_answers(channel, monkeypatch):
    import requests
    class Reply:
        status_code = 302
        def json(self):
            return {"model": "drex-latest", "answers": {}}
    seen = []
    def post(*a, **k):
        seen.append(k)
        return Reply()
    monkeypatch.setattr(requests, "post", post)
    cfg = d.settings()
    with pytest.raises(ValueError):
        d._nace(cfg, {}, {})
    Reply.status_code = 200
    with pytest.raises(ValueError):
        d._nace(cfg, {}, {})
    assert seen[0]["allow_redirects"] is False and seen[0]["timeout"] == (2.0, 2.0)


@pytest.mark.parametrize("provider,model,url", [
    ("nace", "drex-v1.5", "https://drex.nace.ai/v1"),
    ("typesafe", "jev-1.13.0", "https://api.typesafe.ai/v1"),
    ("systemone", "customer-model-v2", "https://example.invalid/v1"),
])
def test_customer_provider_selection_uses_scoped_contract(channel, monkeypatch, provider, model, url):
    import requests
    path, _, _ = channel
    raw = json.loads(path.read_text())
    raw["decision"] = {"enabled": True, "provider": provider, "config": {
        "api_key": "selected-synthetic-key", "model": model, "openai_base_url": url}}
    path.write_text(json.dumps(raw))
    seen = []
    class Reply:
        status_code = 200
        def json(self):
            return {"model": model, "answers": {"type": {
                "type": "choice", "choice": "DECISIONS", "confidence": .95}}}
    def post(endpoint, **kwargs):
        seen.append((endpoint, kwargs))
        return Reply()
    monkeypatch.setattr(requests, "post", post)
    # Restore actual transport, since channel mocks the Nace provider.
    monkeypatch.setitem(d.PROVIDERS, provider, d._systemone)
    assert d.classify("Example project decision", "test-user-one", "work") == ("DECISIONS", .95)
    assert seen[0][0] == url + "/systemone"
    assert seen[0][1]["json"]["model"] == model
    assert d.health()["decision_provider"] == provider
    assert "selected-synthetic-key" not in json.dumps(d.telemetry())


@pytest.mark.parametrize("requested,actual,accepted", [
    ("jev-latest", "jev-1.13.0", True),
    ("drex-latest", "drex-v1.5", True),
    ("jev-preview", "jev-1.14.0", True),
    ("jev-latest", "drex-v1.5", False),
    ("jev-1.13.0", "jev-1.14.0", False),
    ("drex-v1.5", "drex-latest", False),
])
def test_model_alias_resolution_does_not_weaken_version_pins(channel, monkeypatch, requested, actual, accepted):
    import requests
    class Reply:
        status_code = 200
        def json(self):
            return {"model": actual, "answers": {}}
    monkeypatch.setattr(requests, "post", lambda *a, **k: Reply())
    cfg = {**d.settings(), "model": requested}
    if accepted:
        assert d._systemone(cfg, {}, {})["model"] == actual
    else:
        with pytest.raises(ValueError):
            d._systemone(cfg, {}, {})


@pytest.mark.parametrize("config", [{}, {"model": "customer-v2"},
    {"openai_base_url": "https://example.invalid/v1"},
    {"model": [], "openai_base_url": "https://example.invalid/v1"},
    {"model": "customer-v2", "openai_base_url": "https://example.invalid:bad/v1"}])
def test_custom_protocol_requires_explicit_valid_model_and_endpoint(config):
    assert d.validate({"provider": "systemone", "config": config}) is not None


@pytest.mark.parametrize("body", [
    {"provider": "typesafe"},
    {"provider": "typesafe", "config": {"model": "jev-1.13.0", "openai_base_url": "https://api.typesafe.ai/v1"}},
    {"config": {"openai_base_url": "https://example.invalid/v1", "api_key": ""}},
])
def test_provider_switch_never_silently_forwards_old_key(channel, body):
    path, _, client = channel
    before = path.read_bytes()
    assert client.put("/config/decision?caller=test-admin", json=body).status_code == 400
    assert path.read_bytes() == before


def test_explicit_provider_switch_and_model_edit_are_hot_applied(channel):
    path, _, client = channel
    cfg = {"model": "jev-1.13.0", "openai_base_url": "https://api.typesafe.ai/v1", "api_key": "new-synthetic-key"}
    assert client.put("/config/decision?caller=test-admin", json={"provider": "typesafe", "config": cfg}).status_code == 200
    assert d.settings()["provider"] == "typesafe" and d.settings()["model"] == "jev-1.13.0"
    assert client.put("/config/decision?caller=test-admin", json={"config": {"model": "jev-latest"}}).status_code == 200
    assert d.settings()["model"] == "jev-latest"
    assert json.loads(path.read_text())["decision"]["config"]["api_key"] == "new-synthetic-key"


def test_failure_circuits_are_bounded_across_configuration_changes(channel, monkeypatch):
    def broken(*args):
        raise TimeoutError()
    monkeypatch.setitem(d.PROVIDERS, "nace", broken)
    for i in range(40):
        cfg = {**d.settings(), "model": f"synthetic-v{i}"}
        d.decide("memory_type", {}, {}, "test-user-one", "work", cfg)
    assert len(d._circuits) == 32


def test_environment_key_override_blocks_destination_switch(channel, monkeypatch):
    path, _, client = channel
    before = path.read_bytes()
    monkeypatch.setenv("AIDUMEI_DECISION_API_KEY", "environment-synthetic-key")
    cfg = {"model": "jev-1.13.0", "openai_base_url": "https://api.typesafe.ai/v1", "api_key": "new-synthetic-key"}
    assert client.put("/config/decision?caller=test-admin", json={"provider": "typesafe", "config": cfg}).status_code == 400
    assert path.read_bytes() == before


def test_cloudflare_validation_requires_account_and_supported_model():
    base = {"provider": "cloudflare", "config": {
        "model": "clef", "openai_base_url": "https://api.cloudflare.com/client/v4",
        "api_key": "synthetic-cloudflare-key"}}
    assert d.validate(base) is not None
    base["config"]["account_id"] = "0123456789abcdef0123456789abcdef"
    assert d.validate(base) is None
    base["config"]["model"] = "clef-unknown"
    assert d.validate(base) is not None


def test_cloudflare_adapter_builds_scoped_url_and_unwraps_result(channel, monkeypatch):
    import requests
    seen = []

    class Reply:
        status_code = 200

        def json(self):
            return {"success": True, "result": {
                "model": "clef-flash",
                "answers": {"p0": {"type": "noul", "noul": 0.91}},
                "usage": {"input_tokens": 7},
            }}

    def post(endpoint, **kwargs):
        seen.append((endpoint, kwargs))
        return Reply()

    monkeypatch.setattr(requests, "post", post)
    cfg = {"provider": "cloudflare", "model": "clef-flash",
           "openai_base_url": "https://api.cloudflare.com/client/v4",
           "account_id": "0123456789abcdef0123456789abcdef",
           "api_key": "synthetic-cloudflare-key", "timeout_ms": 2000}
    result = d._cloudflare(cfg, {"query": "生日"}, {"p0": {"type": "noul"}})
    assert result["answers"]["p0"]["noul"] == .91
    assert result["usage"]["input_tokens"] == 7
    assert seen[0][0].endswith("/accounts/0123456789abcdef0123456789abcdef/ai/run/@cf/cloudflare/clef-flash")
    assert seen[0][1]["json"]["model"] == "clef-flash"
    assert seen[0][1]["json"]["state"] == {"query": "生日"}
    assert seen[0][1]["allow_redirects"] is False
    assert "synthetic-cloudflare-key" not in json.dumps(d.telemetry())


def test_cloudflare_adapter_rejects_failed_envelope_and_model_swap(channel, monkeypatch):
    import requests

    class Reply:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    cfg = {"provider": "cloudflare", "model": "clef",
           "openai_base_url": "https://api.cloudflare.com/client/v4",
           "account_id": "0123456789abcdef0123456789abcdef",
           "api_key": "synthetic-cloudflare-key", "timeout_ms": 2000}
    monkeypatch.setattr(requests, "post", lambda *a, **k: Reply({"success": False, "errors": [{"code": 1}]}))
    with pytest.raises(ValueError):
        d._cloudflare(cfg, {}, {"p0": {"type": "noul"}})
    monkeypatch.setattr(requests, "post", lambda *a, **k: Reply({"success": True, "result": {
        "model": "clef-flash", "answers": {}}}))
    with pytest.raises(ValueError):
        d._cloudflare(cfg, {}, {"p0": {"type": "noul"}})


def test_cloudflare_config_view_masks_key_and_exposes_account_id(channel):
    path, _, client = channel
    raw = json.loads(path.read_text())
    raw["decision"] = {"enabled": True, "provider": "cloudflare", "config": {
        "model": "clef", "openai_base_url": "https://api.cloudflare.com/client/v4",
        "account_id": "0123456789abcdef0123456789abcdef",
        "api_key": "synthetic-cloudflare-key"}}
    path.write_text(json.dumps(raw))
    view = client.get("/config").json()["decision"]
    assert view["provider"] == "cloudflare"
    assert view["config"]["account_id"] == "0123456789abcdef0123456789abcdef"
    assert view["config"]["api_key"] != "synthetic-cloudflare-key"
    assert "synthetic-cloudflare-key" not in client.get("/config").text


def test_decision_ui_contains_cloudflare_and_task_controls():
    source = open("frontend/js/panels.js", encoding="utf-8").read()
    for marker in ("DECISION MODEL", "edDecProvider", "cloudflare", "edDecAccount",
                   "edDecMemoryType", "edDecRetrieval"):
        assert marker in source
