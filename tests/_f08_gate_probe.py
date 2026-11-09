"""f0.8 ② HTTP probe (run by tests/test_f08_gate_http.py in a subprocess: api_server reads env at import).

usage: python tests/_f08_gate_probe.py  → one JSON line on stdout.
"""
import json
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AIDUMEI_MEMORY_KERNEL"] = "native"
from benchmarks.regression import Adapter  # noqa: E402

ad = Adapter()
c, d = ad.client, ad.tmp
out = {}
SUMMARY_MD = {"kind": "session_distill", "lane": "distill", "_origin_agent": "session-distill",
              "distill_mode": "llm", "distill_source_count": 3}


def H(uid):
    return {"X-Caller-User-Id": uid}


def add(uid, msgs, md=None):
    if isinstance(msgs, str):
        msgs = [{"role": "user", "content": msgs}]
    r = c.post("/add", json={"messages": msgs, "user_id": uid, "caller_user_id": uid, "infer": False,
                             "metadata": md or {}}, headers=H(uid))
    return r.status_code, r.json()


def q(db, sql, args=()):
    con = sqlite3.connect(os.path.join(d, db))
    try:
        return [list(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def live(uid, needle):
    return q("memory.db", "SELECT count(*) FROM memories WHERE user_id=? AND content LIKE ?",
             (uid, f"%{needle}%"))[0][0]


def residue(needle, skip=("tombstones",)):
    hits = {}
    for root, _dirs, files in os.walk(d):
        for name in sorted(files):
            path = os.path.join(root, name)
            rel = os.path.relpath(path, d)
            if name.endswith(("-wal", "-shm", "-journal")):
                continue
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            if data.startswith(b"SQLite format 3"):
                con = sqlite3.connect(path)
                try:
                    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                        if t in skip:
                            continue
                        for col in [x[1] for x in con.execute(f'PRAGMA table_info("{t}")')]:
                            n = con.execute(f'SELECT COUNT(*) FROM "{t}" WHERE CAST("{col}" AS TEXT) LIKE ?',
                                            (f"%{needle}%",)).fetchone()[0]
                            if n:
                                hits[f"{rel}:{t}.{col}"] = n
                except sqlite3.Error:
                    pass
                finally:
                    con.close()
            elif needle.encode() in data:
                hits[rel] = data.count(needle.encode())
    return hits


IMG = "[Image: original 1357x913, displayed at 1357x913.]"

# 1) P1 machine noise: not live, tombstoned for 7 days, restorable
st, body = add("gina", IMG)
out["img_status"] = [st, body.get("action"), [g["principle"] + ":" + g["reason"] for g in body.get("gated", [])]]
out["img_live"] = live("gina", "1357x913")
tid = body["gated"][0]["tombstone_id"]
row = q("facts.db", "SELECT reason, actor, snapshot_expires_at, content_snapshot FROM tombstones WHERE tombstone_id=?",
        (tid,))[0]
out["img_tomb"] = [row[0], row[1], round((row[2] - time.time()) / 86400), row[3] == IMG]
out["img_listed"] = any(t.get("tombstone_id") == tid for t in c.get(
    "/api/tombstones", params={"user_id": "gina"}, headers=H("gina")).json().get("results", []))
out["img_verdict"] = q("write_policy.db", "SELECT verdict, mode, reasons FROM verdicts WHERE memory_id=?",
                       (body["gated"][0]["memory_id"],))
rr = c.post("/api/tombstone/restore", json={"tombstone_id": tid, "user_id": "gina"}, headers=H("gina")).json()
out["img_restore"] = rr.get("status")
out["img_live_after_restore"] = live("gina", "1357x913")

# 2) a mixed turn: the noise goes, the real sentence stays
st, body = add("gina", [{"role": "assistant", "content": "[SILENT]"},
                        {"role": "user", "content": "我答应周末陪家人去湖边，说好了不再加班"}])
out["mixed"] = [st, len(body.get("gated", [])), live("gina", "陪家人去湖边"), live("gina", "[SILENT]")]

# 3) continuation notice
st, body = add("gina", "This session is being continued from a previous conversation that ran out of context.")
out["continuation"] = [st, body.get("action"), live("gina", "being continued")]

# 4) short confirmations are stored, in the daily layer
for t in ("好，那走吧", "改"):
    add("gina", t)
v = c.get("/api/write-policy/verdicts", params={"user_id": "gina"}, headers=H("gina")).json()["verdicts"]
out["short"] = [[x["verdict"], x["layer"], x["mode"]] for x in v[:2]]
out["short_live"] = [live("gina", "好，那走吧"), live("gina", "改")]

# 5) thinking text on the session-summary path: gated; the same text as a fallback summary is stored
think = "We need to output one or two sentences summarizing the session in Chinese, under 80 characters."
st, body = add("gina", [{"role": "assistant", "content": think}], SUMMARY_MD)
out["thinking"] = [st, body.get("action"), [g["principle"] for g in body.get("gated", [])], live("gina", "We need to")]

# 6) P8: a forget rule drops the write and keeps no text anywhere (tombstones included)
fr = c.post("/api/forget", json={"user_id": "gina", "memory_ids": ["00000000-0000-0000-0000-000000000000"],
                                 "never_store": "青石巷 九号"}, headers=H("gina")).json()
st, body = add("gina", "我的旧住址是青石巷九号二单元")
out["p8"] = [fr.get("rule_id") is not None, st, body.get("action"),
             [g["principle"] for g in body.get("gated", [])], residue("九号二单元", skip=())]

# 7) each enforced principle can be turned back to shadow by env
os.environ["AIDUMEI_WRITE_POLICY_P1"] = "shadow"
st, body = add("hank", "[SILENT]")
out["p1_shadow"] = [st, body.get("action"), "gated" in body, live("hank", "[SILENT]")]
os.environ.pop("AIDUMEI_WRITE_POLICY_P1")
os.environ["AIDUMEI_WRITE_POLICY_P8"] = "shadow"
c.post("/api/forget", json={"user_id": "hank", "memory_ids": ["00000000-0000-0000-0000-000000000000"],
                            "never_store": "青石巷"}, headers=H("hank"))
st, body = add("hank", "我的旧住址是青石巷九号二单元")
out["p8_shadow"] = [st, "gated" in body, live("hank", "青石巷")]
os.environ.pop("AIDUMEI_WRITE_POLICY_P8")
print(json.dumps(out, ensure_ascii=False))
