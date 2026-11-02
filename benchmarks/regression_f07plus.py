"""f0.7+ regression: four "what is worth remembering" categories.

Same offline harness as benchmarks/regression.py (in-process app, hash
embeddings, no network, zero tokens); only the HTTP contract is used:
/add, /search, /api/facts, /api/forget, /api/write-policy/verdicts.

  noise_not_stored     noise → shadow verdict reject; its paired real statement → not reject
  inference_not_fact   a session summary comes back marked inferred; a direct statement does not
  forget_no_resurrect  after /api/forget: absent from /search (natural + exact) and /api/facts;
                       writing it again hits the never-store rule
  supersede_old        the later statement is logged as superseding (or pending confirmation of)
                       the earlier one; unrelated statements are not

Run:  python -m benchmarks.regression_f07plus [--out f.json] [--baseline f.json]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

CASES_PATH = Path(__file__).parent / "regression_f07plus.json"
CATEGORIES = ("noise_not_stored", "inference_not_fact", "forget_no_resurrect", "supersede_old")
SUMMARY_MD = {"kind": "session_distill", "lane": "distill", "_origin_agent": "session-distill",
              "distill_source_count": 3}


class Harness:
    def __init__(self):
        from benchmarks.regression import Adapter
        self.ad = Adapter()
        self.c = self.ad.client
        self.n = 0

    def user(self) -> str:
        self.n += 1
        return f"u_wp{self.n:03d}"

    def add(self, uid: str, text: str, md: dict | None = None) -> int:
        r = self.c.post("/add", json={"messages": [{"role": "user", "content": text}], "user_id": uid,
                                      "infer": False, "metadata": dict(md or {})},
                        headers={"X-Caller-User-Id": uid})
        return r.status_code

    def verdicts(self, uid: str, limit: int = 50) -> list[dict]:
        r = self.c.get("/api/write-policy/verdicts", params={"user_id": uid, "limit": limit},
                       headers={"X-Caller-User-Id": uid})
        return r.json().get("verdicts", []) if r.status_code == 200 else []

    def search(self, uid: str, q: str, k: int = 5) -> list[dict]:
        r = self.c.post("/search", json={"query": q, "user_id": uid, "limit": k},
                        headers={"X-Caller-User-Id": uid})
        return r.json().get("results", []) if r.status_code == 200 else []

    def facts_text(self, uid: str) -> str:
        return self.c.get("/api/facts", params={"user_id": uid}, headers={"X-Caller-User-Id": uid}).text


def _row_for(rows: list[dict], text: str) -> dict | None:
    for r in rows:
        if text in str(r.get("memory") or ""):
            return r
    return None


def judge(h: Harness, c: dict) -> bool:
    cat = c["category"]
    uid = h.user()
    if cat == "noise_not_stored":
        h.add(uid, c["noise"])
        h.add(uid, c["control"])
        v = list(reversed(h.verdicts(uid)))            # oldest first
        mem_rows = [x for x in v if x.get("memory_id")]
        if len(mem_rows) < 2:
            return False
        return mem_rows[0]["verdict"] == "reject" and mem_rows[1]["verdict"] != "reject"
    if cat == "inference_not_fact":
        h.add(uid, c["summary"], SUMMARY_MD)
        h.add(uid, c["direct"])
        s = _row_for(h.search(uid, c["summary_query"]), c["summary"])
        d = _row_for(h.search(uid, c["direct_query"]), c["direct"])
        return bool(s and s.get("inferred") is True and d and d.get("inferred") is False)
    if cat == "forget_no_resurrect":
        h.add(uid, c["text"])
        before = _row_for(h.search(uid, c["query"]), c["exact"]) or _row_for(h.search(uid, c["exact"]), c["exact"])
        if not before:
            return False                                   # precondition: it was recallable
        listing = h.c.post("/api/forget", json={"user_id": uid, "query": c["exact"]},
                           headers={"X-Caller-User-Id": uid}).json()
        ids = [x["id"] for x in listing.get("candidates", []) if c["exact"] in x.get("preview", "")]
        r = h.c.post("/api/forget", json={"user_id": uid, "memory_ids": ids, "never_store": c["rule"]},
                     headers={"X-Caller-User-Id": uid})
        if r.status_code != 200 or r.json().get("status") != "ok":
            return False
        gone = not any(c["exact"] in json.dumps(x, ensure_ascii=False)
                       for q in (c["query"], c["exact"]) for x in h.search(uid, q))
        gone = gone and c["exact"] not in h.facts_text(uid)
        h.add(uid, c["text"])
        latest = h.verdicts(uid, limit=1)
        return gone and bool(latest) and latest[0].get("blacklist_rule") is not None
    if cat == "supersede_old":
        h.add(uid, c["old"], SUMMARY_MD if c.get("old_inferred") else None)
        time.sleep(1.05)                                   # distinct second: ties are a separate rule
        h.add(uid, c["new"], SUMMARY_MD if c.get("new_inferred") else None)
        latest = h.verdicts(uid, limit=1)
        sup = (latest[0].get("supersede") or []) if latest else []
        if c["expect"] == "none":
            return not sup
        return any(s.get("decision") == c["expect"] for s in sup)
    raise ValueError(cat)


def run(path: Path = CASES_PATH) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    h = Harness()
    t0 = time.time()
    per: dict[str, list] = {k: [] for k in CATEGORIES}
    fails = []
    for c in data["cases"]:
        ok = judge(h, c)
        per[c["category"]].append(ok)
        if not ok:
            fails.append(c["id"])
    scores = {k: round(sum(v) / len(v), 4) if v else None for k, v in per.items()}
    passed = sum(sum(v) for v in per.values())
    total = sum(len(v) for v in per.values())
    return {"schema": "aidumei-regression-f07plus-result", "total": total, "passed": passed,
            "score": round(passed / total, 4) if total else 0.0, "per_category": scores,
            "failed_ids": fails, "seconds": round(time.time() - t0, 1)}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="")
    ap.add_argument("--baseline", default="")
    a = ap.parse_args(argv)
    res = run()
    print(json.dumps(res, ensure_ascii=False))
    if a.out:
        Path(a.out).write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    if a.baseline:
        base = json.loads(Path(a.baseline).read_text(encoding="utf-8"))
        bad = [k for k, v in (base.get("per_category") or {}).items()
               if v is not None and (res["per_category"].get(k) or 0) < v]
        if bad:
            print("REGRESSION:", ",".join(bad), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
