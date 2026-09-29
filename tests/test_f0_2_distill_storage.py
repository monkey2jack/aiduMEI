"""A session summary must survive independently of its source facts."""
import pytest


SUMMARY = {
    "kind": "session_distill", "lane": "distill",
    "_origin_agent": "session-distill", "_origin_session_id": "session-1",
    "distill_mode": "llm",
}


@pytest.mark.parametrize("infer", [False, True])
@pytest.mark.parametrize("collision", ["none", "semantic", "exact"])
def test_summary_is_added_without_reextracting_or_replacing_facts(monkeypatch, infer, collision):
    import ducky.layer1_selfcheck as layer
    import ducky.self_edit as edit

    calls = []
    indexed = []
    old_fact = {"text": "The team chose the blue design.", "kind": "fact"}

    class Memory:
        def add(self, messages, **kwargs):
            calls.append((messages, kwargs))
            # Model the real extraction contract: already-known facts can yield NONE.
            return {"results": [] if kwargs["infer"] else [
                {"id": "summary-1", "memory": messages, "event": "ADD"}]}

        def update(self, ref, text, metadata):
            old_fact.update(text=text, kind=metadata.get("kind"))

    def self_edit(*args, **kwargs):
        return {"action": "duplicate", "memory_id": "fact-1"} if collision == "semantic" else None

    monkeypatch.setattr(edit, "self_edit_on_add", self_edit)
    monkeypatch.setattr(layer, "dedup_check", lambda *a, **k: "fact-1" if collision == "exact" else None)
    monkeypatch.setattr(layer, "check_capacity", lambda *a, **k: {"needs_merge": False})
    monkeypatch.setattr(layer, "track_knowledge_evolution", lambda *a, **k: None)
    monkeypatch.setattr(layer, "_sync_indexes_after_update", lambda *a, **k: None)
    monkeypatch.setattr(layer, "_index_after_add", lambda result, **kwargs: indexed.extend(result["results"]))
    out = layer.layer1_add_wrapper(Memory(), old_fact["text"], "alice", SUMMARY,
                                   bank_id="work", infer=infer)
    assert out["status"] == "ok"
    assert len(calls) == 1
    assert calls[0][1]["infer"] is False
    assert calls[0][1]["metadata"]["bank_id"] == "work"
    assert [row["id"] for row in indexed] == ["summary-1"]
    assert old_fact == {"text": "The team chose the blue design.", "kind": "fact"}


@pytest.mark.parametrize("mode", ["llm", "fallback"])
def test_summary_direct_write_keeps_derived_provenance(monkeypatch, mode):
    import ducky.epistemic as epistemic
    import ducky.layer1_selfcheck as layer

    stamps = []
    monkeypatch.setattr(epistemic, "stamp_memory_refs", lambda refs, kind, **kw: stamps.append((refs, kind, kw)))
    layer._index_after_add({"results": [{"id": "summary-1"}]}, user_id="alice",
                           bank_id="work", infer=False,
                           metadata={**SUMMARY, "distill_mode": mode})
    assert stamps[0][1] == "reasoned"
    assert stamps[0][2]["origin"][:2] == ("session-distill", "session-1")
