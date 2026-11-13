"""f0.8 ② HTTP probe for the write paths besides /add: POST /update and the background
auto_memory loop (run by tests/test_f08_gate_paths.py in a subprocess: api_server reads env at import).

Synthetic text only.  usage: python tests/_f08_gate_paths_probe.py  → one JSON line on stdout.
"""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AIDUMEI_MEMORY_KERNEL"] = "native"
_HOST_DIR = tempfile.mkdtemp(prefix="aidumei_host_")         # host state.db lives outside the data dir
HOST_DB = os.path.join(_HOST_DIR, "state.db")
os.environ["AIDUMEM_HOST_STATE_DB"] = HOST_DB
from benchmarks.regression import Adapter  # noqa: E402

ad = Adapter()
c, d = ad.client, ad.tmp
out = {}

import ducky.gear  # noqa: E402
import ducky.tombstone  # noqa: E402
from ducky.extended import auto_memory as am  # noqa: E402
from ducky.utils import DEFAULT_USER_ID as DU  # noqa: E402

ducky.gear.should_try_llm = lambda **_kw: False                # offline harness: deterministic direct write
am.AUTO_MEMORY_STATE = os.path.join(_HOST_DIR, "auto_state.json")
_real_tomb = ducky.tombstone.tombstone_gated_write


def _broken_tomb(*_a, **_kw):
    raise sqlite3.OperationalError("synthetic: tombstone table unavailable")


def H(uid):
    return {"X-Caller-User-Id": uid}


def q(db, sql, args=()):
    con = sqlite3.connect(os.path.join(d, db))
    try:
        return [list(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def live(uid, needle):
    return q("memory.db", "SELECT count(*) FROM memories WHERE user_id=? AND content LIKE ?",
             (uid, f"%{needle}%"))[0][0]


def tombs(uid, reason_like="write_policy:%"):
    return q("facts.db", "SELECT reason FROM tombstones WHERE user_id=? AND reason LIKE ? ORDER BY tombstone_id",
             (uid, reason_like))


def residue(needle, skip=("tombstones",)):
    """Every copy of needle outside the tombstone table (f0.8+ R3-04: P8 waits there, restorable)."""
    hits = {}
    for root, _dirs, files in os.walk(d):
        for name in sorted(files):
            if name.endswith(("-wal", "-shm", "-journal")):
                continue
            path = os.path.join(root, name)
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
                                hits[f"{os.path.relpath(path, d)}:{t}.{col}"] = n
                except sqlite3.Error:
                    pass
                finally:
                    con.close()
            elif needle.encode() in data:
                hits[os.path.relpath(path, d)] = data.count(needle.encode())
    return hits


def add(uid, text):
    c.post("/add", json={"messages": [{"role": "user", "content": text}], "user_id": uid,
                         "caller_user_id": uid, "infer": False}, headers=H(uid))
    return q("memory.db", "SELECT id FROM memories WHERE user_id=? AND content=?", (uid, text))[0][0]


def update(uid, mid, text):
    r = c.post("/update", json={"memory_id": mid, "user_id": uid, "caller_user_id": uid, "content": text},
               headers=H(uid))
    return r.status_code, r.json()


def forget_rule(uid, phrase):
    return c.post("/api/forget", json={"user_id": uid, "memory_ids": ["00000000-0000-0000-0000-000000000000"],
                                       "never_store": phrase}, headers=H(uid)).json().get("rule_id")


def gsum(body):
    return [g["principle"] + ":" + g["reason"] for g in body.get("gated", [])]


# ══ /update ═══════════════════════════════════════════════════════════
mid = add("ivy", "我每周六早上去河边跑步五公里")
st, body = update("ivy", mid, "[SILENT]")
out["upd_noise"] = [st, body.get("action"), gsum(body), body.get("updated"),
                    live("ivy", "河边跑步"), live("ivy", "[SILENT]"), tombs("ivy")]
st, body = update("ivy", mid, "[Image: original 800x600, displayed at 800x600.]")
out["upd_image"] = [st, body.get("action"), gsum(body), live("ivy", "800x600")]
st, body = update("ivy", mid, "我每周日早上去河边跑步十公里")
out["upd_normal"] = [st, body.get("action"), "gated" in body, live("ivy", "跑步十公里"), live("ivy", "跑步五公里")]
st, body = update("ivy", "00000000-0000-0000-0000-00000000dead", "[SILENT]")
out["upd_missing_id"] = [st, len(tombs("ivy"))]           # ownership first: 404, no new tombstone

rid = forget_rule("ivy", "青石巷 九号")
st, body = update("ivy", mid, "我的旧住址是青石巷九号二单元")
out["upd_p8"] = [rid is not None, st, body.get("action"), [g["principle"] for g in body.get("gated", [])],
                 residue("九号二单元"), live("ivy", "跑步十公里"), tombs("ivy", "write_policy:P8:%")]

os.environ["AIDUMEI_WRITE_POLICY_P1"] = "shadow"
st, body = update("ivy", mid, "[SILENT]")
out["upd_p1_shadow"] = [st, "gated" in body, live("ivy", "[SILENT]")]
os.environ.pop("AIDUMEI_WRITE_POLICY_P1")
mid2 = add("jo", "我习惯晚上十点前睡觉")
os.environ["AIDUMEI_WRITE_POLICY_P8"] = "shadow"
forget_rule("jo", "青石巷")
st, body = update("jo", mid2, "我的旧住址是青石巷九号二单元")
out["upd_p8_shadow"] = [st, "gated" in body, live("jo", "青石巷")]
os.environ.pop("AIDUMEI_WRITE_POLICY_P8")
mid3 = add("jo", "我习惯早上喝一杯温水")
os.environ["AIDUMEI_WRITE_POLICY"] = "off"
st, body = update("jo", mid3, "[SILENT]")
out["upd_off"] = [st, "gated" in body, live("jo", "[SILENT]")]
os.environ.pop("AIDUMEI_WRITE_POLICY")

mid5 = add("kit", "我常去楼下那家面馆吃午饭")
st, body = update("kit", mid5, "好的")
out["upd_short"] = [st, "gated" in body, q("memory.db", "SELECT count(*) FROM memories WHERE id=? AND content=?",
                                             (mid5, "好的"))[0][0]]
mid4 = add("kit", "我喜欢在阳台种薄荷")
ducky.tombstone.tombstone_gated_write = _broken_tomb
st, body = update("kit", mid4, "[SILENT]")
ducky.tombstone.tombstone_gated_write = _real_tomb
out["upd_tomb_fail"] = [st, "gated" in body, live("kit", "[SILENT]"), len(tombs("kit"))]

# ══ background auto_memory loop ═══════════════════════════════════════
with sqlite3.connect(HOST_DB) as hc:
    hc.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, role TEXT, content TEXT,"
               " session_id TEXT, created_at TEXT)")


def host_session(sid, rows):
    with sqlite3.connect(HOST_DB) as hc:
        hc.executemany("INSERT INTO messages(role, content, session_id) VALUES (?,?,?)",
                       [(r, t, sid) for r, t in rows])


def summaries(needle):
    return q("memory.db", "SELECT count(*) FROM memories WHERE user_id=? AND content LIKE ? AND content LIKE ?",
             (DU, "%总结以下对话%", f"%{needle}%"))[0][0]


host_session("s1", [("user", "我答应周末陪家人去湖边，说好了不再加班，这件事请帮我记住"),
                    ("assistant", "[SILENT]"),
                    ("user", "[Image: original 1357x913, displayed at 1357x913.]"),
                    ("assistant", "好的，记下了：周末陪家人去湖边，不再加班。"),
                    ("user", "好")])
r = am._run_auto_memory()
out["am_noise"] = [r.get("gated"), r.get("extracted"), summaries("陪家人去湖边"), summaries("[SILENT]"),
                   summaries("1357x913"), tombs(DU)]

host_session("s2", [("assistant", "[SILENT]"),
                    ("user", "This session is being continued from a previous conversation that ran out of context.")])
r = am._run_auto_memory()
out["am_all_noise"] = [r.get("gated"), r.get("extracted"), summaries("being continued")]

forget_rule(DU, "梧桐里 七号")
host_session("s3", [("user", "我以前住在梧桐里，那边的早点铺子很好吃，想起来就想回去看看"),
                    ("assistant", "记下了，你提过的是七号三单元那一栋老楼，楼下就是早点铺子")])
r = am._run_auto_memory()
out["am_p8_cross"] = [r.get("gated"), r.get("extracted"), residue("七号三单元"), residue("早点铺子"),
                      len(tombs(DU, "write_policy:P8:%"))]

os.environ["AIDUMEI_WRITE_POLICY_P1"] = "shadow"
host_session("s4", [("user", "我每天下班后会去公园散步半小时，这个习惯已经坚持了整整两年，风雨无阻"),
                    ("assistant", "[SILENT]")])
r = am._run_auto_memory()
os.environ.pop("AIDUMEI_WRITE_POLICY_P1")
out["am_p1_shadow"] = [r.get("gated"), r.get("extracted"), summaries("公园散步") and summaries("[SILENT]")]

ducky.tombstone.tombstone_gated_write = _broken_tomb
host_session("s5", [("user", "我打算明年春天去学游泳，先报一个周末的初级班试试看"),
                    ("assistant", "[Image: original 640x480, displayed at 640x480.]")])
r = am._run_auto_memory()
ducky.tombstone.tombstone_gated_write = _real_tomb
out["am_tomb_fail"] = [r.get("gated"), r.get("extracted"), summaries("640x480")]
print(json.dumps(out, ensure_ascii=False))
