#!/usr/bin/env python3
"""Fail closed when new Git identities expose private data.

Scanned identities: author and committer of every new commit, plus the tagger
of every annotated tag object that is scanned (``--tag`` / ``--tags-in-range``)
or pushed (``--pre-push``, fed by the git pre-push hook for every ref: branches
and tags alike).

Only an exact name/email pair already present in the public base history may
inherit a wordlist hit. Unsafe email domains are rejected even for such pairs:
numeric or partial IP addresses, single-label hosts, private suffixes
(``.local`` ``.localhost`` ``.localdomain`` ``.internal`` ``.intranet`` ``.lan``
``.home`` ``.corp`` ``.private`` ``.arpa``) and default cloud host names
(Aliyun ``iZ...Z``, Tencent ``VM-x-y-os``, dashed IPv4 labels such as AWS
``ip-10-0-0-1``).

``--require-allowlist`` adds the allowlist mode: every scanned identity must be
a GitHub noreply address or an exact project identity given with
``--allow-identity 'Name <email>'`` or ``AIDUMEI_IDENTITY_ALLOWLIST``
(entries separated by ``;`` or newlines).

The report contains counts only; names, addresses and matched words stay private.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
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
    refs: int = 0
    tags: int = 0
    not_allowlisted: int = 0

    @property
    def blocked(self) -> bool:
        return bool(self.new_identity_word_hits or self.forbidden_domains
                    or self.not_allowlisted)


Identity = tuple[bytes, bytes]

_IDENTITY = re.compile(rb"(.*) <([^<>]*)> [0-9]+ [+-][0-9]{4}")
_NUMERIC_IPV4 = re.compile(rb"(?:[0-9]{1,3}\.){3}[0-9]{1,3}")
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_ZERO_SHA = re.compile(r"0{40}(?:0{24})?")
# 私网/本机后缀：落在这些后缀下的「邮箱」指向一台机器或一张私网，
# 不可能是公开邮箱（RFC 6762 .local、RFC 6761 .localhost、RFC 8375 home.arpa
# 与事实上的内网惯用名）。只比最后一个标签，避免误伤 home.nl 这类真实域名。
_PRIVATE_SUFFIXES = frozenset((
    b"local", b"localhost", b"localdomain", b"internal", b"intranet",
    b"lan", b"home", b"corp", b"private", b"arpa",
))
# 云主机默认主机名：形如 iZ<实例号>Z、VM-0-12-centos 的厂商出厂实例名。
_CLOUD_HOST_LABEL = re.compile(rb"iz[0-9a-z]{8,}z|vm-[0-9]{1,3}-[0-9]{1,3}-[a-z0-9]+")
# 标签里嵌着点分改连字符的 IPv4（AWS ip-10-0-0-1 / ec2-54-1-2-3 等）。
_DASHED_IPV4 = re.compile(rb"(?:^|-)[0-9]{1,3}(?:-[0-9]{1,3}){3}(?:-|$)")
_GITHUB_NOREPLY = re.compile(
    rb"(?:[0-9]+\+)?[a-z0-9](?:[a-z0-9-]{0,37}[a-z0-9])?(?:\[bot\])?"
    rb"@users\.noreply\.github\.com"
)
_ALLOW_ENTRY = re.compile(r"(\S(?:.*\S)?) <([^<>\s]+)>")


def _git(repo: Path, *args: str, stdin: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", *args], cwd=repo, input=stdin, capture_output=True, check=False,
    )
    if result.returncode:
        raise MetadataScanError("Git 引用或提交元数据不可读取")
    return result.stdout


def _commit_ids(repo: Path, *args: str) -> list[str]:
    raw = _git(repo, "rev-list", *args)
    ids = raw.decode("ascii").splitlines()
    if any(not _SHA.fullmatch(sha) for sha in ids):
        raise MetadataScanError("Git 提交列表无效")
    return ids


def _object_type(repo: Path, sha: str) -> str:
    return _git(repo, "cat-file", "-t", sha).decode("ascii").strip()


def _peeled_commit(repo: Path, rev: str) -> str | None:
    """rev 剥到提交；剥不到（指向 tree/blob 或对象不存在）返回 None。"""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", f"{rev}^{{commit}}"],
        cwd=repo, capture_output=True, check=False,
    )
    sha = result.stdout.decode("ascii", "replace").strip()
    return sha if result.returncode == 0 and _SHA.fullmatch(sha) else None


def _parse_identity(raw: bytes) -> Identity:
    match = _IDENTITY.fullmatch(raw)
    if match is None:
        raise MetadataScanError("Git 身份字段无效")
    name, email = match.groups()
    if not name or not email or any(c < 32 or c == 127 for c in name + email):
        raise MetadataScanError("Git 身份无效")
    try:
        (name + email).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MetadataScanError("Git 身份编码无效") from exc
    return name, email


def _header_values(raw_object: bytes, key: bytes) -> list[bytes]:
    header = raw_object.split(b"\n\n", 1)[0]
    return [line[len(key):] for line in header.split(b"\n") if line.startswith(key)]


def _commit_identities(raw_commit: bytes) -> list[Identity]:
    """author + committer，外加 mergetag 头里内嵌的 tagger（合并签名标签时写进提交）。"""
    result: list[Identity] = []
    for key in (b"author ", b"committer "):
        lines = _header_values(raw_commit, key)
        if len(lines) != 1:
            raise MetadataScanError("Git 作者或提交者字段缺失")
        result.append(_parse_identity(lines[0]))
    # mergetag 是多行头：续行以一个空格开头，内嵌标签的 tagger 行即 " tagger …"。
    result.extend(_parse_identity(line) for line in _header_values(raw_commit, b" tagger "))
    return result


def _commit_bodies(repo: Path, shas: list[str]) -> list[bytes]:
    """一次 `git cat-file --batch` 读全部提交；任何对不上的输出都按不可核验处理。"""
    if not shas:
        return []
    out = _git(repo, "cat-file", "--batch",
               stdin=("\n".join(shas) + "\n").encode("ascii"))
    bodies: list[bytes] = []
    pos = 0
    for sha in shas:
        newline = out.find(b"\n", pos)
        fields = out[pos:newline].split(b" ") if newline >= 0 else []
        if len(fields) != 3 or fields[0] != sha.encode("ascii") or fields[1] != b"commit":
            raise MetadataScanError("Git 提交对象不可读取")
        size = int(fields[2])
        body = out[newline + 1:newline + 1 + size]
        if len(body) != size or out[newline + 1 + size:newline + 2 + size] != b"\n":
            raise MetadataScanError("Git 提交对象不可读取")
        bodies.append(body)
        pos = newline + 2 + size
    return bodies


def _tag_chain(repo: Path, sha: str) -> tuple[list[Identity], str, str]:
    """沿（可能嵌套的）附注标签链走到底：返回 (各层 tagger, 终点对象, 终点类型)。"""
    taggers: list[Identity] = []
    obj_type = _object_type(repo, sha)
    seen: set[str] = set()
    while obj_type == "tag":
        if sha in seen or len(seen) >= 32:
            raise MetadataScanError("Git 标签链异常")
        seen.add(sha)
        raw = _git(repo, "cat-file", "tag", sha)
        tagger_lines = _header_values(raw, b"tagger ")
        object_lines = _header_values(raw, b"object ")
        if len(tagger_lines) != 1 or len(object_lines) != 1:
            raise MetadataScanError("Git 标签 tagger 或目标字段缺失")
        taggers.append(_parse_identity(tagger_lines[0]))
        sha = object_lines[0].decode("ascii", "replace").strip()
        if not _SHA.fullmatch(sha):
            raise MetadataScanError("Git 标签目标无效")
        obj_type = _object_type(repo, sha)
    return taggers, sha, obj_type


def _forbidden_email_domain(email: bytes) -> bool:
    if email.count(b"@") != 1:
        return True
    domain = email.rsplit(b"@", 1)[1].lower().rstrip(b".")
    if domain.startswith(b"[") and domain.endswith(b"]"):
        domain = domain[1:-1]
    labels = domain.split(b".")
    return (
        not domain
        or not bool(re.fullmatch(rb"[a-z0-9.-]+", domain))
        or any(not label or label.startswith(b"-") or label.endswith(b"-")
               for label in labels)
        or bool(_NUMERIC_IPV4.fullmatch(domain))
        or len(labels) < 2                       # 单标签主机：localhost / build-01 / iZ…Z
        or labels[-1].isdigit()                  # 残缺或数字地址：10.0.1
        or labels[-1] in _PRIVATE_SUFFIXES
        or any(_CLOUD_HOST_LABEL.fullmatch(label) or _DASHED_IPV4.search(label)
               for label in labels)
    )


def _is_allowlisted(identity: Identity, allow: frozenset[Identity]) -> bool:
    if identity in allow:
        return True
    email = identity[1].lower()
    return email == b"noreply@github.com" or bool(_GITHUB_NOREPLY.fullmatch(email))


def load_identity_allowlist(
    entries: list[str] | tuple[str, ...] = (), env: dict[str, str] | None = None,
) -> frozenset[Identity]:
    """项目身份白名单：`Name <email>` 精确对；条目写错即不可核验，不静默忽略。"""
    env = os.environ if env is None else env
    raw = list(entries) + re.split(r"[;\n]", env.get("AIDUMEI_IDENTITY_ALLOWLIST", ""))
    allow: set[Identity] = set()
    for entry in raw:
        entry = entry.strip()
        if not entry:
            continue
        match = _ALLOW_ENTRY.fullmatch(entry)
        if match is None:
            raise MetadataScanError("身份白名单条目无效")
        allow.add((match.group(1).encode("utf-8"), match.group(2).encode("utf-8")))
    return frozenset(allow)


def _word_hits(name: bytes, email: bytes, words: list[str]) -> int:
    # Metadata must never honor the file-only release-scan:allow marker.
    count = 0
    for field in (name, email):
        hits, waived = scan_bytes(field, words)
        count += sum(hits.values()) + sum(waived.values())
    return count


@dataclass
class _Policy:
    words: list[str]
    previously_public: set[Identity]
    require_allowlist: bool = False
    allow: frozenset[Identity] = frozenset()


def _count_identity(counts: ScanCounts, policy: _Policy, identity: Identity) -> None:
    name, email = identity
    counts.identities += 1
    hits = _word_hits(name, email, policy.words)
    if identity in policy.previously_public:
        counts.inherited_identities += 1
        counts.inherited_word_hits += hits
    else:
        counts.new_identity_word_hits += hits
    counts.forbidden_domains += int(_forbidden_email_domain(email))
    if policy.require_allowlist and not _is_allowlisted(identity, policy.allow):
        counts.not_allowlisted += 1


def _public_identities(repo: Path, *revs: str) -> set[Identity]:
    identities: set[Identity] = set()
    for body in _commit_bodies(repo, _commit_ids(repo, *revs)):
        identities.update(_commit_identities(body))
    return identities


def _scan_commits(repo: Path, shas: list[str], counts: ScanCounts, policy: _Policy) -> None:
    for body in _commit_bodies(repo, shas):
        counts.commits += 1
        for identity in _commit_identities(body):
            _count_identity(counts, policy, identity)


def _tag_ref(tag: str) -> str:
    if not tag or tag.startswith("-"):
        raise MetadataScanError("标签名无效")
    return tag if tag.startswith("refs/") else f"refs/tags/{tag}"


def scan_commit_metadata(
    repo: Path, base: str, head: str, words: list[str], *,
    tags: list[str] | tuple[str, ...] = (), tags_in_range: bool = False,
    require_allowlist: bool = False, allow: frozenset[Identity] = frozenset(),
    allow_diverged_base: bool = False,
) -> ScanCounts:
    """BASE..HEAD 的新提交 + 指定/落在新范围内的附注标签（tagger）。"""
    if not words:
        raise MetadataScanError("全量词表为空")
    repo = repo.resolve()
    if subprocess.run(
        ["git", "merge-base", "--is-ancestor", base, head],
        cwd=repo, capture_output=True, check=False,
    ).returncode:
        if not allow_diverged_base:
            raise MetadataScanError("公开基线不是候选提交的祖先")
        # A public release may squash private development. BASE..HEAD still
        # scans EVERY candidate commit absent from the actual public history.
        # Require shared provenance, but never substitute the common ancestor
        # for the actual public identity set or the declared scan range.
        if not _git(repo, "merge-base", base, head).strip():
            raise MetadataScanError("公开基线与候选没有共同来源")

    policy = _Policy(words, _public_identities(repo, base), require_allowlist, allow)
    counts = ScanCounts()
    new_commits = _commit_ids(repo, f"{base}..{head}")
    _scan_commits(repo, new_commits, counts, policy)

    tag_refs = [_tag_ref(tag) for tag in tags]
    if tags_in_range:
        # 落在新范围内（或正指着 HEAD）的附注标签一定是新的：发布流程里
        # 标签常打在已推的 HEAD 上，所以 HEAD 本身也算进窗口。
        window = set(new_commits)
        head_commit = _peeled_commit(repo, head)
        if head_commit is None:
            raise MetadataScanError("HEAD 不可剥成提交")
        window.add(head_commit)
        listing = _git(repo, "for-each-ref", "--format=%(objecttype) %(refname)", "refs/tags")
        for line in listing.decode("utf-8").splitlines():
            obj_type, _, refname = line.partition(" ")
            if obj_type == "tag" and _peeled_commit(repo, refname) in window:
                tag_refs.append(refname)
    for refname in dict.fromkeys(tag_refs):
        counts.refs += 1
        sha = _git(repo, "rev-parse", "--verify", refname).decode("ascii").strip()
        if not _SHA.fullmatch(sha):
            raise MetadataScanError("Git 标签引用无效")
        taggers, _, _ = _tag_chain(repo, sha)
        counts.tags += len(taggers)
        for identity in taggers:
            _count_identity(counts, policy, identity)
    return counts


def scan_pre_push(
    repo: Path, remote: str, ref_lines: list[str], words: list[str], *,
    require_allowlist: bool = False, allow: frozenset[Identity] = frozenset(),
) -> ScanCounts:
    """git pre-push 钩子的 stdin：每个被推送的引用都扫，分支与标签一个不跳。

    每行 `<local ref> <local sha> <remote ref> <remote sha>`。附注标签扫 tagger，
    新提交（远端跟踪引用与远端已有对象都够不着的）扫 author + committer。
    删除引用不推送任何对象，只计数。
    """
    if not words:
        raise MetadataScanError("全量词表为空")
    if not remote or remote.startswith("-") or any(c in remote for c in "*?[\\"):
        raise MetadataScanError("远端名无效")
    repo = repo.resolve()
    counts = ScanCounts()
    known_remote: list[str] = []
    tips: list[str] = []
    taggers: list[Identity] = []
    for line in ref_lines:
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 4:
            raise MetadataScanError("pre-push 输入行无效")
        _local_ref, local_sha, _remote_ref, remote_sha = parts
        if not (_SHA.fullmatch(local_sha) and _SHA.fullmatch(remote_sha)):
            raise MetadataScanError("pre-push 对象名无效")
        counts.refs += 1
        if not _ZERO_SHA.fullmatch(remote_sha):
            remote_commit = _peeled_commit(repo, remote_sha)
            if remote_commit:
                known_remote.append(remote_commit)
        if _ZERO_SHA.fullmatch(local_sha):
            continue
        chain, final_sha, final_type = _tag_chain(repo, local_sha)
        taggers.extend(chain)
        counts.tags += len(chain)
        if final_type == "commit":
            tips.append(final_sha)

    exclude = [f"--remotes={remote}", *dict.fromkeys(known_remote)]
    policy = _Policy(words, _public_identities(repo, *exclude),
                     require_allowlist, allow)
    if tips:
        _scan_commits(repo, _commit_ids(repo, *dict.fromkeys(tips), "--not", *exclude),
                      counts, policy)
    for identity in taggers:
        _count_identity(counts, policy, identity)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="upstream/main")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--allow-diverged-base", action="store_true",
                        help="accept a related public sibling; still scan all BASE..HEAD commits")
    parser.add_argument("--tag", action="append", default=[],
                        help="also scan the tagger chain of this tag (repeatable)")
    parser.add_argument("--tags-in-range", action="store_true",
                        help="also scan annotated tags pointing at HEAD or into BASE..HEAD")
    parser.add_argument("--pre-push", metavar="REMOTE",
                        help="read git pre-push lines from stdin; scan every pushed ref")
    parser.add_argument("--require-allowlist", action="store_true",
                        help="every scanned identity must be GitHub noreply or allowlisted")
    parser.add_argument("--allow-identity", action="append", default=[],
                        metavar="'NAME <EMAIL>'", help="exact project identity (repeatable)")
    args = parser.parse_args(argv)
    try:
        words = load_words()
        allow = load_identity_allowlist(args.allow_identity)
        if args.pre_push is not None:
            lines = sys.stdin.buffer.read().decode("utf-8").splitlines()
            counts = scan_pre_push(Path.cwd(), args.pre_push, lines, words,
                                   require_allowlist=args.require_allowlist, allow=allow)
        else:
            counts = scan_commit_metadata(
                Path.cwd(), args.base, args.head, words, tags=args.tag,
                tags_in_range=args.tags_in_range,
                require_allowlist=args.require_allowlist, allow=allow,
                allow_diverged_base=args.allow_diverged_base)
    except (MetadataScanError, OSError, UnicodeError, RuntimeError, ValueError):
        print("提交元数据扫描：不可核验，停推")
        return 2
    allowlist = str(counts.not_allowlisted) if args.require_allowlist else "未启用"
    print(
        "提交元数据扫描："
        f"新增提交={counts.commits}，身份字段={counts.identities}，"
        f"已公开同身份={counts.inherited_identities}，"
        f"已公开身份词命中={counts.inherited_word_hits}，"
        f"新身份硬命中={counts.new_identity_word_hits}，"
        f"禁止邮箱域={counts.forbidden_domains}，"
        f"扫描引用={counts.refs}，标签对象={counts.tags}，"
        f"白名单外身份={allowlist}"
    )
    return 1 if counts.blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
