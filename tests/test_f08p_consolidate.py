"""f0.8+ nightly consolidation: same-subject grouping, supersede links, recall filter, restore."""
from __future__ import annotations

import json
import sqlite3
import threading

import numpy as np
import pytest

from ducky import consolidate as C
from ducky.memory_kernel.impl_native import connect as native_connect


def _vec(*hot: float) -> bytes:
    v = np.zeros(16, dtype="<f4")
    for i, x in enumerate(hot):
        v[i] = x
    return v.tobytes()


class _Mem:
    def __init__(self, conn):
        self.conn = conn
        self._lock = threading.Lock()


@pytest.fixture()
def dbs(tmp_path):
    m = native_connect(str(tmp_path / "memory.db"))
    f = sqlite3.connect(":memory:")
    yield m, f
    m.close()
    f.close()


def _add(m, mid, text, when, vec, md=None, user="u1"):
    m.execute("INSERT INTO memories (mid,id,content,user_id,bank_id,hash,metadata,created_at) VALUES (?,?,?,?,?,?,?,?)",
              (mid, f"m{mid}", text, user, "default", f"h{mid}", json.dumps(md or {}), when))
    m.execute("INSERT INTO memory_vectors (mid,dim,embedding) VALUES (?,?,?)", (mid, 16, vec))
    m.commit()


def _llm_pairs(pairs):
    """Fake judge: a reasoning model that thinks out loud before its JSON."""
    def llm(prompt, **_kw):
        body = json.dumps({"superseded": [{"old": o, "new": n, "relation": r, "why": "x"} for o, n, r in pairs]})
        return 'We need to decide. Draft: {"superseded": []} hmm, let me check again.\n' + body
    return llm


# ── judge output parsing ──────────────────────────────────────────────────
def test_final_json_takes_last_object_with_key_after_reasoning():
    raw = 'We need to output JSON like {"x": 1}. Final: {"superseded": [{"old": "A1"}]} trailing {"y": 2}'
    assert C.final_json(raw, "superseded") == {"superseded": [{"old": "A1"}]}
    assert C.final_json("<think>{\"superseded\": [1]}</think>{\"superseded\": []}", "superseded") == {"superseded": []}
    assert C.final_json("no json at all", "superseded") is None
    assert C.final_json(None) is None


def test_event_ts_uses_latest_date_inside_bridged_document():
    created = C._ts("2026-11-09T20:00:00+00:00")
    md = {"cc_file": "x.md"}
    text = "2026-10-18 rule set\n### 2026-11-08 later change"
    ts = C.event_ts(text, md, created)
    assert C._day(ts) == "2026-11-08"
    assert C.event_ts(text, {}, created) == created            # chat rows keep created_at
    assert C.event_ts("2027-01-01 future", md, created) == created   # dates after import are ignored


# ── run: duplicates, changes, guards ──────────────────────────────────────
def test_exact_duplicates_collapse_by_rule_keeping_newest(dbs):
    m, f = dbs
    _add(m, 1, "同一句话", "2026-11-01T01:00:00+00:00", _vec(1))
    _add(m, 2, "同一句话", "2026-11-02T01:00:00+00:00", _vec(0, 1))
    rep = C.run_scope(m, f, "u1", mode="shadow", use_llm=False)
    assert rep["links_duplicate"] == 1 and not rep.get("error")
    assert f.execute("SELECT superseded_id, current_id, kind, judge, state FROM consolidation_links").fetchall() == \
        [("m1", "m2", "duplicate", "rule", "shadow")]


@pytest.mark.parametrize("first,second", [
    ("Service ID: ABC", "Service ID: abc"),
    ("if x:\n    run()", "if x:\nrun()"),
])
def test_exact_duplicate_rule_preserves_case_and_whitespace(dbs, first, second):
    m, f = dbs
    _add(m, 1, first, "2026-11-01T01:00:00+00:00", _vec(1))
    _add(m, 2, second, "2026-11-02T01:00:00+00:00", _vec(1))
    result = C.run_scope(m, f, "u1", mode="on", use_llm=False)
    assert result["links_duplicate"] == 0
    assert f.execute("SELECT COUNT(*) FROM consolidation_links").fetchone()[0] == 0


@pytest.mark.parametrize("first,second", [
    ("Service ID: ABC", "Service ID: abc"),
    ("if x:\n    run()", "if x:\nrun()"),
])
def test_model_duplicate_cannot_override_normalization_difference(dbs, first, second):
    m, f = dbs
    _add(m, 1, first, "2026-11-01T01:00:00+00:00", _vec(1))
    _add(m, 2, second, "2026-11-02T01:00:00+00:00", _vec(1))
    result = C.run_scope(m, f, "u1", mode="on", llm=_llm_pairs([("A1", "A2", "duplicate")]))
    assert result["llm_calls"] == 1
    assert result["guard_reasons"] == {"normalization_only": 1}
    assert result["links_duplicate"] == 0


def test_duplicate_prefers_what_was_said_over_a_summary(dbs):
    m, f = dbs
    _add(m, 1, "原话内容一模一样", "2026-11-01T01:00:00+00:00", _vec(1))
    _add(m, 2, "原话内容一模一样", "2026-11-02T01:00:00+00:00", _vec(1), md={"kind": "session_distill"})
    C.run_scope(m, f, "u1", mode="shadow", use_llm=False)
    assert f.execute("SELECT superseded_id, current_id FROM consolidation_links").fetchall() == [("m2", "m1")]


def test_llm_update_links_older_to_newer_and_guards_hold(dbs):
    m, f = dbs
    _add(m, 1, "简报卡片发到私密 webhook 群", "2026-10-30T01:00:00+00:00", _vec(1, 0.1))
    _add(m, 2, "简报卡片改回测试主体5私聊直发，webhook 群废除", "2026-11-06T01:00:00+00:00", _vec(1, 0.2))
    rep = C.run_scope(m, f, "u1", mode="on", llm=_llm_pairs([("A1", "A2", "update")]))
    assert rep["llm_calls"] == 1 and rep["links_changed"] == 1
    assert f.execute("SELECT superseded_id, current_id, kind, state FROM consolidation_links").fetchall() == \
        [("m1", "m2", "changed", "superseded")]
    # lineage written for the superseded original, original text untouched
    assert m.execute("SELECT content FROM memories WHERE id='m1'").fetchone()[0].startswith("简报卡片发到")
    assert f.execute("SELECT action, actor FROM memory_lineage WHERE memory_id='m1'").fetchall() == \
        [("SUPERSEDE", "consolidate")]


@pytest.mark.parametrize("pairs, why", [
    ([("A2", "A1", "update")], "not_newer"),
    ([("A1", "A3", "update")], "new_is_question"),
])
def test_guards_refuse_bad_updates(dbs, pairs, why):
    m, f = dbs
    _add(m, 1, "定时任务主模型是 Gemini", "2026-10-18T01:00:00+00:00", _vec(1, 0.1))
    _add(m, 2, "定时任务主模型临时换成 GLM", "2026-11-10T01:00:00+00:00", _vec(1, 0.2))
    _add(m, 3, "定时任务现在用什么模型吗？", "2026-11-11T01:00:00+00:00", _vec(1, 0.3))
    rep = C.run_scope(m, f, "u1", mode="shadow", llm=_llm_pairs(pairs))
    assert rep["links_changed"] == 0 and rep["guard_reasons"].get(why) == 1


def test_inferred_never_supersedes_direct(dbs):
    m, f = dbs
    _add(m, 1, "测试主体5说首页第五张卡片是时代智汇", "2026-11-04T01:00:00+00:00", _vec(1, 0.1))
    _add(m, 2, "摘要：首页卡片顺序调整过", "2026-11-05T01:00:00+00:00", _vec(1, 0.2), md={"kind": "session_distill"})
    rep = C.run_scope(m, f, "u1", mode="shadow", llm=_llm_pairs([("A1", "A2", "update")]))
    assert rep["guard_reasons"] == {"inferred_over_direct": 1}


def test_model_duplicate_must_be_a_real_copy(dbs):
    m, f = dbs
    _add(m, 1, "main 上怎么有变化了？测试主体4检查一下", "2026-11-11T01:00:00+00:00", _vec(1, 0.1))
    _add(m, 2, "查出两个实锤：分支早就合进 main，今天多了 20 个提交", "2026-11-11T02:00:00+00:00", _vec(1, 0.2))
    rep = C.run_scope(m, f, "u1", mode="shadow", llm=_llm_pairs([("A1", "A2", "duplicate")]))
    assert rep["links_duplicate"] == 0 and rep["guard_reasons"] == {"not_a_copy": 1}


def test_llm_failure_counts_and_writes_nothing(dbs):
    m, f = dbs
    _add(m, 1, "第一条关于部署的记录", "2026-11-01T01:00:00+00:00", _vec(1, 0.1))
    _add(m, 2, "第二条关于部署的记录", "2026-11-02T01:00:00+00:00", _vec(1, 0.2))
    rep = C.run_scope(m, f, "u1", mode="on", llm=lambda *_a, **_k: None)
    assert rep["llm_failed"] == 1 and rep["links_changed"] == 0 and not rep.get("error")


def test_incremental_watermark_only_looks_at_new_rows(dbs):
    m, f = dbs
    _add(m, 1, "旧记录甲", "2026-11-01T01:00:00+00:00", _vec(1))
    _add(m, 2, "旧记录乙", "2026-11-02T01:00:00+00:00", _vec(0, 1))
    assert C.run_scope(m, f, "u1", mode="shadow", use_llm=False)["new_rows"] == 2
    assert C.run_scope(m, f, "u1", mode="shadow", use_llm=False)["new_rows"] == 0
    _add(m, 3, "旧记录甲", "2026-11-03T01:00:00+00:00", _vec(1))
    rep = C.run_scope(m, f, "u1", mode="shadow", use_llm=False)
    assert rep["new_rows"] == 1 and rep["links_duplicate"] == 1


# ── recall side ───────────────────────────────────────────────────────────
def test_recall_filter_only_in_mode_on_and_only_while_current_exists(dbs, monkeypatch):
    m, f = dbs
    _add(m, 1, "旧版本", "2026-11-01T01:00:00+00:00", _vec(1))
    _add(m, 2, "旧版本", "2026-11-02T01:00:00+00:00", _vec(1))
    C.run_scope(m, f, "u1", mode="on", use_llm=False)
    cands = [{"id": "m1"}, {"id": "m2"}]
    mem = _Mem(m)
    monkeypatch.setenv("AIDUMEI_CONSOLIDATE_MODE", "shadow")
    assert C.superseded_ids(["m1", "m2"], "u1", mem=mem, fconn=f) == set()
    monkeypatch.setenv("AIDUMEI_CONSOLIDATE_MODE", "on")
    assert C.superseded_ids(["m1", "m2"], "u1", mem=mem, fconn=f) == {"m1"}
    assert C.superseded_ids(["m1"], "other-user", mem=mem, fconn=f) == set()      # scoped
    m.execute("DELETE FROM memories WHERE id='m2'")
    m.commit()
    assert C.superseded_ids(["m1"], "u1", mem=mem, fconn=f) == set()   # current gone → old re-surfaces
    assert C.drop_superseded(cands, "u1", mem=None) == cands           # no native conn → hide nothing


def test_restore_is_sticky(dbs):
    m, f = dbs
    _add(m, 1, "同一件事", "2026-11-01T01:00:00+00:00", _vec(1))
    _add(m, 2, "同一件事", "2026-11-02T01:00:00+00:00", _vec(1))
    C.run_scope(m, f, "u1", mode="on", use_llm=False)
    assert C.restore(f, "u1", "default", "m1") == 1
    C.run_scope(m, f, "u1", mode="on", use_llm=False, full=True)
    assert f.execute("SELECT state FROM consolidation_links WHERE superseded_id='m1'").fetchall() == [("restored",)]


def test_mode_switch_and_legacy_loops(monkeypatch):
    monkeypatch.delenv("AIDUMEI_CONSOLIDATE_MODE", raising=False)
    assert C.consolidate_mode() == "off" and not C.replaces_legacy_loops()
    monkeypatch.setenv("AIDUMEI_CONSOLIDATE_MODE", "bogus")
    assert C.consolidate_mode() == "off"
    monkeypatch.setenv("AIDUMEI_CONSOLIDATE_MODE", "on")
    assert not C.replaces_legacy_loops()  # unfinished replacement must not silence legacy work
    monkeypatch.setenv("AIDUMEI_CONSOLIDATE_MODE", "shadow")
    assert not C.replaces_legacy_loops()


# ── insights gate ─────────────────────────────────────────────────────────
def test_insight_needs_two_sessions_on_two_days(monkeypatch, tmp_path):
    rows = {"a": {"id": "a", "session": "s1", "day": "2026-11-01"},
            "b": {"id": "b", "session": "s1", "day": "2026-11-02"},
            "c": {"id": "c", "session": "s2", "day": "2026-11-01"},
            "d": {"id": "d", "session": "s3", "day": "2026-11-03"}}
    assert not C.insight_eligible(["a", "b"], rows)        # one session
    assert not C.insight_eligible(["a", "c"], rows)        # one day
    assert not C.insight_eligible(["m1", "m3"], rows)      # prompt-local refs resolve to nothing
    assert C.insight_eligible(json.dumps(["a", "d"]), rows)
    assert C.insight_eligible([], rows, confirmed=True)
    rows["e"] = {"id": "e", "session": "", "day": "2026-11-04"}
    assert not C.insight_eligible(["a", "e"], rows)  # unknown session is not independent evidence
    assert not C.insight_eligible(["a", "a", "e"], rows)
    assert not C.insight_eligible("42", rows)
    rows["fact:1"] = {"id": "fact:1", "session": "s9", "day": "2026-11-09"}
    assert not C.insight_eligible(["a", "fact:1"], rows)  # fact references are not memory IDs

    # Exercise the real reflect flow: scoped prompt references become stable IDs,
    # and on mode rejects a model claim backed by only one session.
    from ducky import reflect
    from ducky.memory_kernel.impl_native import NativeMemory
    native = NativeMemory(path=str(tmp_path / "real-native.db"))
    try:
        for ident, date, session in (("a", "2026-11-01T01:00:00Z", "s1"),
                                     ("d", "2026-11-03T01:00:00Z", "s3")):
            native.insert(ident, "scoped source " + ident, user_id="u1", vector=[0.1] * 16,
                          metadata={"_origin_session_id": session}, created_at=date, history=False)
        gathered = reflect._gather_recent_memories(native, "u1", 10, bank_id="default")
        assert {(x["id"], x["source_user"], x["session"], x["day"]) for x in gathered} == {
            ("a", "u1", "s1", "2026-11-01"), ("d", "u1", "s3", "2026-11-03")}
    finally:
        native.conn.close()
    memories = [
        {"ref": "m1", "id": "a", "text": "一", "created_at": "2026-11-01", "session": "s1", "day": "2026-11-01", "source_user": "u1"},
        {"ref": "m2", "id": "d", "text": "二", "created_at": "2026-11-03", "session": "s3", "day": "2026-11-03", "source_user": "u1"},
        {"ref": "m3", "id": "b", "text": "三", "created_at": "2026-11-02", "session": "s1", "day": "2026-11-02", "source_user": "u1"},
        {"ref": "m4", "id": "x", "text": "FOREIGN_OWNER_PRIVATE", "created_at": "2026-11-04", "session": "s4", "day": "2026-11-04", "source_user": "default"},
    ]
    claims = [{"type": "pattern", "content": "supported", "evidence": ["m1", "m2"]},
              {"type": "pattern", "content": "one session", "evidence": ["m1", "m3"]}]
    monkeypatch.setattr(reflect, "ensure_reflect_schema", lambda: None)
    monkeypatch.setattr(reflect, "_gather_recent_memories", lambda *a, **k: memories)
    monkeypatch.setattr(reflect, "_gather_recent_facts", lambda *a, **k: [])
    prompts = []
    monkeypatch.setattr(reflect, "call_llm", lambda prompt, **k: prompts.append(prompt) or json.dumps(claims))
    written = []
    monkeypatch.setattr(reflect, "save_insights", lambda items, *a, **k: written.extend(items) or len(items))
    fields = reflect._memory_source_fields({"user_id": "u1", "created_at": "2026-11-01T01:00:00Z", "metadata": {"_origin_session_id": "s1"}}, "text")
    assert fields == {"session": "s1", "day": "2026-11-01", "source_user": "u1"}
    monkeypatch.setenv("AIDUMEI_CONSOLIDATE_MODE", "on")
    result = reflect.run_reflect(memory=object(), user_id="u1", bank_id="default", save=True)
    assert [x["content"] for x in result["insights"]] == ["supported"]
    assert result["insights"][0]["evidence"] == ["a", "d"]
    assert [x["content"] for x in written] == ["supported"]
    assert result["source_gate"] == {"checked": 2, "accepted": 1, "blocked": 1}
    assert "FOREIGN_OWNER_PRIVATE" not in prompts[0]
    monkeypatch.setattr(reflect, "_gather_recent_memories", lambda *a, **k: memories[:1])
    calls_before = len(prompts)
    insufficient = reflect.run_reflect(memory=object(), user_id="u1", bank_id="default", save=False)
    assert not insufficient["llm_used"] and not insufficient["insights"]
    assert insufficient["source_gate"]["reason"] == "insufficient_independent_sources"
    assert len(prompts) == calls_before
    monkeypatch.setattr(reflect, "_gather_recent_memories", lambda *a, **k: memories)
    monkeypatch.setenv("AIDUMEI_CONSOLIDATE_MODE", "off")
    result = reflect.run_reflect(memory=object(), user_id="u1", bank_id="default", save=False)
    assert len(result["insights"]) == 2


def test_failed_judgement_does_not_skip_rows_on_the_next_night(dbs):
    m, f = dbs
    _add(m, 1, "服务当前的主题设置为蓝色", "2026-11-01T01:00:00Z", _vec(1))
    _add(m, 2, "服务当前的主题设置为绿色", "2026-11-02T01:00:00Z", _vec(1))
    first = C.run_scope(m, f, "u1", mode="shadow", llm=lambda *_a, **_kw: None)
    assert first["llm_failed"] == 1
    second = C.run_scope(m, f, "u1", mode="shadow", llm=_llm_pairs([("A1", "A2", "update")]))
    assert second["watermark_from"] == 0 and second["new_rows"] == 2
    assert second["links_changed"] == 1


def test_insight_audit_does_not_read_another_users_evidence(dbs):
    m, f = dbs
    _add(m, 1, "owner first observation", "2026-11-01T01:00:00Z", _vec(1), {"_origin_session_id": "s1"})
    _add(m, 2, "owner second observation", "2026-11-02T01:00:00Z", _vec(1), {"_origin_session_id": "s2"})
    f.execute("CREATE TABLE reflections (user_id TEXT, evidence TEXT)")
    f.executemany("INSERT INTO reflections VALUES (?,?)", [("u1", '["m1", "m2"]'), ("u2", '["m1", "m2"]')])
    assert C.audit_insights(f, C.load_rows(m, "u1", "default"), "u1") == {
        "total": 1, "eligible": 1, "resolvable": 1}

    f = sqlite3.connect(":memory:")
    f.execute("CREATE TABLE reflections (user_id TEXT, bank_id TEXT, evidence TEXT)")
    f.executemany("INSERT INTO reflections VALUES (?,?,?)", [("u1", "default", "[]"), ("u1", "work", "[]"), ("u2", "default", "[]")])
    assert C.audit_insights(f, [], "u1", "default")["total"] == 1
    assert C.audit_insights(f, [], "u1", "work")["total"] == 1
    assert C.audit_insights(f, [], "u1", "private")["total"] == 0
    f.execute("DROP TABLE reflections")
    f.execute("CREATE TABLE reflections (user_id TEXT, evidence TEXT)")
    f.execute("INSERT INTO reflections VALUES (?,?)", ("u1", "[]"))
    assert C.audit_insights(f, [], "u1", "default")["total"] == 1
    assert C.audit_insights(f, [], "u1", "work")["total"] == 0
    f.close()
