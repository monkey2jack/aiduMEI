#!/usr/bin/env python3
"""aiduMEI 资源对比基准：同一台机器、同一套桩嵌入、同一份负载，测「开箱」与「使用」两种状态。

用法：python scripts/slim_bench.py --code DIR --venv DIR --label f0.7 [--qdrant] [--adds 2000] [--searches 300]
输出一行 JSON。嵌入/LLM 用本地桩服务（确定性 1024 维向量），不调任何外部 API，
所以测到的是 aiduMEI 自身（及其向量库）的开销，不含模型推理。
"""
import argparse
import hashlib
import json
import os
import random
import re
import secrets
import signal
import subprocess
import threading
import time
import sys
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

D = 1024
# 被测实例由本脚本自己拉起；开着鉴权门禁跑，与生产形态一致（每次随机 token，不落盘）。
# 鉴权头复用仓内单一凭据入口 ducky.utils.api_auth_headers。
# token 只在 main() 里写进本进程环境，import 本文件不留副作用。
TOKEN = secrets.token_hex(16)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ducky.utils import api_auth_headers as _auth_headers  # noqa: E402

STUB_PORT, APP_PORT, QPORT = 8792, 8791, 6333


def vec(t):
    out, i = [], 0
    while len(out) < D:
        out += [b / 255 - 0.5 for b in hashlib.sha256(f"{i}:{t}".encode()).digest()]
        i += 1
    return out[:D]


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        j = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if "embeddings" in self.path:
            inp = j["input"]
            inp = [inp] if isinstance(inp, str) else inp
            r = {"object": "list", "model": "stub", "usage": {"prompt_tokens": 1, "total_tokens": 1},
                 "data": [{"object": "embedding", "index": k, "embedding": vec(str(x))} for k, x in enumerate(inp)]}
        else:
            # mem0 抽取：把用户原话当作一条事实返回，让旧内核真的写进 Qdrant（与 native 原样入库对齐）

            blob = json.dumps(j.get("messages", []), ensure_ascii=False)
            m = re.findall(r'第\d+条：[^\\"]+', blob)
            content = json.dumps({"memory": [{"text": m[-1]}]} if m else {"memory": []}, ensure_ascii=False)
            r = {"id": "x", "object": "chat.completion", "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                 "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}]}
        b = json.dumps(r).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


def post(path, body, timeout=60):
    body = dict(body, caller_user_id="bench")
    req = urllib.request.Request(f"http://127.0.0.1:{APP_PORT}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-Caller-User-Id": "bench", **_auth_headers()})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read())


def tree_rss_mb(pid):
    """进程及其子进程 RSS 合计（MB）。"""
    tot = 0
    pids = [pid]
    while pids:
        p = pids.pop()
        try:
            tot += int([l for l in open(f"/proc/{p}/status") if l.startswith("VmRSS")][0].split()[1])
            pids += [int(x) for x in open(f"/proc/{p}/task/{p}/children").read().split()]
        except Exception:
            pass
    return tot / 1024


def hwm_mb(pid):
    return int([l for l in open(f"/proc/{pid}/status") if l.startswith("VmHWM")][0].split()[1]) / 1024


def qdrant_mb():
    out = subprocess.run(["sudo", "docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", "bench-qdrant"],
                         capture_output=True, text=True).stdout.strip().split("/")[0].strip()
    n = float(out[:-3]); u = out[-3:]
    return n * {"MiB": 1, "GiB": 1024, "KiB": 1 / 1024}[u]


def du_mb(path):
    if not os.path.exists(path):
        return 0.0
    out = subprocess.run(["sudo", "du", "-sk", path], capture_output=True, text=True).stdout.split()[0]
    return int(out) / 1024


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code"); ap.add_argument("--venv"); ap.add_argument("--label")
    ap.add_argument("--qdrant", action="store_true"); ap.add_argument("--kernel", default="native")
    ap.add_argument("--adds", type=int, default=2000); ap.add_argument("--searches", type=int, default=300)
    a = ap.parse_args()
    os.environ["AIDUMEM_API_TOKEN"] = TOKEN
    work = os.path.abspath(f"run-{a.label}")
    subprocess.run(["sudo", "rm", "-rf", work]); os.makedirs(work + "/data"); os.makedirs(work + "/home")
    qvol = work + "/qdrant"
    res = {"label": a.label}

    # 开箱：安装体积
    res["venv_mb"] = round(du_mb(a.venv))
    pk = subprocess.run(["uv", "pip", "list", "--python", a.venv + "/bin/python"], capture_output=True, text=True).stdout
    res["packages"] = len(pk.strip().splitlines()) - 2
    res["direct_deps"] = sum(1 for l in open(a.code + "/requirements.txt") if l.strip() and not l.strip().startswith("#"))

    threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", STUB_PORT), Stub).serve_forever, daemon=True).start()

    cfg = json.load(open(a.code + "/mem0_config_local.json.example"))
    for k in ("llm", "embedder"):
        cfg[k]["config"].update(openai_base_url=f"http://127.0.0.1:{STUB_PORT}/v1", api_key="stub", model="stub")
    cfg.get("rerank", {})["enabled"] = False
    cfg.get("_features", {})["vision"] = False
    if a.qdrant:
        cfg["vector_store"]["config"] = {"collection_name": "mem0", "url": f"http://127.0.0.1:{QPORT}",
                                         "on_disk": True, "embedding_model_dims": D}
        subprocess.run(["sudo", "docker", "rm", "-f", "bench-qdrant"], capture_output=True)
        os.makedirs(qvol)
        subprocess.run(["sudo", "docker", "run", "-d", "--name", "bench-qdrant", "-p", f"127.0.0.1:{QPORT}:6333",
                        "-v", f"{qvol}:/qdrant/storage", "qdrant/qdrant:v1.20.0"], check=True, capture_output=True)
        res["qdrant_image_mb"] = round(int(subprocess.run(["sudo", "docker", "image", "inspect", "qdrant/qdrant:v1.20.0",
                                       "--format", "{{.Size}}"], capture_output=True, text=True).stdout) / 1e6)
        time.sleep(4)
    else:
        cfg["vector_store"]["config"]["path"] = work + "/data/qdrant"
    cfg["history_db_path"] = work + "/data/history.db"
    json.dump(cfg, open(a.code + "/mem0_config_local.json", "w"))

    env = dict(os.environ, AIDUMEM_DATA_DIR=work + "/data", HOME=work + "/home", AIDUMEI_MEMORY_KERNEL=a.kernel,
               AIDUMEI_RATE_ADD_PER_MIN="1000000", AIDUMEI_RATE_ADD_GLOBAL_PER_MIN="1000000", PYTHONUNBUFFERED="1", AIDUMEM_API_TOKEN=TOKEN,
               AIDUMEM_API_PORT=str(APP_PORT), AIDUMEI_API_PORT=str(APP_PORT), PORT=str(APP_PORT))
    t0 = time.time()
    log = open(work + "/server.log", "w")
    srv = subprocess.Popen([a.venv + "/bin/python", "api_server.py", "--port", str(APP_PORT)], cwd=a.code, env=env,
                           stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        while True:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{APP_PORT}/health", timeout=2).read(); break
            except Exception:
                if time.time() - t0 > 180 or srv.poll() is not None:
                    raise SystemExit(json.dumps(dict(res, error="server did not start")))
                time.sleep(0.3)
        res["startup_s"] = round(time.time() - t0, 1)
        time.sleep(10)
        res["idle_app_mb"] = round(tree_rss_mb(srv.pid))
        res["idle_qdrant_mb"] = round(qdrant_mb()) if a.qdrant else 0
        res["idle_total_mb"] = res["idle_app_mb"] + res["idle_qdrant_mb"]

        # 使用：写入 + 检索
        random.seed(7)
        cities = ["上海", "北京", "杭州", "东京", "大阪", "成都", "广州", "西安"]
        foods = ["拉面", "火锅", "寿司", "咖啡", "豆浆", "饺子", "烤鸭", "米线"]
        ta, ok = [], 0
        for i in range(a.adds):
            msg = f"第{i}条：我在{random.choice(cities)}吃了{random.choice(foods)}，同行的人编号{i * 7 % 997}"
            t = time.time()
            try:
                st, _ = post("/add", {"messages": [{"role": "user", "content": msg}], "user_id": "bench"})
                ok += st == 200
            except Exception:
                pass
            ta.append((time.time() - t) * 1000)
        time.sleep(3)
        ts, hits = [], []
        for i in range(a.searches):
            t = time.time()
            try:
                _, j = post("/search", {"query": f"{random.choice(cities)}{random.choice(foods)}", "user_id": "bench"})
                hits.append(len(j.get("results", [])))
            except Exception:
                hits.append(0)
            ts.append((time.time() - t) * 1000)
        time.sleep(5)
        q = lambda xs, p: round(sorted(xs)[int(len(xs) * p) - 1], 1)
        res.update(adds_ok=ok, add_p50_ms=q(ta, .5), add_p95_ms=q(ta, .95),
                   search_p50_ms=q(ts, .5), search_p95_ms=q(ts, .95), search_avg_hits=round(sum(hits) / len(hits), 1),
                   used_app_mb=round(tree_rss_mb(srv.pid)), peak_app_mb=round(hwm_mb(srv.pid)),
                   used_qdrant_mb=round(qdrant_mb()) if a.qdrant else 0)
        res["used_total_mb"] = res["used_app_mb"] + res["used_qdrant_mb"]
        res["disk_data_mb"] = round(du_mb(work + "/data"), 1)
        res["disk_qdrant_mb"] = round(du_mb(qvol), 1) if a.qdrant else 0
    finally:
        os.killpg(srv.pid, signal.SIGTERM)
        if a.qdrant:
            subprocess.run(["sudo", "docker", "rm", "-f", "bench-qdrant"], capture_output=True)
        os.remove(a.code + "/mem0_config_local.json")
    print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
