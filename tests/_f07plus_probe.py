"""f0.7+ HTTP probe (run by tests/test_f07plus_http.py in a subprocess: api_server reads env at import).

usage: python tests/_f07plus_probe.py  → one JSON line on stdout.
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AIDUMEI_MEMORY_KERNEL"] = "native"
from benchmarks.regression import Adapter  # noqa: E402

ad = Adapter()
c, d = ad.client, ad.tmp
out = {}
SECRET = "sk-proj-Zq8vT3mK9pL2xW7nR4bY6cJ1"


def H(uid):
    return {"X-Caller-User-Id": uid}


def add(uid, text, md=None, headers=None):
    return c.post("/add", json={"messages": [{"role": "user", "content": text}], "user_id": uid,
                                "caller_user_id": uid, "infer": False, "metadata": md or {}},
                  headers=headers or H(uid))


def residue(needle):
    """Every file under the data dir: SQLite tables cell by cell, anything else as raw bytes
    (mutation journal, WAL, job files...).  Tombstone snapshots are reported by the caller."""
    hits = {}
    for root, _dirs, files in os.walk(d):
        for name in sorted(files):
            path = os.path.join(root, name)
            rel = os.path.relpath(path, d)
            if name.endswith(("-wal", "-shm", "-journal")):
                continue      # SQLite physical page images: freed on checkpoint, not a logical copy
            try:
                with open(path, "rb") as fh:
                    head = fh.read(16)
                    data = head + fh.read()
            except OSError:
                continue
            if head.startswith(b"SQLite format 3"):
                con = sqlite3.connect(path)
                try:
                    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
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


def wp_rows(sql, args=()):
    con = sqlite3.connect(os.path.join(d, "write_policy.db"))
    try:
        return [list(r) for r in con.execute(sql, args)]
    finally:
        con.close()


# 1) P7: a credential inside a turn is redacted before any layer sees it
r = add("alice", f"新的模型密钥是 {SECRET} ，明天换到测试机上")
out["secret_add"] = r.status_code
out["secret_residue"] = residue("Zq8vT3mK9pL2xW7nR4bY6cJ1")
hits = c.post("/search", json={"query": "模型密钥 测试机", "user_id": "alice"}, headers=H("alice")).json()["results"]
out["secret_redacted_recall"] = any("[redacted:api_key]" in (x.get("memory") or "") for x in hits)

# 2) P7: a raw e-mail copy is refused whole; the log keeps only reason + hash
mail = "From: a@example.com\nTo: b@example.com\nSubject: 季度报表\nDate: Mon, 2 Nov 2026\n\n正文：报表见附件 QX-77"
r = add("alice", mail)
out["mail_status"] = r.status_code
out["mail_residue"] = residue("QX-77")
out["mail_logged"] = wp_rows("SELECT hard_block, memory_id FROM verdicts WHERE hard_block IS NOT NULL")

# 3) shadow verdicts, provenance on recall, delete cascades our rows
add("alice", "好的")
_SUM_MD = {"kind": "session_distill", "lane": "distill", "_origin_agent": "session-distill", "distill_source_count": 3}
from ducky.distill_seal import SEAL_FIELD, seal  # noqa: E402  f0.8+（R3-05）：精华身份要服务端签名
add("alice", "用户可能偏好清淡口味，晚饭常点粥",
    {**_SUM_MD, SEAL_FIELD: seal("用户可能偏好清淡口味，晚饭常点粥", "alice", "default", _SUM_MD)})
v = c.get("/api/write-policy/verdicts", params={"user_id": "alice"}, headers=H("alice")).json()["verdicts"]
out["verdicts"] = [[x["verdict"], x["source_type"], x["layer"]] for x in reversed(v) if x["memory_id"]]
hits = c.post("/search", json={"query": "清淡口味 粥", "user_id": "alice"}, headers=H("alice")).json()["results"]
row = [x for x in hits if "清淡口味" in (x.get("memory") or "")][0]
out["inferred_flag"] = [row.get("inferred"), row.get("source_type"), row.get("basis")]
mid = row["id"]
del_res = c.post("/delete", json={"memory_id": mid, "user_id": "alice"}, headers=H("alice")).json()
out["delete_status"] = del_res.get("status")
out["verdict_rows_after_delete"] = wp_rows("SELECT count(*) FROM verdicts WHERE memory_id=?", (mid,))[0][0]

# 4) P8 forget: listing never deletes; ids delete for good; rule fires on the next write
add("alice", "我的旧住址是梧桐路十七号三单元")
lst = c.post("/api/forget", json={"user_id": "alice", "query": "梧桐路十七号"}, headers=H("alice")).json()
out["forget_listing"] = lst["status"]
ids = [x["id"] for x in lst["candidates"] if "梧桐路" in x["preview"]]
out["still_there_after_listing"] = bool(ids) and "梧桐路" in json.dumps(
    c.post("/search", json={"query": "梧桐路", "user_id": "alice"}, headers=H("alice")).json(), ensure_ascii=False)
fr = c.post("/api/forget", json={"user_id": "alice", "memory_ids": ids, "never_store": "梧桐路"},
            headers=H("alice")).json()
out["forget_status"] = fr["status"]
out["forget_search"] = "梧桐路" in json.dumps(
    c.post("/search", json={"query": "旧住址 梧桐路", "user_id": "alice"}, headers=H("alice")).json(), ensure_ascii=False)
out["forget_residue"] = {k: v for k, v in residue("梧桐路十七号").items() if "tombstones" not in k}
add("alice", "我的旧住址是梧桐路十七号三单元")
out["rule_hit"] = wp_rows("SELECT blacklist_rule FROM verdicts ORDER BY id DESC LIMIT 1")[0][0]
rules = c.get("/api/forget/rules", params={"user_id": "alice"}, headers=H("alice")).json()["rules"]
out["rule_delete"] = c.post("/api/forget/rules/delete", json={"user_id": "alice", "rule_id": rules[0]["id"]},
                            headers=H("alice")).json().get("deleted")
out["bob_cannot_list_alice_rules"] = c.get("/api/forget/rules", params={"user_id": "alice",
                                                                         "caller_user_id": "bob"},
                                           headers=H("bob")).status_code

# 5) N4: another tenant's id deletes nothing and is not reported as matched
add("erin", "erin 的周会改到周三上午")
eh = c.post("/search", json={"query": "周会 周三", "user_id": "erin"}, headers=H("erin")).json()["results"]
eid = [x["id"] for x in eh if "周会" in (x.get("memory") or "")][0]
sal = sqlite3.connect(os.path.join(d, "salience.db"))
before = sal.execute("SELECT count(*) FROM salience WHERE memory_id=?", (eid,)).fetchone()[0]
fx = c.post("/delete", json={"memory_id": eid, "user_id": "frank"}, headers=H("frank")).json()
after = sal.execute("SELECT count(*) FROM salience WHERE memory_id=?", (eid,)).fetchone()[0]
out["n4"] = [fx.get("status"), before, after]
out["n4_erin_still"] = "周会" in json.dumps(
    c.post("/search", json={"query": "周会 周三", "user_id": "erin"}, headers=H("erin")).json(), ensure_ascii=False)

# 6) per-agent keys (S3): main key keeps working; agent key bound to its identities
from ducky import agent_keys  # noqa: E402
os.environ["AIDUMEM_API_TOKEN"] = "main-token-for-probe-0001"
tok = agent_keys.new_token()
agent_keys.add_key("helper", ["helper", "helper-group"], tok)
B = lambda t, uid: {"Authorization": f"Bearer {t}", "X-Caller-User-Id": uid}  # noqa: E731
out["keys_file_mode"] = oct(os.stat(agent_keys.keys_path()).st_mode & 0o777)
out["keys_file_has_plain"] = tok in open(agent_keys.keys_path()).read()
out["no_token"] = c.post("/search", json={"query": "x", "user_id": "helper"}).status_code
out["wrong_token"] = c.post("/search", json={"query": "x", "user_id": "helper", "caller_user_id": "helper"},
                            headers=B("amk_wrong", "helper")).status_code
out["agent_own"] = add("helper", "helper 的任务列表放在看板第二列", headers=B(tok, "helper")).status_code
out["agent_other_shadow"] = c.post("/search", json={"query": "周会", "user_id": "erin", "caller_user_id": "erin"},
                                   headers=B(tok, "erin")).status_code
os.environ["AIDUMEI_AGENT_KEY_ENFORCE"] = "1"
out["agent_other_enforced"] = c.post("/search", json={"query": "周会", "user_id": "erin",
                                                      "caller_user_id": "erin"}, headers=B(tok, "erin")).status_code
out["agent_own_enforced"] = c.post("/search", json={"query": "看板", "user_id": "helper",
                                                    "caller_user_id": "helper"}, headers=B(tok, "helper")).status_code
os.environ["AIDUMEI_AGENT_KEY_ENFORCE"] = "0"
out["main_unbound_any"] = c.post("/search", json={"query": "周会", "user_id": "erin", "caller_user_id": "erin"},
                                 headers=B("main-token-for-probe-0001", "erin")).status_code
data = agent_keys.load()
agent_keys.save({**data, "main": {"agent": "owner", "callers": ["alice"]}})
out["main_bound_other_shadow"] = c.post("/search", json={"query": "周会", "user_id": "erin",
                                                         "caller_user_id": "erin"},
                                        headers=B("main-token-for-probe-0001", "erin")).status_code
out["key_events"] = wp_rows("SELECT key_agent, target_user, outcome, n FROM key_events ORDER BY key_agent, outcome")

# 7) f0.7.3: the key binding holds on every route, not only where a handler calls Pantheon
os.environ["AIDUMEI_AGENT_KEY_ENFORCE"] = "1"
G = lambda path, t, **q: c.get(path, params=q, headers=B(t, q.get("caller_user_id", ""))).status_code  # noqa: E731
P = lambda path, t, body: c.post(path, json=body, headers=B(t, body.get("caller_user_id", ""))).status_code  # noqa: E731
out["route_own"] = {
    "stats": G("/api/stats", tok, user_id="helper", caller_user_id="helper"),
    "recent": G("/api/recent", tok, user_id="helper", caller_user_id="helper"),
    "verdicts": G("/api/write-policy/verdicts", tok, user_id="helper", caller_user_id="helper"),
}
out["route_other"] = {
    "stats_other": G("/api/stats", tok, user_id="erin"),
    "stats_global": G("/api/stats", tok, user_id=""),
    "stats_default": c.get("/api/stats", headers={"Authorization": f"Bearer {tok}"}).status_code,
    "recent": G("/api/recent", tok, user_id="erin"),
    "tombstones": G("/api/tombstones", tok, user_id="erin"),
    "events": G("/api/events/history", tok, user_id="erin"),
    "facts": G("/api/facts", tok, user_id="erin"),
    "graph_checkup": G("/api/graph/checkup", tok, user_id="erin"),
    "verdicts": G("/api/write-policy/verdicts", tok, user_id="erin"),
    "forget_rules": G("/api/forget/rules", tok, user_id="erin"),
    "dossier": G("/api/dossier", tok, user_id="erin"),
    "governance_all": G("/api/governance/candidates", tok),
    "metrics": G("/metrics", tok),
    "usage": G("/api/usage", tok),
    "config": G("/api/config", tok),
    "delete": P("/delete", tok, {"memory_id": "00000000-0000-0000-0000-000000000000", "user_id": "erin"}),
    "forget": P("/api/forget", tok, {"user_id": "erin", "query": "周会"}),
    "search_no_caller": P("/search", tok, {"query": "周会", "user_id": "erin"}),
}
out["main_bound_route"] = [G("/api/stats", "main-token-for-probe-0001", user_id="erin"),
                           G("/api/stats", "main-token-for-probe-0001", user_id="alice", caller_user_id="alice")]
os.environ["AIDUMEI_AGENT_KEY_ENFORCE"] = "0"
out["route_other_shadow"] = G("/api/stats", tok, user_id="")
out["route_events"] = wp_rows("SELECT target_user, action, outcome, n FROM key_events WHERE key_agent='helper'"
                              " AND action IN ('read', 'admin:GET /metrics', 'delete') AND target_user IN ('*','erin')"
                              " ORDER BY target_user, action, outcome")

# 8) f0.7.3: delete_all keeps "never store" rules (they are about the future)
from ducky import write_policy as _wp  # noqa: E402
_rid = _wp.add_rule("frank", "default", "保险柜密码位置")
_wp.purge_scope("frank", "default")
out["rules_after_delete_all"] = [r["id"] for r in _wp.list_rules("frank", "default")] == [_rid]
print(json.dumps(out, ensure_ascii=False))
