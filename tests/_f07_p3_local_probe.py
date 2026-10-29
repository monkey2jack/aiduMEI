"""f0.7 P3:native 内核 + 本地档(零外呼)写入必须可召回(由 tests/test_f07_p3_connect_host.py 子进程调用)。"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if len(sys.argv) > 1 and sys.argv[1] == "default":
    os.environ.pop("AIDUMEI_MEMORY_KERNEL", None)       # 本地档未显式选内核 → 默认 native
else:
    os.environ["AIDUMEI_MEMORY_KERNEL"] = "native"
os.environ["AIDUMEI_ENGINE_MODE"] = "local"
from benchmarks.regression import Adapter  # noqa: E402

c = Adapter().client
H = {"X-Caller-User-Id": "default"}
r = c.post("/add", json={"messages": [{"role": "user", "content": "请记住，我的接入暗号是 AIDU-77F1A2。"}],
                         "user_id": "default", "async_mode": True,
                         "metadata": {"_origin_agent": "aidumem-cc-ingest"}}, headers=H).json()
hits = c.post("/search", json={"query": "AIDU-77F1A2", "user_id": "default"}, headers=H).json().get("results") or []
hit = any("AIDU-77F1A2" in json.dumps(h, ensure_ascii=False) for h in hits)
out = {"add": r.get("action"), "mode": r.get("engine_mode"), "hit": hit}
if len(sys.argv) > 2 and sys.argv[2] == "restore":
    # f0.7 WP3b：本地档删除 → 墓碑恢复，native 记忆本体必须回来（此前只回 FTS/原文）
    mid = next(h["id"] for h in hits if "AIDU-77F1A2" in json.dumps(h, ensure_ascii=False)
               and not str(h["id"]).startswith("verbatim:"))
    d = c.post("/delete", json={"memory_id": mid, "user_id": "default"}, headers=H).json()
    from ducky.tombstone import restore_tombstone
    from ducky.utils import get_facts_conn
    tid = get_facts_conn().execute("SELECT tombstone_id FROM tombstones WHERE target_id=? "
                                   "ORDER BY tombstone_id DESC", (mid,)).fetchone()[0]
    res = restore_tombstone(tid, user_id="default", bank_id="default")
    from ducky.memory_kernel import get_memory
    back = get_memory().get(mid, user_id="default", bank_id="default")
    out.update(deleted=d.get("status"), restored=res.get("status"), native_row=bool(back))
print(json.dumps(out))
