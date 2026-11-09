"""f0.8 ①：CC 记忆桥走真 /add /search /delete（本地档 + native 内核，零外呼）。

由 tests/test_f08_cc_memory_bridge.py 子进程调用（环境隔离同 _f07_p3_local_probe.py）。
"""
import io
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AIDUMEI_MEMORY_KERNEL"] = "native"
os.environ["AIDUMEI_ENGINE_MODE"] = "local"
from benchmarks.regression import Adapter  # noqa: E402
from scripts.cc_memory_sweep import B  # noqa: E402

c = Adapter().client


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def urlopen(req, timeout=None):
    path = "/" + req.full_url.split("://", 1)[1].split("/", 1)[1]
    resp = c.post(path, content=req.data, headers=dict(req.header_items()))
    if resp.status_code >= 400:
        raise urllib.error.HTTPError(req.full_url, resp.status_code, "", {}, io.BytesIO(resp.content))
    r = _Resp(resp.content)
    r.status = resp.status_code
    return r


urllib.request.urlopen = urlopen
home = Path(tempfile.mkdtemp())
os.environ.update(HOME=str(home), AIDUMEI_CC_BRIDGE_STATE=str(home / "st.json"), AIDUMEM_USER_ID="default",
                  AIDUMEM_LOG_DIR=str(home))
proj = home / ".claude" / "projects" / "-w-p"
mem = proj / "memory"
mem.mkdir(parents=True)
f = mem / "feedback_style.md"
secret = "sk-proj-" + "Q" * 20 + "w1x2y3z4"
f.write_text("---\nname: answer style\ndescription: how to answer\ntype: feedback\n---\n"
             f"Prefers terse answers about the zebra-otter project. db key {secret}\n", encoding="utf-8")
pl = {"session_id": "s9", "transcript_path": str(proj / "s9.jsonl"), "cwd": "/w/p",
      "tool_name": "Write", "tool_input": {"file_path": str(f)}}
cl = B.Client(B.identity())
r1 = B.run_hook(pl, client=cl)
redacted_on_disk = secret not in f.read_text() and "[redacted:" in f.read_text()


def hits(q):
    return c.post("/search", json={"query": q, "user_id": "default", "caller_user_id": "default", "limit": 20}).json().get(
        "results") or []


h1 = [h for h in hits("zebra-otter") if "zebra-otter" in json.dumps(h, ensure_ascii=False)]
md_ok = any(B._meta(h).get("source") == "cc_auto_memory" and B._meta(h).get("cc_file") == "feedback_style.md"
            for h in h1)
leak = any(secret in json.dumps(h, ensure_ascii=False) for h in hits("zebra-otter"))
r2 = B.run_hook({**pl, "tool_name": "Edit"}, client=cl)
f.write_text(f.read_text().replace("terse answers", "detailed answers"), encoding="utf-8")
r3 = B.run_hook({**pl, "tool_name": "Edit"}, client=cl)
after = [json.dumps(h, ensure_ascii=False) for h in hits("zebra-otter")]
print(json.dumps({"r1": r1.get("action"), "redacted": r1.get("redacted"), "on_disk": redacted_on_disk,
                  "metadata": md_ok, "leak": leak, "r2": r2.get("reason"), "r3": r3.get("action"),
                  "superseded": r3.get("superseded"),
                  "new_present": any("detailed answers" in a for a in after),
                  "old_mem_rows": sum("terse answers" in a and "verbatim:" not in a for a in after),
                  "old_verbatim_rows": sum("terse answers" in a and "verbatim:" in a for a in after),
                  "n1": len(h1)}))
