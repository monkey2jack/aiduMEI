"""f0.6+++ WP1 · 真实使用回归集 v3 运行器(零 token · 本地 · 秒级 · 内核无关)。

取代 goldenset.py(v1 种子占位 + v2 只判 id 交集)。用例只对 HTTP 公开契约
说话(/add /update /delete /search),判分看**返回文本**而不是内核私有 id,
所以同一套用例可以打 mem0 内核、native 内核或远端测试实例(--url),WP3
影子比对直接复用。

五类判据(case-level 通过/不通过,类得分 = 通过率):
  fact_recall / temporal  top_k 内出现 expect 任一条
  update                  自然问法 ∪ 旧原文探针:新值在、旧值不在
  delete                  删前按原文能搜到(前提),删后自然问法与原文探针都搜不到
  abstain                 recall_verdict != found(没有就说没有)
  cc_replay               会话 N 写入 → 会话 N+1 的 inject(/search limit 3)带出 expect

本地模式:进程内起真实 app(TestClient),临时数据目录,嵌入用确定性字符
n-gram 哈希(离线、零 token);LLM/嵌入的 base_url 指向 127.0.0.1:9,任何
漏网的外呼当场失败而不是花钱。阈值钉生产已标定的 0.46。

跑法: python -m benchmarks.regression [--out f.json] [--baseline f.json] [--url URL]
     --baseline:任一类得分低于基线即红(exit 1)——逐类不降。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
from pathlib import Path

CASES_PATH = Path(__file__).parent / "regression_v3.json"
CATEGORIES = ("fact_recall", "update", "delete", "temporal", "abstain", "cc_replay")
USER, BANK = "u_reg", "default"
DIM = 256


def hash_embed(text, *_a, **_k):
    """确定性离线嵌入:字符 1/2-gram 哈希到 256 维再 L2 归一。"""
    v = [0.0] * DIM
    s = (text or "").lower()
    for g in list(s) + [s[i:i + 2] for i in range(len(s) - 1)]:
        v[int(hashlib.md5(g.encode()).hexdigest()[:8], 16) % DIM] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class Adapter:
    """薄适配器:只走 HTTP 契约。local=进程内 app;url=远端实例。"""

    def __init__(self, url: str = ""):
        self.url = url.rstrip("/")
        if not self.url:
            self._boot_local()

    def _boot_local(self):
        tmp = tempfile.mkdtemp(prefix="aidumei_reg_", dir="/dev/shm" if os.path.isdir("/dev/shm") else None)
        dead = "http://127.0.0.1:9/v1"
        cfg = {"llm": {"provider": "openai", "config": {"model": "offline", "api_key": "offline-harness", "openai_base_url": dead}},
               "embedder": {"provider": "openai", "config": {"model": "offline", "api_key": "offline-harness",
                                                            "openai_base_url": dead, "embedding_dims": DIM}},
               "vector_store": {"provider": "qdrant", "config": {"collection_name": "reg", "path": os.path.join(tmp, "qdrant"),
                                                                 "embedding_model_dims": DIM, "on_disk": True}},
               "history_db_path": os.path.join(tmp, "history.db")}
        Path(tmp, "cfg.json").write_text(json.dumps(cfg))
        for k in ("OPENAI_API_KEY", "AIDUMEI_LLM_API_KEY", "AIDUMEI_EMBEDDER_API_KEY", "AIDUMEI_RERANKER_API_KEY",
                  "AIDUMEI_API_TOKEN", "AIDUMEM_API_TOKEN"):
            os.environ.pop(k, None)
        os.environ.update({"AIDUMEM_DATA_DIR": tmp, "AIDUMEM_CONFIG_FILE": os.path.join(tmp, "cfg.json"),
                           "AIDUMEI_RECALL_VERDICT_THRESHOLD": os.environ.get("AIDUMEI_RECALL_VERDICT_THRESHOLD", "0.46"),
                           "AIDUMEM_RECALL_SCORE_FLOOR": "0",   # 召回类判排序;0.46 只作 abstain 的判语线
                           "AIDUMEI_RATE_ADD_PER_MIN": "0", "AIDUMEI_RATE_ADD_GLOBAL_PER_MIN": "0",  # 灌库不是失控循环
                           "AIDUMEM_REFLECT_ENABLED": "false", "AIDUMEM_SELF_EDIT_ENABLED": "false",
                           "HF_HUB_OFFLINE": "1"})
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import logging
        logging.disable(logging.CRITICAL)
        from fastapi.testclient import TestClient
        from api_server import app
        import ducky.utils
        if os.path.realpath(ducky.utils.DATA_DIR) != os.path.realpath(tmp):   # 早于本函数 import 过 ducky = 会写进真库
            raise RuntimeError(f"数据目录未隔离: {ducky.utils.DATA_DIR} —— 拒绝运行")
        from ducky.schema_bootstrap import ensure_core_schema
        from ducky.mem0_runtime import get_memory
        ensure_core_schema()
        get_memory().embedding_model.embed = hash_embed
        self.tmp, self.client = tmp, TestClient(app, raise_server_exceptions=False)

    def _post(self, path: str, body: dict) -> dict:
        body = {"user_id": USER, "bank_id": BANK, **body}
        if not self.url:
            r = self.client.post(path, json=body)
            status, data = r.status_code, r.json()
        else:
            import urllib.request
            from ducky.utils import api_auth_headers
            req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json", **api_auth_headers()})
            with urllib.request.urlopen(req, timeout=30) as resp:
                status, data = resp.status, json.loads(resp.read())
        if status >= 400 or data.get("status") == "error":
            raise RuntimeError(f"{path} -> {status} {str(data)[:200]}")
        return data

    def add(self, text: str, session: str = ""):
        meta = {"_origin_session_id": session} if session else {}
        self._post("/add", {"messages": text, "infer": False, "metadata": meta})

    def search(self, query: str, top_k: int = 5, session: str = "") -> tuple[list[str], str]:
        body = {"query": query, "limit": top_k, "top_k": top_k}
        if session:
            body["session_id"] = session
        d = self._post("/search", body)
        return [str(r.get("memory") or "") for r in (d.get("results") or [])[:top_k]], d.get("recall_verdict", "")

    def _id_of(self, text: str) -> str:
        d = self._post("/search", {"query": text, "limit": 20})
        for r in d.get("results") or []:                # verbatim 抽屉条目不是可改写的记忆本体
            if _norm(r.get("memory")) == _norm(text) and not str(r.get("id", "")).startswith(("verbatim:", "raw-")):
                return r["id"]
        raise RuntimeError(f"找不到待操作记忆: {text[:40]}")

    def update(self, old: str, new: str):
        self._post("/update", {"memory_id": self._id_of(old), "content": new})

    def delete(self, text: str):
        self._post("/delete", {"memory_id": self._id_of(text)})


class KernelAdapter(Adapter):
    """直打内核(get_memory() 句柄)、绕过 HTTP 层:WP3 新旧内核同口径对比。

    abstain 判语用同一条阈值线(top1 score >= 阈值 即 found);update/delete 按原文找 id。
    """

    def __init__(self, kernel: str):
        os.environ["AIDUMEI_MEMORY_KERNEL"] = kernel
        self.url = ""
        self._boot_local()
        from ducky.mem0_runtime import get_memory
        self.mem, self.thr = get_memory(), float(os.environ["AIDUMEI_RECALL_VERDICT_THRESHOLD"])

    def add(self, text: str, session: str = ""):
        meta = {"_origin_session_id": session} if session else {}
        self.mem.add(text, user_id=USER, metadata=meta, infer=False)

    def search(self, query: str, top_k: int = 5, session: str = "") -> tuple[list[str], str]:
        rows = self.mem.search(query, filters={"user_id": USER}, top_k=top_k).get("results") or []
        verdict = "found" if rows and float(rows[0].get("score") or 0) >= self.thr else "not_found"
        return [str(r.get("memory") or "") for r in rows[:top_k]], verdict

    def _id_of(self, text: str) -> str:
        for r in self.mem.search(text, filters={"user_id": USER}, top_k=20).get("results") or []:
            if _norm(r.get("memory")) == _norm(text):
                return r["id"]
        raise RuntimeError(f"找不到待操作记忆: {text[:40]}")

    def update(self, old: str, new: str):
        self.mem.update(self._id_of(old), new)

    def delete(self, text: str):
        self.mem.delete(self._id_of(text))


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _hit(texts: list[str], wanted: list[str]) -> bool:
    """期望命中:期望片段必须出现在某条召回正文里(单向)。

    f0.7 P2-1(外审 muse P1-08):此前双向子串,召回只返回一个短片段(如「端口」)也会被判命中。"""
    got = [_norm(t) for t in texts]
    return any(w and any(w in g for g in got if g) for w in map(_norm, wanted))


def _touches(texts: list[str], forbidden: list[str]) -> bool:
    """禁止命中:旧值/已删内容与召回正文任一方向重叠即算出现(宁严勿松,删改类的安全口径)。"""
    got = [_norm(t) for t in texts]
    return any(w and any(w in g or g in w for g in got if g) for w in map(_norm, forbidden))


def load_cases(path: Path = CASES_PATH) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != "regression.v3":
        raise ValueError("回归集 schema 不是 regression.v3 —— 判分依据不明,拒绝运行")
    return data


def judge(ad: Adapter, c: dict) -> bool:
    cat, k = c["category"], c.get("top_k", 5)
    for st in c.get("steps", []):
        if st["op"] == "check":                      # 前提:删前按原文必能搜到(否则删后"搜不到"不说明问题)
            if not _hit(ad.search(st["expect"][0], k)[0], st["expect"]):
                return False
        elif st["op"] == "add":
            ad.add(st["text"], st.get("session", ""))
        elif st["op"] == "update":
            ad.update(st["old"], st["text"])
        elif st["op"] == "delete":
            ad.delete(st["text"])
    if cat == "cc_replay":
        k = 3                                         # CC inject 钩子固定 limit 3
    texts, verdict = ad.search(c["query"], k, c.get("session", ""))
    if cat == "abstain":
        return verdict != "found"
    if cat in ("delete", "update"):                   # 再用旧原文当探针:删了/改了就不许再原样出现
        texts += [t for f in c["forbid"] for t in ad.search(f, k)[0]]
    ok = _hit(texts, c["expect"]) if c.get("expect") else True
    return ok and not (c.get("forbid") and _touches(texts, c["forbid"]))


def run(url: str = "", path: Path = CASES_PATH, kernel: str = "") -> dict:
    t0 = time.time()
    data = load_cases(path)
    ad = KernelAdapter(kernel) if kernel else Adapter(url)
    try:
        return _run(ad, data, t0, url)
    finally:
        if not url:
            import shutil
            shutil.rmtree(ad.tmp, ignore_errors=True)          # 工完料净


def _run(ad: Adapter, data: dict, t0: float, url: str) -> dict:
    for m in data.get("corpus", []):
        ad.add(m["text"])
    t_seed = time.time() - t0
    per: dict[str, list[int]] = {}
    failed = []
    order = {c: i for i, c in enumerate(("fact_recall", "temporal", "abstain", "update", "delete", "cc_replay"))}
    for c in sorted(data["cases"], key=lambda c: order[c["category"]]):
        try:
            ok = judge(ad, c)
        except Exception as exc:                      # 故障记为不通过,不许静默当绿
            ok, c = False, {**c, "error": str(exc)[:160]}
        p = per.setdefault(c["category"], [0, 0])
        p[0] += ok
        p[1] += 1
        if not ok:
            failed.append({k: c[k] for k in ("id", "category", "error") if k in c})
    n_pass, n = sum(v[0] for v in per.values()), sum(v[1] for v in per.values())
    return {
        "schema": "regression.v3.result", "cases_version": data.get("version"),
        "per_category": {k: {"pass": v[0], "n": v[1], "score": round(v[0] / v[1], 4)}
                         for k, v in sorted(per.items(), key=lambda kv: CATEGORIES.index(kv[0]))},
        "total": {"pass": n_pass, "n": n, "score": round(n_pass / max(1, n), 4)},
        "runtime_s": round(time.time() - t0, 2), "seed_s": round(t_seed, 2),
        "mode": url or (f"kernel-direct:{os.environ.get('AIDUMEI_MEMORY_KERNEL')} " if isinstance(ad, KernelAdapter) else "") + "local-inprocess(hash-embed, verdict_threshold=%s)" % os.environ.get("AIDUMEI_RECALL_VERDICT_THRESHOLD"),
        "failed": failed,
    }


def check_baseline(baseline: dict) -> list[str]:
    """f0.7 P2-1(外审 muse P2-01):基线文件自身必须自洽——score 与 pass/n 不符(手改过)即拒用。"""
    bad = []
    for cat, b in {**baseline.get("per_category", {}), "total": baseline.get("total") or {}}.items():
        n, p, sc = b.get("n"), b.get("pass"), b.get("score")
        if not isinstance(n, int) or n <= 0 or not isinstance(p, int) or not (0 <= p <= n) \
                or sc is None or abs(round(p / n, 4) - float(sc)) > 1e-4:
            bad.append(f"{cat}: pass={p} n={n} score={sc}")
    return bad


def regressions(result: dict, baseline: dict) -> list[str]:
    """逐类不降:任一类得分低于基线、或基线有的类消失,即红。基线自身不自洽也红。"""
    out = [f"基线不自洽 {x}" for x in check_baseline(baseline)]
    for cat, b in baseline["per_category"].items():
        cur = result["per_category"].get(cat)
        if cur is None or cur["score"] < b["score"]:
            out.append(f"{cat}: {b['score']} -> {cur['score'] if cur else 'missing'}")
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--url", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--baseline", default="")
    ap.add_argument("--kernel", default="", help="mem0|native:绕过 HTTP 直打内核(WP3 新旧对比)")
    a = ap.parse_args(argv)
    res = run(a.url, kernel=a.kernel)
    if a.out:
        Path(a.out).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: res[k] for k in ("per_category", "total", "runtime_s")}, ensure_ascii=False))
    if a.baseline:
        bad = regressions(res, json.loads(Path(a.baseline).read_text(encoding="utf-8")))
        if bad:
            print("🔴 回归集 v3 退化: " + "; ".join(bad), file=sys.stderr)
            return 1
        print("🟢 回归集 v3 逐类不降")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
