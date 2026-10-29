"""f0.7 定向复核探针(外审 sesame / haiku 第二轮的复现脚本收编;由 tests/test_f07_reaudit.py 在独立子进程里跑)。

用法:python tests/_f07_reaudit_probe.py <native|mem0>  → stdout 最后一行 JSON。
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AIDUMEI_MEMORY_KERNEL"] = sys.argv[1]
from benchmarks.regression import Adapter  # noqa: E402

KERNEL = sys.argv[1]
ad = Adapter()
c, d = ad.client, ad.tmp
H = {"X-Caller-User-Id": "default"}
out = {}


def add(text, session):
    r = c.post("/add", json={"messages": [{"role": "user", "content": text}], "user_id": "default",
                             "infer": False, "metadata": {"session_id": session}}, headers=H)
    assert r.status_code == 200, r.text


def mid_of(text):
    hits = c.post("/search", json={"query": text, "user_id": "default", "limit": 10}, headers=H).json()["results"]
    return [x["id"] for x in hits if (x.get("memory") or "") == text][0]


def facts_conn():
    return sqlite3.connect(os.path.join(d, "facts.db"))


def verbatim_sessions(text):
    con = facts_conn()
    try:
        return sorted(r[0] for r in con.execute("SELECT session_id FROM verbatim_turns WHERE content=?", (text,)))
    finally:
        con.close()


def residue(needle, skip=("tombstones",)):
    found = {}
    for name in sorted(os.listdir(d)):
        if not name.endswith(".db"):
            continue
        con = sqlite3.connect(os.path.join(d, name))
        for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            if t in skip or t.endswith(("_data", "_idx", "_content", "_docsize", "_config")):
                continue
            try:
                for col in [x[1] for x in con.execute(f'PRAGMA table_info("{t}")')]:
                    n = con.execute(f'SELECT COUNT(*) FROM "{t}" WHERE CAST("{col}" AS TEXT) LIKE ?',
                                    (f"%{needle}%",)).fetchone()[0]
                    if n:
                        found[f"{name}:{t}.{col}"] = n
            except sqlite3.Error:
                pass
        con.close()
    return found


# A) haiku N1:同一句在两个会话说过(HTTP 去重成一条记忆、两行原文),删这条记忆 → 两行原文都删
A = "我的提示词是猫咪1234"
add(A, "sA")
add(A, "sB")
out["A_verbatim_before"] = verbatim_sessions(A)
out["A_delete"] = c.post("/delete", json={"memory_id": mid_of(A), "user_id": "default"}, headers=H).json().get("status")
out["A_verbatim_after"] = verbatim_sessions(A)
out["A_residue"] = residue("猫咪1234")

# B) haiku N3(native):两条独立记忆说着同一句、各在自己的会话 → 删一条只带走它那个会话的原文
if KERNEL == "native":
    from ducky.mem0_runtime import get_memory
    from ducky.verbatim_vault import store_verbatim
    nm = get_memory()
    B = "好的收到"
    for sid, mid in (("sX", "mem-x"), ("sY", "mem-y")):
        nm.insert(mid, B, user_id="default", metadata={"bank_id": "default", "session_id": sid},
                  vector=[], history=False)
        store_verbatim("default", [{"role": "user", "content": B}], metadata={"session_id": sid})
    out["B_verbatim_before"] = verbatim_sessions(B)
    out["B_delete"] = c.post("/delete", json={"memory_id": "mem-x", "user_id": "default"}, headers=H).json().get("status")
    out["B_verbatim_after"] = verbatim_sessions(B)
    out["B_other_alive"] = bool(nm.get("mem-y", user_id="default", bank_id="default"))

# C) haiku N2:欠账撤销不再子串匹配 —— 删「好的呀」不许连带别的记忆的待补任务
from ducky.dual_index import ensure_pending_schema  # noqa: E402
ensure_pending_schema()
C = "好的呀"
add(C, "sC")
con = facts_conn()
con.executemany("INSERT INTO pending_embeddings (side, ref_id, payload, user_id, bank_id) VALUES (?,?,?,?,?)", [
    ("local", "m-other", json.dumps({"text": "好的呀，收到了", "payload": {}}, ensure_ascii=False), "default", "default"),
    ("local", "m-third", json.dumps({"text": "好的呀", "payload": {}}, ensure_ascii=False), "default", "default"),
    ("cloud", "add", json.dumps({"messages": [{"role": "user", "content": "好的呀，明天见"}]}, ensure_ascii=False),
     "default", "default"),
])
con.commit()
con.close()
out["C_delete"] = c.post("/delete", json={"memory_id": mid_of(C), "user_id": "default"}, headers=H).json().get("status")
con = facts_conn()
out["C_pending_left"] = sorted(r[0] for r in con.execute("SELECT ref_id FROM pending_embeddings WHERE ref_id<>''"))
con.close()

# D) sesame F-03 变体:先改写再删,改写前原文不许留在欠账账本(原文本地点的待补载荷)
from ducky.dual_index import verbatim_local_pid  # noqa: E402
D_OLD, D_NEW = "我的暗号是紫色长颈鹿", "我的暗号是橙色斑马"
add(D_OLD, "sD")
con = facts_conn()
con.execute("INSERT INTO pending_embeddings (side, ref_id, payload, user_id, bank_id) VALUES ('local',?,?,?,?)",
            (verbatim_local_pid("default", "default", D_OLD),
             json.dumps({"text": D_OLD, "payload": {"data": D_OLD}}, ensure_ascii=False), "default", "default"))
con.commit()
con.close()
mid = mid_of(D_OLD)
out["D_update"] = c.post("/update", json={"memory_id": mid, "data": D_NEW, "user_id": "default"}, headers=H).status_code
out["D_delete"] = c.post("/delete", json={"memory_id": mid, "user_id": "default"}, headers=H).json().get("status")
out["D_residue_old"] = residue("紫色长颈鹿")
out["D_residue_new"] = residue("橙色斑马")

# E) sesame N-1:墓碑 TTL 到期 → 四列快照全擦,全库(含墓碑)不留原文
for name in ("facts.db", "memory.db"):
    p = os.path.join(d, name)
    if os.path.exists(p):
        con = sqlite3.connect(p)
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='tombstones'").fetchone():
            con.execute("UPDATE tombstones SET snapshot_expires_at=1")
            con.commit()
        con.close()
from ducky.tombstone import erase_expired_snapshots  # noqa: E402
out["E_erase"] = erase_expired_snapshots()
out["E_residue"] = {k: v for needle in ("猫咪1234", "紫色长颈鹿", "橙色斑马")
                    for k, v in residue(needle, skip=()).items()}

# F) sesame N-2:graph 族端点与 /api/facts 同口径 —— 没配绑定时 bob 也不许读 alice
c.post("/add", json={"messages": [{"role": "user", "content": "alice 的体检报告在协和"}], "user_id": "alice",
                     "infer": False}, headers={"X-Caller-User-Id": "alice"})
hb = {"X-Caller-User-Id": "bob"}
q = {"user_id": "alice", "caller_user_id": "bob"}
out["F_status"] = {
    "nebula": c.get("/api/graph/nebula", params=q, headers=hb).status_code,
    "checkup": c.get("/api/graph/checkup", params=q, headers=hb).status_code,
    "stats": c.get("/api/graph/stats", params=q, headers=hb).status_code,
    "neighbors": c.get("/api/graph/neighbors", params={**q, "node_id": "x"}, headers=hb).status_code,
    "facts": c.get("/api/facts", params=q, headers=hb).status_code,
}
out["F_owner_ok"] = c.get("/api/graph/checkup", params={"user_id": "alice", "caller_user_id": "alice"},
                          headers={"X-Caller-User-Id": "alice"}).status_code
# H) P5 自查(native):删除连带清掉的原文副本,恢复时一并放回;同文独立记忆在时只带走本会话副本
if KERNEL == "native":
    import uuid
    Hid = str(uuid.uuid4())
    Hx = "我的复核口令是蓝色河马"
    nm.insert(Hid, Hx, user_id="default", metadata={"bank_id": "default", "session_id": "sH"}, vector=[], history=False)
    nm.insert("verbatim:9001", Hx, user_id="default", metadata={"bank_id": "default", "memory_type": "VERBATIM",
              "session_id": "sH"}, vector=[], history=False)
    j = c.post("/delete", json={"memory_id": Hid, "user_id": "default"}, headers=H).json()
    out["H_copy_after_delete"] = bool(nm.get("verbatim:9001", user_id="default", bank_id="default"))
    r = c.post("/api/tombstone/restore", json={"tombstone_id": j["details"]["tombstone_id"], "user_id": "default"},
               headers=H).json()
    out["H_restore"] = r.get("status")
    out["H_main_back"] = bool(nm.get(Hid, user_id="default", bank_id="default"))
    out["H_copy_back"] = bool(nm.get("verbatim:9001", user_id="default", bank_id="default"))
    Bk = "好的收到"     # B 里 mem-y(会话 sY)还活着;给 sY / sZ 各放一份原文副本,再加一条 sZ 的独立记忆后删它
    for vid, sid in (("verbatim:9101", "sY"), ("verbatim:9102", "sZ")):
        nm.insert(vid, Bk, user_id="default", metadata={"bank_id": "default", "memory_type": "VERBATIM",
                  "session_id": sid}, vector=[], history=False)
    nm.insert("mem-z", Bk, user_id="default", metadata={"bank_id": "default", "session_id": "sZ"}, vector=[], history=False)
    c.post("/delete", json={"memory_id": "mem-z", "user_id": "default"}, headers=H)
    out["H_copies_left"] = sorted(v for v in ("verbatim:9101", "verbatim:9102")
                                  if nm.get(v, user_id="default", bank_id="default"))

# G) haiku N7:事实单一真源用行为验证(此前是源码字符串断言)—— native 生产档 memory.db 不派生第二份事实
add("我的测试城市是苏州", "sG")
out["G_facts_api"] = "苏州" in c.get("/api/facts", params={"user_id": "default"}, headers=H).text
if KERNEL == "native":
    con = sqlite3.connect(os.path.join(d, "memory.db"))
    has = con.execute("SELECT 1 FROM sqlite_master WHERE name='facts'").fetchone()
    out["G_memdb_facts"] = con.execute("SELECT COUNT(*) FROM facts").fetchone()[0] if has else 0
    con.close()
print(json.dumps(out, ensure_ascii=False))
