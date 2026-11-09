#!/usr/bin/env python3
"""Claude Code 自带记忆每日兜底扫（f0.8 ①）。

钩子（aidumem-cc-memory-bridge.sh）只管被 Write/Edit 的那一个文件；本脚本把
整个 CC 记忆目录过一遍，做同样的事，外加「CC 那边保持精简」：

- 打码：任何 .md 有密钥/证件片段 → 原地打码（--apply 才改）。
- 入库：主题文件按钩子同一套去重/取代规则写进 aiduMEI（--apply 才写）。
- 裁剪（--apply --trim 才动文件，否则只报告）：
  - 已导入、内容与导入时一致、且超过 N 天（默认 14）没改的主题文件，
    正文换成一行存根「已存入 aiduMEI：<标题>」（frontmatter 保留，CC 仍认得它）；
    改之前原文备份到 ~/.aidumem/cc-bridge-trash/<时间>/。
  - MEMORY.md 索引超过上限（默认 80 行）时，先摘指向存根文件的条目，
    仍超就从最早的条目摘起——只摘所指文件已导入 aiduMEI 的条目，摘掉的写进备份。

幂等：重复执行只做还没做的事。默认 --dry-run（只报告）。

用法：
    python scripts/cc_memory_sweep.py                       # 所有项目，只报告
    python scripts/cc_memory_sweep.py --apply               # 打码 + 入库
    python scripts/cc_memory_sweep.py --apply --trim        # 再裁剪
    python scripts/cc_memory_sweep.py --dir ~/.claude/projects/<项目>/memory --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

def _load_bridge():
    """钩子真源在 integrations/claude-code/（独立集成件，文件名不是包路径），按文件加载。"""
    import importlib.util
    src = Path(__file__).resolve().parents[1] / "integrations" / "claude-code" / "cc_memory_bridge.py"
    spec = importlib.util.spec_from_file_location("aidumei_cc_memory_bridge", src)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


B = _load_bridge()

_LINK_RE = re.compile(r"\]\(([^)\s]+\.md)\)")


def memory_dirs(explicit: list[str] | None) -> list[Path]:
    if explicit:
        return [Path(os.path.expanduser(d)) for d in explicit]
    projects = B.claude_home() / "projects"
    out = []
    if projects.is_dir():
        out = sorted(p / "memory" for p in projects.iterdir()
                     if p.is_dir() and not p.is_symlink() and (p / "memory").is_dir())
    custom = B.auto_memory_setting()  # 用户级 autoMemoryDirectory（项目级的请用 --dir）
    if custom is not None and custom.is_dir() and custom not in out:
        out.append(custom)
    return out


def md_files(root: Path) -> list[Path]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for fn in sorted(filenames):
            real = B.resolve_inside(Path(dirpath) / fn, root)
            if real is not None:
                out.append(real)
    return out


def _trash_dir(stamp: str) -> Path:
    base = os.environ.get("AIDUMEI_CC_BRIDGE_TRASH") or os.path.join(
        os.environ.get("HOME") or str(Path.home()), ".aidumem", "cc-bridge-trash")
    return Path(base) / stamp


def _backup(path: Path, root: Path, text: str, stamp: str) -> Path:
    dst = _trash_dir(stamp) / root.parent.name / path.relative_to(root)
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(dst.parent, 0o700)
    dst.write_text(text, encoding="utf-8")
    os.chmod(dst, 0o600)
    return dst


def _imported(state: dict, ident: dict, root: Path, path: Path, text: str) -> bool:
    rec = state.get(B.state_key(ident, root, path.relative_to(root).as_posix())) or {}
    return rec.get("status") in ("imported", "duplicate") and rec.get("sha") == B.content_sha(text)


def stub_text(text: str, title: str) -> str:
    _, fm_block, _ = B.split_frontmatter(text)
    return (fm_block if fm_block.endswith("\n") or not fm_block else fm_block + "\n") + f"{B.STUB_PREFIX}{title}\n"


def trim_topic(path: Path, root: Path, text: str, state: dict, ident: dict, *, days: int,
               apply: bool, stamp: str, now: float) -> dict | None:
    """够老、已导入、未变的主题文件 → 存根。返回动作说明或 None。"""
    if path.name == B.INDEX_NAME or B.is_stub(text) or not _imported(state, ident, root, path, text):
        return None
    age_days = (now - path.stat().st_mtime) / 86400
    if age_days < days:
        return None
    title = B.file_title(path, text)
    act = {"action": "stub" if apply else "would_stub", "file": path.relative_to(root).as_posix(),
           "age_days": int(age_days)}
    if apply:
        act["backup"] = str(_backup(path, root, text, stamp))
        B.atomic_rewrite(path, stub_text(text, title))
    return act


def _index_entries(lines: list[str]) -> list[int]:
    return [i for i, ln in enumerate(lines) if ln.lstrip().startswith(("-", "*")) and _LINK_RE.search(ln)]


def trim_index(idx: Path, root: Path, state: dict, ident: dict, *, cap: int, apply: bool,
               stamp: str) -> dict | None:
    """MEMORY.md 超上限时摘条目（只摘所指文件已导入/已存根的）。"""
    text = B._read_nofollow(idx)
    lines = text.splitlines()
    if len(lines) <= cap:
        return None

    def target(i: int) -> Path | None:
        m = _LINK_RE.search(lines[i])
        if not m:
            return None
        return B.resolve_inside((root / m.group(1)).absolute(), root)

    def removable(i: int) -> tuple[bool, bool]:
        t = target(i)
        if t is None:
            return False, False
        tt = B._read_nofollow(t)
        stub = B.is_stub(tt)
        return stub or _imported(state, ident, root, t, tt), stub

    entries = _index_entries(lines)
    info = {i: removable(i) for i in entries}
    order = [i for i in entries if info[i][1]] + [i for i in entries if info[i][0] and not info[i][1]]
    need = len(lines) - cap
    drop = set(order[:need])
    act = {"action": "cap_index" if apply else "would_cap_index", "file": B.INDEX_NAME,
           "lines": len(lines), "cap": cap, "drop": len(drop)}
    if len(drop) < need:
        act["over_cap_unimported"] = need - len(drop)
    if not drop:
        act["action"] = "index_over_cap"
        return act
    if apply:
        act["backup"] = str(_backup(idx, root, text, stamp))
        kept = [ln for i, ln in enumerate(lines) if i not in drop]
        B.atomic_rewrite(idx, "\n".join(kept) + ("\n" if text.endswith("\n") else ""))
    return act


def sweep_dir(root: Path, client, state: dict, *, apply: bool, trim: bool, days: int, cap: int,
              stamp: str, now: float) -> list[dict]:
    if root.is_symlink() or not root.is_dir():
        return [{"action": "skip", "reason": "not_a_dir", "dir": str(root)}]
    root = root.resolve()
    acts: list[dict] = []
    for path in md_files(root):
        rel = path.relative_to(root).as_posix()
        text, kinds = B.redact_file(path, apply=apply)
        if kinds:
            acts.append({"action": "redacted" if apply else "would_redact", "file": rel, "kinds": kinds})
        res = B.ingest_file(path, root, text, client, state, apply=apply)
        if res.get("action") != "skip":
            acts.append(res)
        if trim:
            t = trim_topic(path, root, text, state, client.ident, days=days, apply=apply, stamp=stamp, now=now)
            if t:
                acts.append(t)
    idx = root / B.INDEX_NAME
    if trim and B.resolve_inside(idx, root) is not None:
        t = trim_index(idx, root, state, client.ident, cap=cap, apply=apply, stamp=stamp)
        if t:
            acts.append(t)
    for a in acts:
        a.setdefault("project", root.parent.name)
    return acts


def main(argv: list[str] | None = None, client=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", action="append", help="只扫这个记忆目录（可多次）；缺省 ~/.claude/projects/*/memory")
    ap.add_argument("--apply", action="store_true", help="真改（打码/入库）；缺省只报告")
    ap.add_argument("--dry-run", action="store_true", help="只报告（缺省即是）")
    ap.add_argument("--trim", action="store_true", help="裁剪：存根旧主题文件、索引封顶（与 --apply 同用才改文件）")
    B.redact_text("")  # 确保打码器（及其 ducky 命名空间壳）已加载
    from ducky.env_config import int_env
    ap.add_argument("--days", type=int, default=int_env("AIDUMEI_CC_TRIM_DAYS", 14, minimum=0))
    ap.add_argument("--index-cap", type=int, default=int_env("AIDUMEI_CC_INDEX_CAP", 80, minimum=5))
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    apply = a.apply and not a.dry_run
    client = client or B.Client(B.identity())
    state = B.load_state()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    now = time.time()
    dirs = memory_dirs(a.dir)
    acts: list[dict] = []
    for d in dirs:
        acts += sweep_dir(d, client, state, apply=apply, trim=a.trim, days=max(0, a.days),
                          cap=max(5, a.index_cap), stamp=stamp, now=now)
    if apply:
        B.save_state(state)
    errors = [x for x in acts if x.get("action") == "error"]
    report = {"mode": "apply" if apply else "dry-run", "trim": a.trim, "dirs": len(dirs),
              "actions": acts, "errors": len(errors)}
    if a.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(f"[cc-memory-sweep] {report['mode']}{' +trim' if a.trim else ''} · 目录 {len(dirs)} · "
              f"动作 {len(acts)} · 失败 {len(errors)}")
        for x in acts:
            extra = ", ".join(f"{k}={x[k]}" for k in ("reason", "kinds", "superseded", "age_days", "drop",
                                                       "over_cap_unimported", "code") if k in x)
            print(f"  {x.get('action'):<16} {x.get('project', '')}/{x.get('file', x.get('dir', ''))}"
                  + (f"  ({extra})" if extra else ""))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
