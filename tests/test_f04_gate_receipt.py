"""A failed log remains failed, even if someone attempts to replace its receipt."""
import hashlib
import json
import subprocess
import sys

import pytest

from scripts.gate_receipt import REQUIRED_STEPS, begin, finish, record_step, run_step


@pytest.fixture
def candidate(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "source.py").write_text("print('candidate')\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=Gate Fixture", "-c",
                    "user.email=gate@example.invalid", "commit", "-qm", "fixture"],
                   cwd=repo, check=True)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    return repo, evidence


def test_failure_is_bound_to_log_and_cannot_be_rewritten(candidate):
    repo, evidence = candidate
    begin(repo, evidence)
    raw = b"Traceback: WordlistMissing\n"
    (evidence / "scan.log").write_bytes(raw)
    assert finish(repo, evidence, 2) == 2
    before = (evidence / "receipt.json").read_bytes()
    receipt = json.loads(before)
    assert receipt["status"] == "FAIL"
    assert receipt["logs"]["scan.log"]["sha256"] == hashlib.sha256(raw).hexdigest()
    with pytest.raises(FileExistsError):
        finish(repo, evidence, 0)
    assert (evidence / "receipt.json").read_bytes() == before


def test_source_change_cannot_receive_pass(candidate):
    repo, evidence = candidate
    begin(repo, evidence)
    (repo / "source.py").write_text("print('changed')\n")
    assert finish(repo, evidence, 0) == 1
    assert json.loads((evidence / "receipt.json").read_text())["source_unchanged"] is False


def test_clean_success_and_explicit_skips(candidate):
    repo, evidence = candidate
    begin(repo, evidence)
    for name in REQUIRED_STEPS:
        if name == "static":
            (evidence / "ruff.log").write_text("ruff unavailable\n")
            record_step(evidence, name, 0, skipped="ruff_unavailable")
        else:
            assert run_step(repo, evidence, name, [sys.executable, "-c", "print('fixture command')"]) == 0
    assert finish(repo, evidence, 0, "static") == 0
    receipt = json.loads((evidence / "receipt.json").read_text())
    assert receipt["status"] == "PASS_WITH_SKIPS"
    assert receipt["skipped"] == ["static"]


def test_dirty_candidate_is_rejected_before_execution(candidate):
    repo, evidence = candidate
    (repo / "unreviewed.py").write_text("unexpected = True\n")
    with pytest.raises(RuntimeError, match="clean candidate"):
        begin(repo, evidence)
    assert finish(repo, evidence, 1) == 1


def test_begin_and_zero_exit_do_not_prove_execution(candidate):
    repo, evidence = candidate
    begin(repo, evidence)
    assert finish(repo, evidence, 0) == 1
    receipt = json.loads((evidence / "receipt.json").read_text())
    assert set(receipt["step_errors"]) == set(REQUIRED_STEPS)


def test_failed_command_cannot_be_masked_by_successful_finish(candidate):
    repo, evidence = candidate
    begin(repo, evidence)
    for name in REQUIRED_STEPS:
        code = 7 if name == "tree-scan" else 0
        assert run_step(repo, evidence, name, [sys.executable, "-c", f"raise SystemExit({code})"]) == code
    assert finish(repo, evidence, 0) == 1
    receipt = json.loads((evidence / "receipt.json").read_text())
    assert receipt["step_errors"] == ["tree-scan"]


def test_completed_log_change_is_detected(candidate):
    repo, evidence = candidate
    begin(repo, evidence)
    for name in REQUIRED_STEPS:
        assert run_step(repo, evidence, name, [sys.executable, "-c", "print('original')"]) == 0
    (evidence / "tests.log").write_text("replacement\n")
    assert finish(repo, evidence, 0) == 1
    assert json.loads((evidence / "receipt.json").read_text())["step_errors"] == ["tests"]
