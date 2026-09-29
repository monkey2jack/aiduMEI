"""The release scan may inherit only exact public baseline and reviewed lines."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import release_scan as scan


PUBLIC = "SYNTH-PUBLIC-62DB"
PRIVATE = "SYNTH-PRIVATE-710A"
WORDS = sorted([PUBLIC, PRIVATE])


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def policy_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Synthetic Reviewer")
    _git(repo, "config", "user.email", "reviewer@example.invalid")
    (repo / "old.txt").write_text(f"{PUBLIC}\n{PRIVATE}\n", encoding="utf-8")
    _git(repo, "add", "old.txt")
    _git(repo, "commit", "-qm", "synthetic baseline")
    baseline = _git(repo, "rev-parse", "HEAD")
    digest = hashlib.sha256("\0".join(WORDS).encode()).hexdigest()
    public_file = tmp_path / "public-policy.txt"
    public_file.write_text(
        f"# baseline={baseline}\n# full_wordlist_sha256={digest}\n{PUBLIC}\n",
        encoding="utf-8",
    )
    public_file.chmod(0o600)
    review_file = tmp_path / "reviewed-policy.txt"

    def write_review(entries: list[tuple[str, bytes, int]]) -> None:
        lines = [f"# baseline={baseline}", f"# full_wordlist_sha256={digest}"]
        lines += [f"{rel}\t{hashlib.sha256(raw).hexdigest()}\t{count}"
                  for rel, raw, count in entries]
        review_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        review_file.chmod(0o600)

    write_review([])
    monkeypatch.chdir(repo)
    env = {"AIDUMEI_SCAN_PUBLIC_WORDLIST": str(public_file),
           "AIDUMEI_SCAN_REVIEWED_PUBLIC": str(review_file)}
    return repo, public_file, review_file, env, write_review


def _totals(path: Path, policy: scan.PublicPolicy) -> tuple[int, int, int, int]:
    hard, waived, inherited, reviewed, scanned, skipped = (
        scan.scan_tree_with_public_baseline(path, WORDS, policy)
    )
    assert (scanned, skipped) == (1, 0)
    return tuple(sum(sum(files.values()) for files in bucket.values())
                 for bucket in (hard, waived, inherited, reviewed))


def test_unchanged_public_line_inherits_but_private_word_does_not(policy_repo):
    repo, _, _, env, _ = policy_repo
    policy = scan.load_public_policy(WORDS, env)
    assert policy is not None
    assert _totals(repo / "old.txt", policy) == (1, 0, 1, 0)


def test_extra_copy_of_baseline_line_is_hard_hit(policy_repo):
    repo, _, _, env, _ = policy_repo
    (repo / "old.txt").write_text(f"{PUBLIC}\n{PRIVATE}\n{PUBLIC}\n", encoding="utf-8")
    policy = scan.load_public_policy(WORDS, env)
    assert policy is not None
    assert _totals(repo / "old.txt", policy) == (2, 0, 1, 0)


def test_changed_line_and_new_file_do_not_inherit(policy_repo):
    repo, _, _, env, _ = policy_repo
    (repo / "old.txt").write_text(
        f"changed {PUBLIC} # {scan.ALLOW_MARK}\n{PRIVATE}\n", encoding="utf-8",
    )
    (repo / "new.txt").write_text(f"{PUBLIC}\n", encoding="utf-8")
    policy = scan.load_public_policy(WORDS, env)
    assert policy is not None
    assert _totals(repo / "old.txt", policy) == (2, 0, 0, 0)
    assert _totals(repo / "new.txt", policy) == (1, 0, 0, 0)


def test_reviewed_line_binds_path_bytes_and_copy_count(policy_repo):
    repo, _, _, env, write_review = policy_repo
    approved = f"reviewed {PUBLIC}".encode()
    write_review([("new.txt", approved, 1)])
    (repo / "new.txt").write_bytes(approved + b"\n")
    policy = scan.load_public_policy(WORDS, env)
    assert policy is not None
    assert _totals(repo / "new.txt", policy) == (0, 0, 0, 1)

    (repo / "new.txt").write_bytes(approved + b"\n" + approved + b"\n")
    assert _totals(repo / "new.txt", policy) == (1, 0, 0, 1)
    (repo / "other.txt").write_bytes(approved + b"\n")
    assert _totals(repo / "other.txt", policy) == (1, 0, 0, 0)


def test_reviewed_line_change_or_private_word_is_hard_hit(policy_repo):
    repo, _, _, env, write_review = policy_repo
    approved = f"reviewed {PUBLIC}".encode()
    write_review([("new.txt", approved, 1)])
    policy = scan.load_public_policy(WORDS, env)
    assert policy is not None
    (repo / "new.txt").write_bytes(b"revised " + approved + b"\n")
    assert _totals(repo / "new.txt", policy) == (1, 0, 0, 0)
    (repo / "new.txt").write_bytes(
        approved + b" " + PRIVATE.encode() + f" # {scan.ALLOW_MARK}\n".encode()
    )
    assert _totals(repo / "new.txt", policy) == (2, 0, 0, 0)


def test_public_policy_rejects_missing_or_wide_file(policy_repo):
    _, public_file, _, env, _ = policy_repo
    public_file.chmod(0o644)
    with pytest.raises(scan.PublicPolicyError):
        scan.load_public_policy(WORDS, env)
    public_file.unlink()
    with pytest.raises(scan.PublicPolicyError):
        scan.load_public_policy(WORDS, env)


def test_policy_rejects_shortened_wordlist_and_invalid_review_file(policy_repo):
    _, public_file, review_file, env, _ = policy_repo
    with pytest.raises(scan.PublicPolicyError):
        scan.load_public_policy([PUBLIC], env)
    review_file.chmod(0o644)
    with pytest.raises(scan.PublicPolicyError):
        scan.load_public_policy(WORDS, env)
    review_file.chmod(0o600)
    public_file.write_text(public_file.read_text().replace("# baseline=", "# baseline=0"))
    with pytest.raises(scan.PublicPolicyError):
        scan.load_public_policy(WORDS, env)


def test_policy_rejects_baseline_that_is_not_head_ancestor(policy_repo):
    repo, public_file, _, env, _ = policy_repo
    (repo / "later.txt").write_text("synthetic later change\n", encoding="utf-8")
    _git(repo, "add", "later.txt")
    _git(repo, "commit", "-qm", "synthetic future")
    future = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "--detach", "HEAD~1")
    original = public_file.read_text(encoding="utf-8")
    public_file.write_text(
        original.replace(original.splitlines()[0], f"# baseline={future}"),
        encoding="utf-8",
    )
    with pytest.raises(scan.PublicPolicyError):
        scan.load_public_policy(WORDS, env)


def test_reviewed_policy_rejects_duplicate_and_invalid_count(policy_repo):
    _, _, review_file, env, write_review = policy_repo
    approved = f"reviewed {PUBLIC}".encode()
    write_review([("new.txt", approved, 1), ("new.txt", approved, 1)])
    with pytest.raises(scan.PublicPolicyError):
        scan.load_public_policy(WORDS, env)
    write_review([("new.txt", approved, 0)])
    with pytest.raises(scan.PublicPolicyError):
        scan.load_public_policy(WORDS, env)
    assert review_file.stat().st_mode & 0o077 == 0


def test_reviewed_policy_rejects_path_traversal(policy_repo):
    _, _, _, env, write_review = policy_repo
    write_review([("../outside.txt", f"reviewed {PUBLIC}".encode(), 1)])
    with pytest.raises(scan.PublicPolicyError):
        scan.load_public_policy(WORDS, env)


def test_redacted_report_contains_no_matched_value():
    report, total = scan.format_report(
        f"synthetic-{PUBLIC}", {PUBLIC: {f"{PUBLIC}.txt": 1}},
        {}, WORDS, 1, 0, redact=True,
    )
    assert total == 1
    assert PUBLIC not in report
    assert "词表项#" in report
    assert "路径已隐藏" in report


def test_runtime_selftest_covers_reviewed_line_mutations():
    scan.selftest(WORDS)


def test_cli_redacted_reviewed_line_passes_and_mutation_fails(policy_repo):
    repo, _, _, env, write_review = policy_repo
    approved = f"reviewed {PUBLIC}".encode()
    write_review([("new.txt", approved, 1)])
    target = repo / "new.txt"
    target.write_bytes(approved + b"\n")
    command = [sys.executable, str(Path(scan.__file__).resolve()),
               "--redact-report", str(target)]
    process_env = dict(os.environ)
    process_env.update(env)
    process_env.update({"AIDUMEI_SCAN_WORDS": f"{PUBLIC}|{PRIVATE}",
                        "AIDUMEI_SCAN_WORDLIST": ""})
    passed = subprocess.run(command, cwd=repo, env=process_env,
                            capture_output=True, text=True)
    assert passed.returncode == 0
    assert "总计逐行复核公开标识 = 1" in passed.stdout
    assert "总计硬敏感命中 = 0" in passed.stdout

    target.write_bytes(approved + b" " + PRIVATE.encode() + b"\n")
    failed = subprocess.run(command, cwd=repo, env=process_env,
                            capture_output=True, text=True)
    assert failed.returncode == 1
    assert "总计硬敏感命中 = 2" in failed.stdout
    assert PUBLIC not in failed.stdout and PRIVATE not in failed.stdout


def test_private_review_file_is_required_when_configured(policy_repo):
    _, _, review_file, env, _ = policy_repo
    review_file.unlink()
    with pytest.raises(scan.PublicPolicyError):
        scan.load_public_policy(WORDS, env)


def _standalone_scan_words(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIDUMEI_SCAN_WORDS", PUBLIC)
    for name in ("AIDUMEI_SCAN_WORDLIST", "AIDUMEI_SCAN_PUBLIC_WORDLIST",
                 "AIDUMEI_SCAN_REVIEWED_PUBLIC"):
        monkeypatch.delenv(name, raising=False)


def test_cli_mixed_binary_file_refuses_coverage(tmp_path, monkeypatch, capsys):
    _standalone_scan_words(monkeypatch)
    (tmp_path / "readable.txt").write_text("clean\n", encoding="utf-8")
    (tmp_path / "unknown.blob").write_bytes(b"\x00" + PUBLIC.encode())
    assert scan.main([str(tmp_path)]) == 2
    output = capsys.readouterr()
    assert "覆盖缺口" in output.err
    assert "总计扫描覆盖跳过 = 1" in output.out
    assert "✅ 无硬敏感命中" not in output.out


def test_cli_mixed_unreadable_file_refuses_coverage(tmp_path, monkeypatch, capsys):
    _standalone_scan_words(monkeypatch)
    (tmp_path / "readable.txt").write_text("clean\n", encoding="utf-8")
    unreadable = tmp_path / "unreadable.txt"
    unreadable.write_text("hidden\n", encoding="utf-8")
    original = Path.read_bytes

    def read_or_fail(path: Path) -> bytes:
        if path == unreadable:
            raise OSError("synthetic read failure")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read_or_fail)
    assert scan.main([str(tmp_path)]) == 2
    output = capsys.readouterr()
    assert "覆盖缺口" in output.err
    assert "总计扫描覆盖跳过 = 1" in output.out
    assert "✅ 无硬敏感命中" not in output.out


def test_cli_mixed_symlink_refuses_coverage(tmp_path, monkeypatch, capsys):
    _standalone_scan_words(monkeypatch)
    readable = tmp_path / "readable.txt"
    readable.write_text("clean\n", encoding="utf-8")
    (tmp_path / "linked.txt").symlink_to(readable)
    assert scan.main([str(tmp_path)]) == 2
    output = capsys.readouterr()
    assert "覆盖缺口" in output.err
    assert "总计扫描覆盖跳过 = 1" in output.out
    assert "✅ 无硬敏感命中" not in output.out


def test_scan_gate_uses_private_temp_logs_and_strict_git_log():
    gate = Path(__file__).resolve().parents[1] / "scripts/push_gate.sh"
    source = gate.read_text()
    assert "umask 077" in source and "mktemp -d" in source
    assert "--redact-report" in source
    assert "git log --format='%B'" in source and "|| true" not in source
    assert "/tmp/g_" not in source
    subprocess.run(["bash", "-n", str(gate)], check=True)
