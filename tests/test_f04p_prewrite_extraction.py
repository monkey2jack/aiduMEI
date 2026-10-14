"""f0.4+ WP1: provider refusal at extraction is evidence of "nothing was written".

Production 2026-10-14: one turn whose extraction call was refused by the
provider (HTTP 451 censorship_blocked) was journalled as repair_required, and
the scope-wide fail-closed gate then rejected every later write (HTTP 409) for
that tenant until an operator resolved it by hand. The same shape had already
happened on 2026-10-10. The deterministic infer=False fallback was collateral
damage: it refuses to run when durable evidence shows any SDK attempt, and the
journal had just written one.

These tests pin the fix in three places:
  1. the invariant that makes the classification true (installed mem0 source:
     extraction precedes every write);
  2. the journal/fallback behaviour for that exact failure shape;
  3. negative controls that must still require repair.
"""
from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest
from mem0.exceptions import LLMError

from ducky import mutation_fallback as mf
from ducky import mutation_journal as journal
from ducky import utils
from ducky.gear import is_content_rejection

_EXTRACTION_MARK = "LLM extraction failed"
_WRITE_CALLS = {
    "_create_memory", "_update_memory", "_delete_memory", "_create_procedural_memory",
    "insert", "update", "delete", "upsert", "add_history", "save_messages",
}


class _Refusal(LLMError):
    """LLMError carrying an OpenAI-shaped refusal, as production saw it."""

    def __init__(self, status_code=451, code="censorship_blocked"):
        super().__init__(f"{_EXTRACTION_MARK}: Error code: {status_code}")
        self.status_code = status_code
        self.body = {"error": {"message": "blocked", "type": code}}


def _installed_mem0_main() -> Path:
    import mem0
    return Path(mem0.__file__).resolve().parent / "memory" / "main.py"


def _guarded_extraction_tries(tree: ast.AST) -> list[ast.Try]:
    """Try blocks whose handlers raise the extraction LLMError.

    Sync mem0 calls ``self.llm.generate_response(...)`` inline; the async twin
    wraps the same call in ``asyncio.to_thread``. Accept both shapes so the
    guard does not silently stop inspecting one of them.
    """
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        dump = ast.dump(node)
        if _EXTRACTION_MARK not in dump:
            continue
        if "generate_response" not in dump:
            continue
        if not any(isinstance(inner, ast.Raise) for handler in node.handlers
                   for inner in ast.walk(handler)):
            continue
        found.append(node)
    return found


def _order_violations(source: str) -> list[str]:
    """Return human-readable ordering violations for one mem0 memory/main.py text.

    Extracted from the test so the guard itself can be exercised against a
    deliberately reordered copy (a guard that only ever sees the good tree
    cannot show that it would notice a bad one).
    """
    tree = ast.parse(source)
    tries = {id(n) for n in _guarded_extraction_tries(tree)}
    if len(tries) < 2:
        return [f"expected sync and async extraction guards, found {len(tries)}"]
    problems: list[str] = []
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        guarded = [t for t in ast.walk(node) if isinstance(t, ast.Try) and id(t) in tries]
        if not guarded:
            continue
        checked += 1
        guard_first_line = min(t.lineno for t in guarded)
        guard_last_line = max(t.end_lineno for t in guarded)
        # The infer=False branch writes directly and returns before extraction;
        # it is the fallback this fix relies on, not a pre-extraction write.
        skipped = [
            (branch.lineno, branch.end_lineno) for branch in ast.walk(node)
            if isinstance(branch, ast.If)
            and isinstance(branch.test, ast.UnaryOp) and isinstance(branch.test.op, ast.Not)
            and isinstance(branch.test.operand, ast.Name) and branch.test.operand.id == "infer"
            and branch.body and isinstance(branch.body[-1], ast.Return)
        ]
        # Attribute references, not just direct calls: the async twin hands
        # writers to asyncio.to_thread(self.vector_store.insert, ...).
        sites = [(ref.lineno, ref.attr) for ref in ast.walk(node)
                 if isinstance(ref, ast.Attribute) and ref.attr in _WRITE_CALLS]
        early = sorted({attr for line, attr in sites if line < guard_first_line
                        and not any(lo <= line <= hi for lo, hi in skipped)})
        if early:
            problems.append(f"{node.name} writes before extraction: {early}")
        if not [attr for line, attr in sites if line > guard_last_line]:
            problems.append(f"{node.name}: no post-extraction write detected; guard scope changed")
    if not checked:
        problems.append("no guarded extraction site inspected")
    return problems


def test_installed_mem0_extraction_precedes_every_write():
    """The classification is only true while this invariant holds.

    Reads the installed mem0 (2.2.1, pinned in pyproject) rather than our own
    code: upstream reordering is exactly the change that would silently turn
    "proven no write" into "a write may have happened".
    """
    source = _installed_mem0_main().read_text(encoding="utf-8")
    assert source.count(_EXTRACTION_MARK) >= 2, "upstream LLMError sites moved"
    assert _order_violations(source) == []


def test_ordering_guard_notices_a_reordered_write():
    """Negative control: the same checker must fail on a pre-extraction write."""
    source = _installed_mem0_main().read_text(encoding="utf-8")
    needle = "        user_prompt = generate_additive_extraction_prompt("
    assert needle in source
    mutated = source.replace(
        needle,
        "        self.vector_store.insert(collection_name='mem0', vectors=[], payloads=[])\n" + needle,
        1,
    )
    problems = _order_violations(mutated)
    assert any("writes before extraction" in p for p in problems), problems


def test_infer_false_path_skips_extraction_entirely():
    """Why the fallback is safe: infer=False never reaches the LLM call."""
    src = _installed_mem0_main().read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_add_to_vector_store":
            body = ast.dump(node)
            assert "infer" in body and "if not infer" in src, "infer short-circuit vanished"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(utils, "DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(utils, "FACTS_DB", str(tmp_path / "data" / "facts.db"))
    return tmp_path


def _rows():
    conn = sqlite3.connect(journal.journal_path())
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM mutations ORDER BY created_at")]
    finally:
        conn.close()


def test_extraction_refusal_records_not_applied_and_keeps_scope_open(isolated):
    with pytest.raises(LLMError):
        journal.perform_mutation("mem0_add", "u", "b", {"messages": "synthetic"},
                                 lambda: (_ for _ in ()).throw(_Refusal()))
    row, = _rows()
    assert row["state"] == "not_applied"
    assert row["reason"] == journal.PRE_WRITE_FAILURE_REASON
    assert row["result_json"] in (None, "null")
    assert journal.journal_health()["status"] == "ok", "a proven no-write must not degrade health"
    assert journal.journal_health()["rejected_before_write_24h"] == 1

    # Scope is still writable: the whole point of the fix.
    assert journal.perform_mutation("mem0_add", "u", "b", {},
                                    lambda: {"results": [{"id": "next"}]})["results"]


def test_refusal_does_not_count_as_sdk_attempt_for_fallback_evidence(isolated):
    with pytest.raises(LLMError):
        journal.perform_mutation("mem0_add", "u", "b", {}, lambda: (_ for _ in ()).throw(_Refusal()))
    assert mf.sdk_attempt_count("u", "b") == 0
    mf.require_no_sdk_since(0, "u", "b", _Refusal())  # must not raise


def test_operator_resolution_and_health_still_see_real_repairs(isolated):
    with pytest.raises(journal.MutationUncertain):
        journal.perform_mutation("mem0_add", "u", "b", {}, lambda: (_ for _ in ()).throw(OSError("boom")))
    assert journal.journal_health()["status"] == "degraded"
    assert mf.sdk_attempt_count("u", "b") == 1, "unproven writes must still block fallback"


@pytest.mark.parametrize("case", ["suffix_message", "other_kind", "plain_exception"])
def test_negative_controls_still_require_repair(isolated, case):
    exc = {
        "suffix_message": LLMError("wrapped: LLM extraction failed: x"),
        "other_kind": _Refusal(),
        "plain_exception": OSError("network"),
    }[case]
    kind = "mem0_update" if case == "other_kind" else "mem0_add"
    with pytest.raises(journal.MutationUncertain):
        journal.perform_mutation(kind, "u", "b", {}, lambda: (_ for _ in ()).throw(exc))
    row, = _rows()
    assert row["state"] == "repair_required", case
    assert journal.journal_health()["status"] == "degraded"


def test_classification_requires_kind_real_type_and_extraction_prefix():
    """Only mem0's own extraction LLMError on an add is excused (the AST guard
    above proves that raise site precedes every write)."""
    assert journal.failed_before_write("mem0_add", _Refusal()) is True
    assert journal.failed_before_write("mem0_update", _Refusal()) is False
    assert journal.failed_before_write("mem0_add", LLMError("LLM call failed: x")) is False
    lookalike = type("LLMError", (Exception,), {})(f"{_EXTRACTION_MARK}: 451")
    assert journal.failed_before_write("mem0_add", lookalike) is False, \
        "a same-named class from elsewhere is not mem0's extraction failure"


def test_content_rejection_detection_is_structural():
    assert is_content_rejection(_Refusal()) is True
    assert is_content_rejection(LLMError(f"{_EXTRACTION_MARK}: Error code: 521")) is False
    throttled = _Refusal(status_code=429, code="rate_limit_exceeded")
    assert is_content_rejection(throttled) is False, "429 is provider health, not a refusal"
    chained = LLMError(f"{_EXTRACTION_MARK}: 451")
    chained.__cause__ = _Refusal()
    assert is_content_rejection(chained) is True, "must walk the exception chain"
