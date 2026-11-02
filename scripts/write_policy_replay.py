#!/usr/bin/env python3
"""Replay an existing aiduMEI data directory through the f0.7+ write policy.

Read-only: opens the databases with mode=ro and writes nothing back.  Use it
on a *copy* of production data to see what the shadow gate would have said
about memories that were stored before it existed.

  python scripts/write_policy_replay.py --data-dir DIR --user USER [--samples FILE]

stdout: one JSON object with counts (verdicts, layers, sources, supersede /
pending decisions, P7 redactions and hard blocks over memories *and* the
verbatim tables).  --samples writes per-memory details including short text
previews to FILE (0600) for a human review; keep that file off any repo.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _grams(text: str) -> set:
    t = "".join((text or "").split())[:200]
    return {t[i:i + 2] for i in range(len(t) - 1)}


def replay(data_dir: str, user: str, bank: str = "default") -> tuple[dict, list]:
    from ducky import write_policy as wp
    mem = _ro(os.path.join(data_dir, "memory.db"))
    rows = mem.execute(
        "SELECT id, content, metadata, memory_type, created_at FROM memories WHERE user_id=? AND bank_id=?"
        " AND memory_type!='VERBATIM' ORDER BY created_at, mid", (user, bank)).fetchall()
    stats = collections.defaultdict(collections.Counter)
    samples, prior = [], []
    for r in rows:
        try:
            md = json.loads(r["metadata"] or "{}")
        except ValueError:
            md = {}
        md = md if isinstance(md, dict) else {}
        role = md.get("role") or "user"
        text = r["content"] or ""
        v = wp.classify(text, role, md)
        red, kinds = wp.redact_forbidden(text)
        hb = wp.hard_block_reason(red, summary=wp.legacy_inferred(md))
        stats["verdict"][v["verdict"]] += 1
        stats["layer"][v["layer"]] += 1
        stats["source"][v["source_type"]] += 1
        stats["role"][role] += 1
        for reason in v["reasons"]:
            stats["reason"][reason.split(":")[0] if not reason.startswith("noise") else reason] += 1
        for k in kinds:
            stats["redact_kind"][k] += 1
        if kinds:
            stats["redact"]["memories_with_redaction"] += 1
        if hb:
            stats["hard_block"][hb] += 1
        if v["condition"]:
            stats["condition"]["with_condition"] += 1
        sup = []
        if v["verdict"] != "reject" and (role == "user" or v["source_type"] == "inferred"):
            g = _grams(text)
            keys = set(wp.topic_keys(text))
            pool = [p for p in prior[-400:] if (keys & p["keys"]) or len(g & p["grams"]) >= max(3, len(g) // 4)]
            sup = wp.supersede_candidates(text, {**md, "_created_at": r["created_at"] or ""},
                                          [{"id": p["id"], "content": p["content"], "metadata": p["md"],
                                            "created_at": p["created_at"]} for p in reversed(pool)])
        for s in sup:
            stats["supersede"][s["decision"] + ":" + s["kind"]] += 1
        if sup:
            stats["supersede"]["memories_with_candidate"] += 1
        prior.append({"id": r["id"], "content": text, "md": md, "created_at": r["created_at"],
                      "keys": set(wp.topic_keys(text)), "grams": _grams(text)})
        samples.append({"id": r["id"], "created_at": r["created_at"], "type": r["memory_type"], "role": role,
                        "len": len(text), "verdict": v["verdict"], "layer": v["layer"],
                        "source": v["source_type"], "reasons": v["reasons"], "condition": v["condition"],
                        "redact": kinds, "hard_block": hb, "supersede": sup,
                        "preview": " ".join(text.split())[:120]})
    # P7 over the verbatim layers too (they hold the raw turns).
    for db, table, col in (("memory.db", "memories", "content"), ("facts.db", "verbatim_turns", "content"),
                           ("memory.db", "verbatim_turns", "content")):
        path = os.path.join(data_dir, db)
        if not os.path.exists(path):
            continue
        conn = _ro(path)
        try:
            cols = {x[1] for x in conn.execute(f'PRAGMA table_info("{table}")')}
            if col not in cols:
                continue
            where = " WHERE memory_type='VERBATIM'" if table == "memories" else ""
            n = 0
            for (t,) in conn.execute(f'SELECT "{col}" FROM "{table}"{where}'):
                n += 1
                red, kinds = wp.redact_forbidden(t or "")
                for k in kinds:
                    stats[f"verbatim_redact:{db}:{table}"][k] += 1
                hb = wp.hard_block_reason(red)
                if hb:
                    stats[f"verbatim_hard_block:{db}:{table}"][hb] += 1
                stats["verbatim_len_max"][f"{db}:{table}"] = max(stats["verbatim_len_max"][f"{db}:{table}"],
                                                                 len(t or ""))
            stats["verbatim_rows"][f"{db}:{table}"] = n
        except sqlite3.Error as exc:
            stats["errors"][f"{db}:{table}:{type(exc).__name__}"] += 1
        finally:
            conn.close()
    out = {"user": user, "bank": bank, "memories": len(rows), **{k: dict(v) for k, v in stats.items()}}
    return out, samples


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--user", required=True)
    ap.add_argument("--bank", default="default")
    ap.add_argument("--samples", default="")
    a = ap.parse_args(argv)
    out, samples = replay(a.data_dir, a.user, a.bank)
    print(json.dumps(out, ensure_ascii=False))
    if a.samples:
        fd = os.open(a.samples, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for s in samples:
                fh.write(json.dumps(s, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
