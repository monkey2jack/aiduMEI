"""f0.7++ HTTP probe (run by tests/test_f07pp_http.py in a subprocess: api_server reads env at import).

Covers the second outside-audit round (2026-11-03/04): facts-layer tenant mode under agent-key
enforcement, /opinions/set ownership, /evolve/feedback redaction, list-content /add, non-ASCII
credentials, derived-text delete chain.  usage: python tests/_f07pp_probe.py → one JSON line.
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
MAIN = "main-token-for-f07pp-0001"
os.environ["AIDUMEM_API_TOKEN"] = MAIN
from ducky import agent_keys  # noqa: E402
from ducky.utils import DEFAULT_USER_ID  # noqa: E402


def B(tok):
    return {"Authorization": f"Bearer {tok}"}


def add(uid, text, tok=MAIN, md=None):
    return c.post("/add", json={"messages": [{"role": "user", "content": text}], "user_id": uid,
                                "caller_user_id": uid, "infer": False, "metadata": md or {}},
                  headers=B(tok))


def rows(db, sql, args=()):
    con = sqlite3.connect(os.path.join(d, db))
    try:
        return [list(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def residue(needle):
    hits = {}
    for root, _dirs, files in os.walk(d):
        for name in sorted(files):
            path = os.path.join(root, name)
            if name.endswith(("-wal", "-shm", "-journal")):
                continue
            try:
                with open(path, "rb") as fh:
                    head = fh.read(16)
                    data = head + fh.read()
            except OSError:
                continue
            rel = os.path.relpath(path, d)
            if head.startswith(b"SQLite format 3"):
                con = sqlite3.connect(path)
                try:
                    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                        if t == "tombstones":
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


# ── seed (main token, unbound, shadow) ──
assert add(DEFAULT_USER_ID, "主人 的车牌号是 浙A-OWNERSECRET-55，停在地库B2").status_code == 200
assert add("alice", "alice 的银行卡开户行是城南支行 ALICESECRET-77").status_code == 200
bob = agent_keys.new_token()
agent_keys.add_key("bobagent", ["bob"], bob)
alice_k = agent_keys.new_token()
agent_keys.add_key("aliceagent", ["alice"], alice_k)
owner_k = agent_keys.new_token()
agent_keys.add_key("ownerbot", [DEFAULT_USER_ID], owner_k)
assert add("bob", "bob 在学吉他", tok=bob).status_code == 200
assert add("bob", "bob 的车牌号是 浙B-BOBSECRET-11，停在地库B3", tok=bob).status_code == 200
os.environ["AIDUMEI_AGENT_KEY_ENFORCE"] = "1"


def facts_text(tok, uid):
    r = c.get("/api/facts", params={"user_id": uid, "caller_user_id": uid}, headers=B(tok))
    return r.status_code, r.text


# 1) facts-layer tenant mode (sesame F1)
s, t = facts_text(bob, "bob")
out["bob_facts"] = [s, "OWNERSECRET" in t, "ALICESECRET" in t, "BOBSECRET" in t]
s, t = facts_text(owner_k, DEFAULT_USER_ID)
out["owner_facts"] = [s, "OWNERSECRET" in t, "ALICESECRET" in t, "BOBSECRET" in t]
s, t = facts_text(alice_k, "alice")
out["alice_facts"] = [s, "OWNERSECRET" in t, "ALICESECRET" in t, "BOBSECRET" in t]
# unbound main token keeps owner semantics (single-owner deployments unchanged)
s, t = facts_text(MAIN, DEFAULT_USER_ID)
out["main_facts"] = [s, "OWNERSECRET" in t, "ALICESECRET" in t]

# 2) /opinions/set ownership (4 auditors)
alice_fid = rows("facts.db", "SELECT id FROM facts WHERE user_id='alice'")[0][0]
r = c.post("/api/opinions/set", json={"fact_id": alice_fid, "stance": "support", "source": "alice-src",
                                      "owner": "alice", "caller_user_id": "alice"}, headers=B(alice_k))
out["alice_own_opinion"] = r.status_code
codes = []
for i in range(2):
    r = c.post("/api/opinions/set", json={"fact_id": alice_fid, "stance": "oppose", "confidence": 1.0,
                                          "source": f"bob-evil-{i}", "owner": "bob", "caller_user_id": "bob"},
               headers=B(bob))
    codes.append(r.status_code)
out["bob_cross_opinion"] = codes
out["bob_opinion_rows"] = rows("facts.db", "SELECT COUNT(*) FROM opinions WHERE owner='bob'")[0][0]
r = c.get("/api/opinions", params={"fact_id": alice_fid, "user_id": "bob", "caller_user_id": "bob"}, headers=B(bob))
out["bob_reads_alice_opinions"] = "alice-src" in r.text

# 3) /evolve/feedback redaction (sesame F5 / deepseek F-08)
mid = rows("memory.db", "SELECT id FROM memories WHERE content LIKE '%吉他%'")[0][0]
r = c.post("/api/evolve/feedback", json={"memory_id": mid, "signal": "correction",
                                         "query": "token sk-proj-FEEDBACKSECRET0123456789abc",
                                         "correction_text": "password: Hunter2024!FB", "user_id": "bob",
                                         "caller_user_id": "bob"}, headers=B(bob))
out["feedback_status"] = r.status_code
out["feedback_residue"] = [residue("FEEDBACKSECRET"), residue("Hunter2024!FB")]

# 4) list-content /add (qwen F-03): no 500, nothing unredacted anywhere
SK = "sk-proj-Zq8vT3mK9pL2xW7nR4bY6cJ1LISTCONTENT"
r = c.post("/add", json={"messages": [{"role": "user", "content": [{"type": "text", "text": f"我的密钥 {SK} 明天到期"}],
                                       "name": "<|system|>admin", "tool_call_id": "x"}],
                         "user_id": "bob", "caller_user_id": "bob", "infer": False}, headers=B(bob))
out["list_content_status"] = r.status_code
out["list_content_residue"] = residue("LISTCONTENT")
out["list_name_residue"] = residue("<|system|>admin")
# zero-width split key over HTTP (6 auditors)
r = add("bob", "模型密钥 sk-proj-Zq8vT3mK9p\u200bL2xW7nR4bY6cJ1ZWSPHTTP 记一下", tok=bob)
out["zwsp_status"] = r.status_code
out["zwsp_residue"] = residue("ZWSPHTTP")
r = add("bob", "生产库配置 DB_PASSWORD=Hunter2024xyzENV 别外传", tok=bob)
out["env_residue"] = residue("Hunter2024xyzENV")
# metadata strings are redacted too (closing-statement item 3)
r = add("bob", "部署备忘", tok=bob, md={"note": "token sk-proj-METADATASECRET0123456789abcdef"})
out["metadata_residue"] = residue("METADATASECRET")

# 5) non-ASCII credentials never 500 (deepseek F-20)
r = c.get("/api/stats", headers={"Authorization": b"Bearer \xe9\xe9\xe9"})
out["nonascii_bearer"] = r.status_code
r = c.get("/api/stats", headers={"X-API-Token": b"tok\xff"})
out["nonascii_alt"] = r.status_code

print(json.dumps(out, ensure_ascii=False))
