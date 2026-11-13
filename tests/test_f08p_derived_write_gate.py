"""f0.8+ plan ⑤: server-generated writes (instinct graduation skill, self_edit merge) pass the same
write gate as /add — injection guard, P7 redaction / whole-write refusal, P1 / P8 tombstone."""
from __future__ import annotations

import pytest

from write_path_fixtures import isolated_write_stores as _isolated_write_stores  # noqa: F401 -- opt-in fixture

pytestmark = pytest.mark.usefixtures("isolated_write_stores")

SECRET = "sk-ant-api03-Zq8vT3mK9pL2xW7nR4bY6cJ1abcdefgh"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDUMEM_DATA_DIR", str(tmp_path))
    for k in ("AIDUMEI_WRITE_POLICY", "AIDUMEI_WRITE_POLICY_P1", "AIDUMEI_WRITE_POLICY_P8",
              "AIDUMEI_WRITE_POLICY_THINKING"):
        monkeypatch.delenv(k, raising=False)


# ── the shared helper ─────────────────────────────────────────────────────
def test_gate_derived_write_shapes():
    from ducky import write_policy as wp
    md = {}
    text, rec = wp.gate_derived_write(f"部署时用的密钥是 {SECRET}，记得轮换", md, user_id="alice", bank_id="default")
    assert text and SECRET not in text and "[redacted:api_key]" in text and rec == []
    assert md["redacted"] == ["api_key"]
    assert wp.gate_derived_write(SECRET, {}, user_id="alice", bank_id="default")[0] is None   # secret only
    text, rec = wp.gate_derived_write("[SILENT]", {}, user_id="alice", bank_id="default")
    assert text is None and rec[0]["principle"] == "P1" and rec[0]["tombstone_id"]
    wp.add_rule("alice", "default", "青石巷")
    text, rec = wp.gate_derived_write("用户以前住在青石巷", {}, user_id="alice", bank_id="default")
    assert text is None and rec[0]["principle"] == "P8"
    assert wp.gate_derived_write("用户喜欢绿茶", {}, user_id="alice", bank_id="default") == ("用户喜欢绿茶", [])
    assert wp.gate_derived_write("  ", {}, user_id="alice", bank_id="default") == (None, [])


# ── self_edit ─────────────────────────────────────────────────────────────
class _SelfEditMem:
    def __init__(self):
        self.updated = None

    def search(self, query, filters=None, **kw):
        return {"results": [{"id": "m1", "memory": "用户以前住在梧桐里", "score": 0.9}]}

    def get(self, memory_id):
        return {"id": memory_id, "memory": "用户以前住在梧桐里"}

    def update(self, memory_id, text, metadata=None):
        self.updated = {"id": memory_id, "text": text, "metadata": metadata}

    def get_all(self, filters=None, **kw):
        return {"results": []}

    def add(self, messages, user_id=None, metadata=None, **kw):
        return {"results": []}


def _verdict(merged):
    import json
    return json.dumps({"decision": "duplicate", "memory_id": "m1", "merged_content": merged,
                       "confidence": 0.9, "reason": "同一件事"}, ensure_ascii=False)


@pytest.fixture
def se(monkeypatch):
    import ducky.self_edit as se
    monkeypatch.setattr(se, "_log_edit", lambda *a, **k: 7)
    return se


def test_self_edit_merge_hitting_forget_rule_is_not_written(se, monkeypatch):
    from ducky import write_policy as wp
    wp.add_rule("alice", "default", "青石巷")
    monkeypatch.setattr(se, "call_llm", lambda *a, **k: _verdict("用户以前住在梧桐里，后来搬到青石巷九号"))
    mem = _SelfEditMem()
    calls = []
    real = wp.gate_derived_write
    monkeypatch.setattr(wp, "gate_derived_write", lambda *a, **k: calls.append(a[0]) or real(*a, **k))
    out = se.self_edit_on_add(mem, "alice", [{"role": "user", "content": "用户以前住在梧桐里，后来搬家"}],
                              {"source": "chat"})
    assert calls, "the merge must reach the gate (not skipped earlier)"
    assert out is None and mem.updated is None          # old memory untouched, normal add goes ahead


def test_self_edit_merge_is_redacted_before_update(se, monkeypatch):
    monkeypatch.setattr(se, "call_llm", lambda *a, **k: _verdict(f"用户住在梧桐里，门禁 API key {SECRET}"))
    mem = _SelfEditMem()
    out = se.self_edit_on_add(mem, "alice", [{"role": "user", "content": "用户以前住在梧桐里，门禁换了"}],
                              {"source": "chat"})
    assert out is not None and mem.updated is not None
    assert SECRET not in mem.updated["text"] and "[redacted:api_key]" in mem.updated["text"]


def test_self_edit_plain_merge_unchanged(se, monkeypatch):
    monkeypatch.setattr(se, "call_llm", lambda *a, **k: _verdict("用户以前住在梧桐里，常去楼下早点铺"))
    mem = _SelfEditMem()
    out = se.self_edit_on_add(mem, "alice", [{"role": "user", "content": "用户以前住在梧桐里，楼下有早点铺"}],
                              {"source": "chat"})
    assert out is not None and mem.updated["text"] == "用户以前住在梧桐里，常去楼下早点铺"


# ── instinct graduation ───────────────────────────────────────────────────
class _GradMem:
    def __init__(self):
        self.added = []

    def add(self, messages, user_id=None, metadata=None, **kw):
        self.added.append({"messages": messages, "metadata": metadata})
        return {"results": [{"id": "skill-1"}]}


@pytest.fixture
def grad(monkeypatch):
    import ducky.instinct_graduation as ig
    items = [{"id": f"m{i}", "memory": f"部署前先备份第{i}次", "metadata": {"category": "ops"}}
             for i in range(6)]
    monkeypatch.setattr(ig, "get_all_memories", lambda *a, **k: {"results": items})
    monkeypatch.setattr(ig, "vector_item_in_bank", lambda item, bank_id: True)
    deleted = []
    import ducky.tombstone as tb
    import ducky.wal_engine as we
    monkeypatch.setattr(tb, "snapshot_before_delete", lambda *a, **k: None)
    monkeypatch.setattr(we, "cascade_delete_memory", lambda sid, **k: deleted.append(sid))
    import ducky.gear as gear
    monkeypatch.setattr(gear, "should_try_llm", lambda **k: False)
    return ig, deleted


def test_graduation_skill_hitting_forget_rule_aborts_and_keeps_originals(grad, monkeypatch):
    ig, deleted = grad
    from ducky import write_policy as wp
    wp.add_rule("alice", "default", "青石巷")
    monkeypatch.setattr(ig, "_call_llm", lambda *a, **k: "部署前先备份；备份放在青石巷那台机器上")
    mem = _GradMem()
    assert ig.graduate_to_skill(mem, "alice", {"category": "ops"}) is None
    assert mem.added == [] and deleted == []


def test_graduation_skill_is_redacted(grad, monkeypatch):
    ig, deleted = grad
    monkeypatch.setattr(ig, "_call_llm", lambda *a, **k: f"部署前先备份，备份桶密钥 {SECRET}")
    mem = _GradMem()
    assert ig.graduate_to_skill(mem, "alice", {"category": "ops"})
    text = mem.added[0]["messages"][0]["content"]
    assert SECRET not in text and "[redacted:api_key]" in text
    assert mem.added[0]["metadata"]["redacted"] == ["api_key"]
    assert len(deleted) == 6


def test_graduation_plain_skill_unchanged(grad, monkeypatch):
    ig, deleted = grad
    monkeypatch.setattr(ig, "_call_llm", lambda *a, **k: "部署前先备份数据库")
    mem = _GradMem()
    assert ig.graduate_to_skill(mem, "alice", {"category": "ops"})
    assert mem.added[0]["messages"][0]["content"] == "部署前先备份数据库" and len(deleted) == 6


@pytest.mark.parametrize("rel", ["ducky/self_edit.py", "ducky/instinct_graduation.py"])
def test_derived_writers_reuse_the_add_gate(rel):
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / rel).read_text(encoding="utf-8")
    assert "gate_derived_write(" in src
    for private in ("gate_decision(", "is_noise(", "blacklist_hit(", "tombstone_gated_write(", "ENFORCED_NOISE"):
        assert private not in src, f"{rel} re-implements the gate ({private})"
