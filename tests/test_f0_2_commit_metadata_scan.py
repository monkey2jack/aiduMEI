"""The push gate must inspect identity headers as well as commit messages."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "commit_metadata_scan.py"


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, env=env, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "commit.gpgsign", "false")
    _git(repo, "config", "user.name", "fixture")
    _git(repo, "config", "user.email", "fixture@example.invalid")
    return repo


def _commit(
    repo: Path, author: tuple[str, str],
    committer: tuple[str, str] | None = None,
) -> str:
    committer = committer or author
    env = os.environ.copy()
    env.update({
        "GIT_AUTHOR_NAME": author[0],
        "GIT_AUTHOR_EMAIL": author[1],
        "GIT_COMMITTER_NAME": committer[0],
        "GIT_COMMITTER_EMAIL": committer[1],
    })
    _git(repo, "commit", "--allow-empty", "-qm", "fixture", env=env)
    return _git(repo, "rev-parse", "HEAD")


def _scan(repo: Path, base: str, wordlist: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("AIDUMEI_SCAN_WORDS", None)
    env["AIDUMEI_SCAN_WORDLIST"] = str(wordlist)
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--base", base],
        cwd=repo, env=env, capture_output=True, text=True, check=False,
    )


def test_exact_public_identity_inherits_wordlist_hit_without_exposing_it(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    known = ("public-maintainer", "public-maintainer@users.noreply.github.com")
    base = _commit(repo, known)
    _commit(repo, known)
    words = tmp_path / "words.txt"
    words.write_text("public-maintainer@\n", encoding="utf-8")

    result = _scan(repo, base, words)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "已公开同身份=2" in result.stdout
    assert "已公开身份词命中=2" in result.stdout
    assert "新身份硬命中=0" in result.stdout
    assert "public-maintainer" not in result.stdout + result.stderr


@pytest.mark.parametrize("bad_role", ["author", "committer"])
def test_new_identity_private_word_blocks_both_header_roles(
    tmp_path: Path, bad_role: str,
) -> None:
    repo = _repo(tmp_path)
    known = ("public-maintainer", "public-maintainer@users.noreply.github.com")
    base = _commit(repo, known)
    private = ("new-maintainer", "new-private-fragment@example.com")
    _commit(repo, private if bad_role == "author" else known,
            private if bad_role == "committer" else known)
    words = tmp_path / "words.txt"
    words.write_text("private-fragment\n", encoding="utf-8")

    result = _scan(repo, base, words)

    assert result.returncode == 1
    assert "新身份硬命中=1" in result.stdout
    assert "private-fragment" not in result.stdout + result.stderr


@pytest.mark.parametrize("domain", ["198.51.100.42", "host.local"])
def test_previously_public_identity_still_fails_for_unsafe_email_domain(
    tmp_path: Path, domain: str,
) -> None:
    repo = _repo(tmp_path)
    known = ("public-maintainer", f"public-maintainer@{domain}")
    base = _commit(repo, known)
    _commit(repo, known)
    words = tmp_path / "words.txt"
    words.write_text("public-maintainer@\n", encoding="utf-8")

    result = _scan(repo, base, words)

    assert result.returncode == 1
    assert "已公开同身份=2" in result.stdout
    assert "禁止邮箱域=2" in result.stdout
    assert domain not in result.stdout + result.stderr
