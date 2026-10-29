"""f0.7 P5 自审探针(2026-10-29,对 66c8642 的对抗自审 S-1..S-7)。独立子进程里起进程内 app。
用法: python tests/_f07_selfaudit_probe.py native|mem0 → 末行 JSON。"""
import json
import os
import sqlite3
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)
KERNEL = sys.argv[1]
os.environ["AIDUMEI_MEMORY_KERNEL"] = KERNEL
from benchmarks.regression import Adapter  # noqa: E402

ad = Adapter()
c, d = ad.client, ad.tmp
H = {"X-Caller-User-Id": "default"}
out = {"kernel": KERNEL}

from ducky.verbatim_vault import store_verbatim  # noqa: E402
import ducky.wal_engine as we  # noqa: E402


def add(text, session):
    body = {"messages": [{"role": "user", "content": text}], "user_id": "default", "infer": False,
            "metadata": {"session_id": session}}
    r = c.post("/add", json=body, headers=H)
    assert r.status_code == 200, r.text


def mids(text):
    hits = c.post("/search", json={"query": text, "user_id": "default", "limit": 20}, headers=H).json().get("results", [])
    return [x["id"] for x in hits if (x.get("memory") or "") == text and not str(x["id"]).startswith("verbatim:")]


def delete(mid):
    return c.post("/delete", json={"memory_id": mid, "user_id": "default"}, headers=H).json()


def fq(sql, args=()):
    con = sqlite3.connect(os.path.join(d, "facts.db"))
    try:
        return [tuple(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def vsess(text):
    return sorted(r[0] for r in fq("SELECT session_id FROM verbatim_turns WHERE content=?", (text,)))


def last_tomb():
    r = fq("SELECT tombstone_id, layer_snapshot FROM tombstones ORDER BY tombstone_id DESC LIMIT 1")
    ls = json.loads(r[0][1] or "{}")
    return r[0][0], sorted(x.get("session_id") for x in ls.get("verbatim_rows", []))


def shared_pair(T, x_sess="sX", y_sess="sY"):
    """两条独立记忆说同一句 T(x 在 x_sess,y 在 y_sess),各有一行原文;返回 (x_id, y_id)。"""
    if KERNEL == "native":
        from ducky.mem0_runtime import get_memory
        nm = get_memory()
        ids = []
        for sid, mid in ((x_sess, "x-" + T), (y_sess, "y-" + T)):
            nm.insert(mid, T, user_id="default", metadata={"bank_id": "default", "session_id": sid}, vector=[],
                      history=False)
            store_verbatim("default", [{"role": "user", "content": T}], metadata={"session_id": sid})
            ids.append(mid)
        return ids
    O = T + "·别的"
    add(T, x_sess)
    add(O, y_sess)
    x, y = mids(T)[0], mids(O)[0]
    c.post("/update", json={"memory_id": y, "data": T, "user_id": "default"}, headers=H)
    store_verbatim("default", [{"role": "user", "content": T}], metadata={"session_id": y_sess})
    return x, y


# S-1 重试:首轮原文层失败,WAL 重放正好删掉首轮认领的那批(被删记忆的会话 sX),sY 留着
T1 = "S1重试路径的机密"
x1, _ = shared_pair(T1)
orig = we._cascade_single_verbatim
we._cascade_single_verbatim = lambda *a, **k: a[5]("verbatim", RuntimeError("injected"))
out["S1_first"] = delete(x1).get("status")
we._cascade_single_verbatim = orig
out["S1_tomb"] = last_tomb()[1]
rep = we._reconcile_owned()
out["S1_reconcile"] = [rep.get("recovered"), rep.get("failed")]
out["S1_after"] = vsess(T1)

# S-2 去重合并(sA+sB 一条记忆 M1),另一条 M2 改写成同一句;删 M1 → sA、sB 原文都走
T2 = "S2我家wifi密码是qwe789"
add(T2, "sA")
add(T2, "sB")
add("S2无关记忆", "sC")
m1, m2 = mids(T2)[0], mids("S2无关记忆")[0]
c.post("/update", json={"memory_id": m2, "data": T2, "user_id": "default"}, headers=H)
out["S2_delete"] = delete(m1).get("status")
out["S2_after"] = vsess(T2)

# S-4 /update:改写 x,y(同一句、另一会话)的原文不许跟着删
T4 = "S4好的没问题"
x4, y4 = shared_pair(T4)
out["S4_update"] = c.post("/update", json={"memory_id": x4, "data": "S4改成别的内容", "user_id": "default"},
                          headers=H).status_code
out["S4_after"] = vsess(T4)

# S-5 云侧 add 欠账:只剔掉逐字相等的那条消息,同批其它消息保留
from ducky.dual_index import ensure_pending_schema  # noqa: E402
ensure_pending_schema()
T5 = "S5好的呀"
add(T5, "sP")
m5 = mids(T5)[0]
con = sqlite3.connect(os.path.join(d, "facts.db"))
con.execute("INSERT INTO pending_embeddings (side, ref_id, payload, user_id, bank_id) VALUES ('cloud','add',?,?,?)",
            (json.dumps({"messages": [{"role": "user", "content": "S5我的车牌是浙A12345"},
                                      {"role": "user", "content": T5}]}, ensure_ascii=False), "default", "default"))
con.commit()
con.close()
out["S5_delete"] = delete(m5).get("status")
out["S5_left"] = [[m.get("content") for m in json.loads(p)["messages"]]
                  for (p,) in fq("SELECT payload FROM pending_embeddings WHERE side='cloud'")]

# S-7 过期墓碑:恢复返回 expired,不盖 restored_at
T7 = "S7过期墓碑的句子"
add(T7, "sT")
delete(mids(T7)[0])
tid7 = last_tomb()[0]
for name in ("facts.db", "memory.db"):
    p = os.path.join(d, name)
    if os.path.exists(p):
        con = sqlite3.connect(p)
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='tombstones'").fetchone():
            con.execute("UPDATE tombstones SET snapshot_expires_at=1")
            con.commit()
        con.close()
from ducky.tombstone import erase_expired_snapshots  # noqa: E402
erase_expired_snapshots()
r = c.post("/api/tombstone/restore", json={"tombstone_id": tid7, "user_id": "default"}, headers=H).json()
out["S7_status"] = r.get("status")
out["S7_restored_at"] = fq("SELECT restored_at FROM tombstones WHERE tombstone_id=?", (tid7,))[0][0]

# S-8 检索问句不落库
c.post("/search", json={"query": "S8我的银行卡密码是多少", "user_id": "default"}, headers=H)
econ = sqlite3.connect(os.path.join(d, "evolve_mem.db"))
out["S8_nonempty_queries"] = econ.execute("SELECT COUNT(*) FROM evolve_queries WHERE query <> ''").fetchone()[0]
econ.close()
# P6b 拼接记忆(生产部署验收):一轮 = 用户句 + 助手句拼成一条记忆(messages_to_text 空格拼),原文逐条存;
# 删记忆两行原文都走,同会话别的活记忆也说着的那句(共用行)留着,等它也删了再走
def joined(u, a, session):
    text = u + " " + a
    if KERNEL == "native":
        from ducky.mem0_runtime import get_memory
        mid = "j-" + u
        get_memory().insert(mid, text, user_id="default", metadata={"bank_id": "default", "session_id": session},
                            vector=[], history=False)
    else:
        add(u, session)
        mid = mids(u)[0]
        c.post("/update", json={"memory_id": mid, "data": text, "user_id": "default"}, headers=H)
    store_verbatim("default", [{"role": "user", "content": u}, {"role": "assistant", "content": a}],
                   metadata={"session_id": session})
    return mid


def vrows(*texts):
    return sorted(r[0] for r in fq(f"SELECT content FROM verbatim_turns WHERE content IN ({','.join('?' * len(texts))})", texts))


U1, A1, U2 = "P6b我的自行车叫什么", "P6b白鹭号", "P6b再问一次自行车"
j1 = joined(U1, A1, "sJ")
j2 = joined(U2, A1, "sJ")
out["J_before"] = vrows(U1, A1, U2)
out["J_delete"] = delete(j1).get("status")
out["J_after"] = vrows(U1, A1, U2)
out["J2_delete"] = delete(j2).get("status")
out["J2_after"] = vrows(U1, A1, U2)
print(json.dumps(out, ensure_ascii=False))
