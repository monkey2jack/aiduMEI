"""f0.7.6 · structural / delete-chain / opinions hardening (third outside-audit round, R2-08..R2-10, R2-12..R2-16).

Every PoC from the audit report is replayed as a reverse assertion: the PoC must now FAIL to leave its marker.
HTTP cases run in one subprocess (api_server reads env at import): tests/_f076_struct_probe.py.
"""
import json
import os
import sqlite3
import subprocess
import sys

import pytest

_PROBE = os.path.join(os.path.dirname(__file__), "_f076_struct_probe.py")


@pytest.fixture(scope="module")
def out():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AIDUMEM_", "AIDUMEI_"))}
    p = subprocess.run([sys.executable, _PROBE], capture_output=True, text=True, timeout=300, env=env)
    assert p.returncode == 0, p.stderr[-3000:]
    return json.loads(p.stdout.strip().splitlines()[-1])


# ── R2-08 ──────────────────────────────────────────────────────────────

def test_r208_role_secret_never_stored(out):
    assert out["r208_status"] == 200
    assert out["r208_residue"] == {}
    assert out["r208_roles"] == ["user"]


def test_r208_role_and_type_whitelist_units():
    from ducky.write_policy import normalize_message_shapes, redact_messages_struct
    got = normalize_message_shapes([{"role": "sk-proj-RoleUnit0123456789abcdef", "content": "x"},
                                    {"role": "Assistant", "content": "y"},
                                    {"role": 7, "content": "z"},
                                    {"role": "developer", "content": "w"}])
    assert [m["role"] for m in got] == ["user", "assistant", "user", "developer"]
    red, _ = redact_messages_struct({"role": "password=Hunter2", "content": [
        {"type": "password: TypeUnit#Pw", "text": "hi"}, {"type": "text", "text": "ok"}]})
    assert red["role"] == "user"
    assert [p["type"] for p in red["content"]] == ["text", "text"]
    assert "TypeUnit" not in json.dumps(red)


# ── R2-09 ──────────────────────────────────────────────────────────────

def test_r209_json_string_messages_redacted_before_journal(out):
    assert out["r209_status"] == 200
    assert out["r209_residue"] == {}
    assert out["r209_journal"] is False
    assert out["r209_content_kept"] is True   # JSON strings are parsed, not dropped


def test_r209_journal_payload_redaction_unit():
    from ducky.mutation_journal import _redact_payload
    payload = {"messages": [{"role": "user", "content": "token sk-proj-JournalUnit0123456789abcdef"}],
               "metadata": {"note": "api_key=JournalMeta0123456789xyz"},
               "user_id": "u1", "bank_id": "default", "request_key": "abc-123"}
    red = _redact_payload(payload)
    blob = json.dumps(red, ensure_ascii=False)
    assert "JournalUnit" not in blob and "JournalMeta" not in blob
    assert red["user_id"] == "u1" and red["request_key"] == "abc-123"   # id / scope keys untouched


# ── R2-10 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key", ["sess", "event", "agent"])
def test_r210_top_level_extra_fields_not_stored_or_echoed(out, key):
    status, residue, echoed = out[f"r210_{key}"]
    assert status == 200
    assert residue == {}
    assert echoed is False


def test_r210_well_formed_session_and_event_at_still_land(out):
    status, session, event_at = out["r210_ok_control"]
    assert status == 200
    assert session == "sess-f076.ok:1"
    assert event_at


def test_r210_single_delete_drops_memory_epistemic(out):
    assert out["r210_epi_before"] == 1
    assert out["r210_single_delete"] == 200
    assert out["r210_epi_after"] == 0
    # the episode row is per-session, not per-memory: it is removed by delete_all (below), not by a single delete
    assert set(out["r210_epi_sess_after"]) <= {"evolve_mem.db:evolve_episodes.session_id"}


def test_r210_delete_all_drops_evolve_episodes(out):
    assert out["r210_episodes_before"] >= 1
    assert out["r210_delete_all"] in ("committed", "ok")
    assert out["r210_episodes_after"] == 0
    assert out["r210_all_sess_after"] == {}


def test_r210_origin_and_event_at_units():
    from ducky.origin_context import extract_origin_fields, safe_event_at, safe_origin_id
    assert safe_origin_id("token=Mk17Sess#Tok") == ""
    assert safe_origin_id("a" * 129) == ""
    assert safe_origin_id("sk-proj-OriginUnit0123456789abcdef") == ""
    assert safe_origin_id("hermes_20250101-abc.def:2") == "hermes_20250101-abc.def:2"
    assert extract_origin_fields({"session_id": "x y", "agent": "codex-cli"}, {}) == ("codex-cli", "", 0)
    assert safe_event_at("password=Mk19Event#Pw") is None
    assert safe_event_at("2024-05-01T08:00:00Z") == "2024-05-01T08:00:00Z"
    assert safe_event_at("2024-05-01") == "2024-05-01"
    assert safe_event_at(1714550400) == 1714550400
    assert safe_event_at(True) is None and safe_event_at({"x": 1}) is None


def test_r210_delete_chain_matrix_mentions_new_steps():
    from ducky.wal_engine import DELETE_CHAIN_MATRIX
    assert "R2-10" in DELETE_CHAIN_MATRIX["memory_epistemic"][1]
    assert "evolve_episodes" in DELETE_CHAIN_MATRIX["store:evolve"][1]


# ── R2-15 ──────────────────────────────────────────────────────────────

def test_r215_deleted_text_not_left_in_wal(out):
    assert out["r215_wal_before"] is True      # the probe really exercised the WAL
    status, hits = out["r215_single"]
    assert status == 200 and hits == {}
    assert out["r215_all"] == {}
    assert out["r215_secure_delete"] == 1


def test_r215_checkpoint_never_raises(tmp_path, monkeypatch):
    from ducky import wal_engine
    bad = tmp_path / "broken.db"
    bad.write_bytes(b"not a database")
    (tmp_path / "broken.db-wal").write_bytes(b"junk")
    monkeypatch.setattr(wal_engine, "_wal_checkpoint_paths", lambda: [str(bad)])
    assert isinstance(wal_engine.checkpoint_deleted_text(), dict)
    def _boom():
        raise OSError("data dir gone")

    monkeypatch.setattr(wal_engine, "_wal_checkpoint_paths", _boom)
    assert wal_engine.checkpoint_deleted_text() == {}


# ── R2-13 ──────────────────────────────────────────────────────────────

def test_r213_evidence_ids_validated(out):
    assert out["r213_secret"] == 400
    assert out["r213_secret_residue"] == {}
    assert out["r213_cross_fact"] == 400
    assert out["r213_cross_memory"] == 400
    assert out["r213_unknown_uuid"] == 400
    assert out["r213_own"] == 200
    assert out["r213_none"] == 200


# ── R2-14 ──────────────────────────────────────────────────────────────

def test_r214_shadow_forgery_not_counted_or_listed(out):
    assert out["r214_shadow_write"] == 200          # shadow period lets it in (the audit's premise)
    assert out["r214_alice_list_owners"] == []
    assert out["r214_alice_aggregate_sources"] == 0


def test_r214_upsert_cannot_take_over_another_owners_row(out):
    assert out["r214_overwrite_other_owner"] == 409
    assert out["r214_row_owner"] == [["bob"]]
    assert out["r214_own_write"] == 200


def test_r214_owner_filter_units(monkeypatch, tmp_path):
    from ducky import opinion
    db = tmp_path / "facts.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, fact_key TEXT, fact_value TEXT, "
                 "user_id TEXT, bank_id TEXT)")
    conn.execute("INSERT INTO facts VALUES (1, 'k', 'v', 'tenant_a', 'default')")
    conn.execute("INSERT INTO facts VALUES (2, 'k', 'v', 'default', 'default')")
    conn.commit()

    class _Keep:
        def __getattr__(self, name):
            return getattr(conn, name)

        def close(self):
            pass

    monkeypatch.setattr(opinion, "get_facts_conn", lambda: _Keep())
    assert opinion.set_opinion(1, "support", source="s1", owner="tenant_a")["ok"] is True
    assert opinion.set_opinion(1, "oppose", source="s2", owner="tenant_b")["ok"] is True
    taken = opinion.set_opinion(1, "oppose", source="s1", owner="tenant_b")
    assert taken["ok"] is False and taken.get("conflict") is True
    assert [r["owner"] for r in opinion.list_opinions(1)] == ["tenant_a"]
    assert opinion.aggregate_opinion(1)["distinct_sources"] == 1
    # same owner may update its own row
    assert opinion.set_opinion(1, "neutral", source="s1", owner="tenant_a")["ok"] is True
    # default/unclaimed facts: the default identity's spellings are one owner
    assert opinion.set_opinion(2, "support", source="d1", owner="default")["ok"] is True
    assert opinion.set_opinion(2, "support", source="d2", owner="")["ok"] is True
    assert len(opinion.list_opinions(2)) == 2


# ── R2-12 nit / R2-16 ─────────────────────────────────────────────────

def test_r212_cross_tenant_feedback_is_404(out):
    assert out["r212_cross_feedback"] == 404
    assert out["r212_own_feedback"] == 200


def test_r216_login_non_object_json_is_400(out):
    assert out["r216"] == [400, 400, 400]
