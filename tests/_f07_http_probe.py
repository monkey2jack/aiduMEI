"""f0.7 P0 HTTP 端到端探针(由 tests/test_f07_p0_http.py 在独立子进程里跑:api_server 导入期读环境变量)。

用法:python tests/_f07_http_probe.py <native|mem0>  → stdout 一行 JSON。
"""
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AIDUMEI_MEMORY_KERNEL"] = sys.argv[1]
from benchmarks.regression import Adapter  # noqa: E402

ad = Adapter()
c, d = ad.client, ad.tmp
H = {"X-Caller-User-Id": "default"}
out = {}


def search_hit(q, needle, uid="default"):
    return needle in c.post("/search", json={"query": q, "user_id": uid}, headers={"X-Caller-User-Id": uid}).text


# 1) 删除即擦除(sesame F-03/F-04):紫色长颈鹿
c.post("/add", json={"messages": [{"role": "user", "content": "我的银行卡密码提示是紫色长颈鹿"}],
                     "user_id": "default", "infer": False}, headers=H)
hits = c.post("/search", json={"query": "紫色长颈鹿", "user_id": "default"}, headers=H).json()["results"]
mid = [x["id"] for x in hits if "紫色长颈鹿" in json.dumps(x, ensure_ascii=False)][0]
out["facts_before"] = "紫色长颈鹿" in c.get("/api/facts", params={"user_id": "default"}, headers=H).text
j = c.post("/delete", json={"memory_id": mid, "user_id": "default"}, headers=H).json()
out["delete_status"] = j.get("status")
out["search_after"] = search_hit("紫色长颈鹿", "紫色长颈鹿") or search_hit("银行卡密码提示", "紫色长颈鹿")
out["facts_after"] = "紫色长颈鹿" in c.get("/api/facts", params={"user_id": "default"}, headers=H).text
residue = {}
for name in sorted(os.listdir(d)):
    if not name.endswith(".db"):
        continue
    con = sqlite3.connect(os.path.join(d, name))
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        if t == "tombstones" or t.endswith(("_data", "_idx", "_content", "_docsize", "_config")):
            continue      # 墓碑=可恢复副本(受 TTL,到期另测);evolve_queries 复核后只存摘要,不再豁免;FTS 影子表随主表
        try:
            for col in [x[1] for x in con.execute(f'PRAGMA table_info("{t}")')]:
                n = con.execute(f'SELECT COUNT(*) FROM "{t}" WHERE CAST("{col}" AS TEXT) LIKE ?',
                                ("%紫色长颈鹿%",)).fetchone()[0]
                if n:
                    residue[f"{name}:{t}.{col}"] = n
        except sqlite3.Error:
            pass
    con.close()
out["residue"] = residue

# 2) 改写后派生事实跟着改(glm #1 / sesame F-04)
c.post("/add", json={"messages": [{"role": "user", "content": "我的测试城市是上海"}], "user_id": "default",
                     "infer": False}, headers=H)
hits = c.post("/search", json={"query": "测试城市", "user_id": "default"}, headers=H).json()["results"]
uid_ = [x["id"] for x in hits if "上海" in json.dumps(x, ensure_ascii=False)][0]
up = c.post("/update", json={"memory_id": uid_, "data": "我的测试城市是北京", "user_id": "default"}, headers=H)
facts = c.get("/api/facts", params={"user_id": "default"}, headers=H).text
out["update_status"] = up.status_code
out["facts_new_value"] = "北京" in facts
out["facts_old_value"] = "上海" in facts

# 3) 两租户同 id(sesame F-01,仅 native:迁移后的形态)
if sys.argv[1] == "native":
    from ducky.mem0_runtime import get_memory
    nm = get_memory()
    nm.insert("core::persona", "alice 的人设:医生,住在杭州", user_id="alice", metadata={"bank_id": "default"},
              vector=[], history=False)
    nm.insert("core::persona", "bob 的人设:律师,住在上海", user_id="bob", metadata={"bank_id": "default"},
              vector=[], history=False)
    hb = {"X-Caller-User-Id": "bob"}
    out["bob_update"] = c.post("/update", json={"memory_id": "core::persona", "data": "bob 的人设:律师,已搬到北京",
                                               "user_id": "bob"}, headers=hb).status_code
    out["bob_delete"] = c.post("/delete", json={"memory_id": "core::persona", "user_id": "bob"},
                               headers=hb).json().get("status")
    rows = sqlite3.connect(os.path.join(d, "memory.db")).execute(
        "SELECT user_id, content FROM memories WHERE id='core::persona' ORDER BY user_id").fetchall()
    out["persona_rows"] = [list(r) for r in rows]
print(json.dumps(out, ensure_ascii=False))
