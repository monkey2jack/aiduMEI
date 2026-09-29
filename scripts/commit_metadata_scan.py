#!/usr/bin/env python3
"""Fail closed when new Git commit identities expose private data.

Only an exact name/email pair already present in the public base history may
inherit a wordlist hit. Unsafe email domains are rejected even for such pairs.
The report contains counts only; names, addresses and matched words stay private.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

if __package__:
    from .release_scan import load_words, scan_bytes
else:
    from release_scan import load_words, scan_bytes


class MetadataScanError(RuntimeError):
    """Git metadata or the required scan policy could not be verified."""


@dataclass
class ScanCounts:
    commits: int = 0
    identities: int = 0
    inherited_identities: int = 0
    inherited_word_hits: int = 0
    new_identity_word_hits: int = 0
    forbidden_domains: int = 0

    @property
    def blocked(self) -> bool:
        return bool(self.new_identity_word_hits or self.forbidden_domains)


_IDENTITY = re.compile(rb"(.*) <([^<>]*)> [0-9]+ [+-][0-9]{4}")
_NUMERIC_IPV4 = re.compile(rb"(?:[0-9]{1,3}\.){3}[0-9]{1,3}")


def _git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, check=False,
    )
    if result.returncode:
        raise MetadataScanError("Git 引用或提交元数据不可读取")
    return result.stdout


def _commit_ids(repo: Path, *args: str) -> list[str]:
    raw = _git(repo, "rev-list", *args)
    ids = raw.decode("ascii").splitlines()
    if any(not re.fullmatch(r"[0-9a-f]{40,64}", sha) for sha in ids):
        raise MetadataScanError("Git 提交列表无效")
    return ids


def _identity_pairs(repo: Path, sha: str) -> tuple[tuple[bytes, bytes], tuple[bytes, bytes]]:
    header = _git(repo, "cat-file", "-p", sha).split(b"\n\n", 1)[0]
    result: list[tuple[bytes, bytes]] = []
    for key in (b"author ", b"committer "):
        lines = [line[len(key):] for line in header.split(b"\n") if line.startswith(key)]
        if len(lines) != 1:
            raise MetadataScanError("Git 作者或提交者字段缺失")
        match = _IDENTITY.fullmatch(lines[0])
        if match is None:
            raise MetadataScanError("Git 作者或提交者字段无效")
        name, email = match.groups()
        if not name or not email or any(c < 32 or c == 127 for c in name + email):
            raise MetadataScanError("Git 作者或提交者身份无效")
        try:
            (name + email).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MetadataScanError("Git 作者或提交者身份编码无效") from exc
        result.append((name, email))
    return result[0], result[1]


def _forbidden_email_domain(email: bytes) -> bool:
    if email.count(b"@") != 1:
        return True
    domain = email.rsplit(b"@", 1)[1].lower().rstrip(b".")
    if domain.startswith(b"[") and domain.endswith(b"]"):
        domain = domain[1:-1]
    return (
        not domain
        or not bool(re.fullmatch(rb"[a-z0-9.-]+", domain))
        or any(not label or label.startswith(b"-") or label.endswith(b"-")
               for label in domain.split(b"."))
        or bool(_NUMERIC_IPV4.fullmatch(domain))
        or domain == b"local"
        or domain.endswith(b".local")
    )


def _word_hits(name: bytes, email: bytes, words: list[str]) -> int:
    # Metadata must never honor the file-only release-scan:allow marker.
    count = 0
    for field in (name, email):
        hits, waived = scan_bytes(field, words)
        count += sum(hits.values()) + sum(waived.values())
    return count


def scan_commit_metadata(repo: Path, base: str, head: str, words: list[str]) -> ScanCounts:
    if not words:
        raise MetadataScanError("全量词表为空")
    repo = repo.resolve()
    if subprocess.run(
        ["git", "merge-base", "--is-ancestor", base, head],
        cwd=repo, capture_output=True, check=False,
    ).returncode:
        raise MetadataScanError("公开基线不是候选提交的祖先")

    previously_public: set[tuple[bytes, bytes]] = set()
    for sha in _commit_ids(repo, base):
        previously_public.update(_identity_pairs(repo, sha))

    counts = ScanCounts()
    for sha in _commit_ids(repo, f"{base}..{head}"):
        counts.commits += 1
        for name, email in _identity_pairs(repo, sha):
            counts.identities += 1
            inherited = (name, email) in previously_public
            hits = _word_hits(name, email, words)
            if inherited:
                counts.inherited_identities += 1
                counts.inherited_word_hits += hits
            else:
                counts.new_identity_word_hits += hits
            counts.forbidden_domains += int(_forbidden_email_domain(email))
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="upstream/main")
    parser.add_argument("--head", default="HEAD")
    args = parser.parse_args()
    try:
        counts = scan_commit_metadata(Path.cwd(), args.base, args.head, load_words())
    except (MetadataScanError, OSError, UnicodeError, RuntimeError):
        print("提交元数据扫描：不可核验，停推")
        return 2
    print(
        "提交元数据扫描："
        f"新增提交={counts.commits}，身份字段={counts.identities}，"
        f"已公开同身份={counts.inherited_identities}，"
        f"已公开身份词命中={counts.inherited_word_hits}，"
        f"新身份硬命中={counts.new_identity_word_hits}，"
        f"禁止邮箱域={counts.forbidden_domains}"
    )
    return 1 if counts.blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
