"""f0.7.6 structural / delete-chain / opinions probe (run by tests/test_f076_struct.py in a subprocess:
api_server reads env at import).  Each block replays one PoC of the third outside-audit round
(R2-08..R2-10, R2-12..R2-16) against the offline Adapter.  usage: python tests/_f076_struct_probe.py
→ one JSON line on stdout.
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _k in list(os.environ):
    if _k.startswith(("AIDUMEM_", "AIDUMEI_")):
        os.environ.pop(_k)
os.environ["AIDUMEI_MEMORY_KERNEL"] = "native"
from benchmarks.regression import Adapter  # noqa: E402

ad = Adapter()
c, d = ad.client, ad.tmp
out = {}
MAIN = "main-token-for-f076-struct-0001"
os.environ["AIDUMEM_API_TOKEN"] = MAIN
from ducky import agent_keys  # noqa: E402
from ducky.utils import DEFAULT_USER_ID  # noqa: E402


def B(tok):
    return {"Authorization": f"Bearer {tok}"}


def add(uid, text, tok=MAIN, md=None, **extra):
    body = {"messages": [{"role": "user", "content": text}], "user_id": uid,
            "caller_user_id": uid, "infer": False, "metadata": md or {}}
    body.update(extra)
    return c.post("/add", json=body, headers=B(tok))


def rows(db, sql, args=()):
    con = sqlite3.connect(os.path.join(d, db))
    try:
        return [list(r) for r in con.execute(sql, args)]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def residue(needle, *, wal=True, skip_tables=("tombstones",)):
    """Every column of every SQLite table under DATA_DIR, plus raw bytes of every other file
    (``-wal`` included when wal=True)."""
    hits = {}
    for root, _dirs, files in os.walk(d):
        for name in sorted(files):
            path = os.path.join(root, name)
            side = name.endswith(("-wal", "-shm", "-journal"))
            if side and not wal:
                continue
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            rel = os.path.relpath(path, d)
            if data.startswith(b"SQLite format 3") and not side:
                con = sqlite3.connect(path)
                try:
                    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                        if t in skip_tables:
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
                hits[rel + (" (raw)" if side else "")] = data.count(needle.encode())
    return hits


def wal_residue(needle):
    hits = {}
    for root, _dirs, files in os.walk(d):
        for name in files:
            if name.endswith("-wal"):
                with open(os.path.join(root, name), "rb") as fh:
                    n = fh.read().count(needle.encode())
                if n:
                    hits[os.path.relpath(os.path.join(root, name), d)] = n
    return hits


bob = agent_keys.new_token()
agent_keys.add_key("bobagent", ["bob"], bob)
alice = agent_keys.new_token()
agent_keys.add_key("aliceagent", ["alice"], alice)

# ── R2-08: role carries a secret ──
r = c.post("/add", json={"messages": [{"role": "sk-proj-Mk13RoleAbcdefghijklmnop0123", "content": "今天去了湖边散步"}],
                         "user_id": DEFAULT_USER_ID, "caller_user_id": DEFAULT_USER_ID, "infer": False},
           headers=B(MAIN))
out["r208_status"] = r.status_code
out["r208_residue"] = residue("Mk13Role")
out["r208_roles"] = sorted({x[0] for x in rows("facts.db", "SELECT role FROM verbatim_turns")})

# ── R2-09: JSON-string messages with a \u-escaped key ──
msgs = json.dumps([{"role": "user", "content": "管理台 password: Mk15Escape#Pw 记一下"}]).replace(
    "password", "\\u0070assword")
r = c.post("/add", json={"messages": msgs, "user_id": DEFAULT_USER_ID, "caller_user_id": DEFAULT_USER_ID,
                         "infer": False}, headers=B(MAIN))
out["r209_status"] = r.status_code
out["r209_residue"] = residue("Mk15Escape")
out["r209_journal"] = any("Mk15Escape" in json.dumps(json.loads(x[0] or "null"), ensure_ascii=False)
                          for x in rows("mutation_journal.sqlite3", "SELECT input_json FROM mutations"))
# the plain-text sibling must still be stored (positive control: JSON strings are parsed, not dropped)
out["r209_content_kept"] = bool(rows("memory.db", "SELECT 1 FROM memories WHERE content LIKE '%管理台%'"))

# ── R2-10: top-level extra fields ──
for key, extra, mk in (("sess", {"session_id": "token=Mk17Sess#Tok"}, "Mk17Sess"),
                       ("event", {"event_at": "password=Mk19Event#Pw"}, "Mk19Event"),
                       ("agent", {"agent": "password=Mk18Agent#Pw"}, "Mk18Agent")):
    r = add(DEFAULT_USER_ID, "下午去买了牛奶和面包", **extra)
    s = c.post("/search", json={"query": "牛奶", "user_id": DEFAULT_USER_ID,
                                "caller_user_id": DEFAULT_USER_ID}, headers=B(MAIN))
    out[f"r210_{key}"] = [r.status_code, residue(mk), mk in s.text]
# positive control: a well-formed session id and ISO event_at still land
r = add(DEFAULT_USER_ID, "早上骑车去了图书馆 F076OKMARK", session_id="sess-f076.ok:1",
        event_at="2024-05-01T08:00:00Z")
_md = rows("memory.db", "SELECT metadata FROM memories WHERE content LIKE '%F076OKMARK%'")
_md = json.loads(_md[0][0]) if _md and _md[0][0] else {}
out["r210_ok_control"] = [r.status_code, _md.get("_origin_session_id"), _md.get("event_at")]

# ── R2-10 delete residue: single delete drops memory_epistemic; delete_all drops evolve_episodes ──
r = add("alice", "alice 周末去爬山 F076EPIMARK", tok=alice, session_id="alice-sess-f076a")
mid = rows("memory.db", "SELECT id FROM memories WHERE content LIKE '%F076EPIMARK%'")[0][0]
out["r210_epi_before"] = len(rows("facts.db", "SELECT 1 FROM memory_epistemic WHERE memory_ref=?", (mid,)))
r = c.post("/delete", json={"memory_id": mid, "user_id": "alice", "caller_user_id": "alice"}, headers=B(alice))
out["r210_single_delete"] = r.status_code
out["r210_epi_after"] = len(rows("facts.db", "SELECT 1 FROM memory_epistemic WHERE memory_ref=?", (mid,)))
out["r210_epi_sess_after"] = residue("alice-sess-f076a", wal=False)

add("alice", "alice 喜欢喝乌龙茶 F076ALLMARK", tok=alice, session_id="alice-sess-f076b")
out["r210_episodes_before"] = len(rows("evolve_mem.db", "SELECT 1 FROM evolve_episodes WHERE user_id='alice'"))
from ducky.wal_engine import cascade_delete_all  # noqa: E402
_all = cascade_delete_all("alice", confirm=True)
out["r210_delete_all"] = _all.get("status")
out["r210_episodes_after"] = len(rows("evolve_mem.db", "SELECT 1 FROM evolve_episodes WHERE user_id='alice'"))
out["r210_all_sess_after"] = residue("alice-sess-f076b", wal=False)

# ── R2-15: deleted text must not linger in -wal files ──
add("bob", "bob 记了一句 F076WALSINGLE 关于周末", tok=bob)
bmid = rows("memory.db", "SELECT id FROM memories WHERE content LIKE '%F076WALSINGLE%'")[0][0]
out["r215_wal_before"] = bool(wal_residue("F076WALSINGLE"))
r = c.post("/delete", json={"memory_id": bmid, "user_id": "bob", "caller_user_id": "bob"}, headers=B(bob))
out["r215_single"] = [r.status_code, wal_residue("F076WALSINGLE")]
add("bob", "bob 又记了一句 F076WALALL 关于周日", tok=bob)
cascade_delete_all("bob", confirm=True)
out["r215_all"] = wal_residue("F076WALALL")
from ducky.utils import get_facts_conn  # noqa: E402
out["r215_secure_delete"] = int(get_facts_conn().execute("PRAGMA secure_delete").fetchone()[0])

# ── R2-13 / R2-14 / R2-12 (agent keys: shadow first, then enforce) ──
add("bob", "bob 的车牌号是 浙B-XIDBOB-11，停在地库B3", tok=bob)
add("alice", "alice 的开户行是城南支行", tok=alice)
bob_fid = rows("facts.db", "SELECT id FROM facts WHERE user_id='bob' ORDER BY id DESC")[0][0]
alice_fid = rows("facts.db", "SELECT id FROM facts WHERE user_id='alice' ORDER BY id DESC")[0][0]
alice_mid = rows("memory.db", "SELECT id FROM memories WHERE user_id='alice' ORDER BY rowid DESC")[0][0]
bob_mid = rows("memory.db", "SELECT id FROM memories WHERE user_id='bob' ORDER BY rowid DESC")[0][0]

# R2-14 shadow-period forgery: bob's key writes source="alice" on alice's fact while enforcement is off
os.environ["AIDUMEI_AGENT_KEY_ENFORCE"] = "0"
r = c.post("/api/opinions/set", json={"fact_id": alice_fid, "stance": "oppose", "source": "alice",
                                      "owner": "bob", "caller_user_id": "bob"}, headers=B(bob))
out["r214_shadow_write"] = r.status_code
os.environ["AIDUMEI_AGENT_KEY_ENFORCE"] = "1"
r = c.get("/api/opinions", params={"fact_id": alice_fid, "user_id": "alice", "caller_user_id": "alice"},
          headers=B(alice))
out["r214_alice_list_owners"] = sorted({o.get("owner") for o in r.json().get("results", [])})
r = c.get("/api/opinions/aggregate", params={"fact_id": alice_fid, "user_id": "alice", "caller_user_id": "alice"},
          headers=B(alice))
out["r214_alice_aggregate_sources"] = (r.json().get("details") or {}).get("distinct_sources")
r = c.post("/api/opinions/set", json={"fact_id": alice_fid, "stance": "support", "source": "alice",
                                      "owner": "alice", "caller_user_id": "alice"}, headers=B(alice))
out["r214_overwrite_other_owner"] = r.status_code
out["r214_row_owner"] = rows("facts.db", "SELECT owner FROM opinions WHERE fact_id=? AND source='alice'",
                             (alice_fid,))
r = c.post("/api/opinions/set", json={"fact_id": alice_fid, "stance": "support", "source": "alice-2",
                                      "owner": "alice", "caller_user_id": "alice"}, headers=B(alice))
out["r214_own_write"] = r.status_code

# R2-13 evidence_ids
def opin(evidence, src):
    return c.post("/api/opinions/set", json={"fact_id": bob_fid, "stance": "support", "source": src,
                                             "evidence_ids": evidence, "owner": "bob", "caller_user_id": "bob"},
                  headers=B(bob)).status_code


out["r213_secret"] = opin(["api_key=OPEVI0Secret99"], "s1")
out["r213_secret_residue"] = residue("OPEVI0")
out["r213_cross_fact"] = opin([str(alice_fid)], "s2")
out["r213_cross_memory"] = opin([alice_mid], "s3")
out["r213_unknown_uuid"] = opin(["00000000-0000-4000-8000-000000000000"], "s4")
out["r213_own"] = opin([str(bob_fid), bob_mid], "s5")
out["r213_none"] = opin([], "s6")

# R2-12 nit: cross-tenant feedback on a memory bob cannot see
r = c.post("/api/evolve/feedback", json={"memory_id": alice_mid, "signal": "useful", "user_id": "bob",
                                         "caller_user_id": "bob"}, headers=B(bob))
out["r212_cross_feedback"] = r.status_code
r = c.post("/api/evolve/feedback", json={"memory_id": bob_mid, "signal": "useful", "user_id": "bob",
                                         "caller_user_id": "bob"}, headers=B(bob))
out["r212_own_feedback"] = r.status_code

# ── R2-16: /api/login with a non-object JSON body ──
out["r216"] = [c.post("/api/login", content=raw, headers={"content-type": "application/json"}).status_code
               for raw in ("[1,2]", '"str"', "null")]

print(json.dumps(out, ensure_ascii=False))
