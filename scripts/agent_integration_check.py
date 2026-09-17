#!/usr/bin/env python3
"""Validate the host-agent lifecycle against a running aiduMEI instance.

v20.3.1（九份审计 P0-3）修掉两处空断言 —— 上一版的「接入检查」给宿主的
绿灯有两个是假的：
  1. GET /gate 携带 JSON body：FastAPI 对 GET 只读 query string，body 里的
     nonce 被整个丢弃 → 恒命中 empty_query 早返回 → 任何 200 都算过，
     相关性闸门从未被真正测过。
  2. `values.count(nonce)` 是整串相等：写入的是 `f"{nonce} is the handshake."`，
     检索结果里永远不会有裸 nonce → 恒 count==0 → 恒过。重复注入五次它也绿。
判据改为：gate 必须真的判定过 nonce（needs_memory=True 且 reason 非 empty_query）；
重复检测改为子串包含计数，且带「故意注入两次必须变红」的负向对照（见测试）。
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ducky.utils import api_auth_headers

BASE_URL = os.environ.get("AIDUMEM_API_BASE", "http://127.0.0.1:8767").rstrip("/")
STEPS: list[dict] = []


def request(method: str, path: str, body=None, expect=(200,)):
    headers = {"Content-Type": "application/json"}
    headers.update(api_auth_headers())
    data = None
    if method.upper() == "GET" and isinstance(body, dict):
        # v20.3.1：GET 的参数走 query string —— 服务端 GET 路由只读 URL
        # 参数，JSON body 会被整个丢弃（这正是上一版 /gate 假绿灯的根因）。
        qs = urllib.parse.urlencode({k: v for k, v in body.items() if v is not None})
        if qs:
            path = f"{path}?{qs}"
    elif body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(f"{BASE_URL}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode())
        except Exception:
            body = {}
        return exc.code, body


def check(name: str, ok: bool, data):
    STEPS.append({"name": name, "status": "pass" if ok else "fail", "data": data})
    if not ok:
        raise AssertionError(f"{name} failed: {data}")


# 与 /health 的 ingest_liveness 探针同一口径（两处判据必须同源，
# 否则「脚本说通过、探针说降级」会让人无所适从）。
try:
    from ducky.env_config import int_env as _int_env
    _INGEST_MIN_READS = _int_env("AIDUMEI_INGEST_MIN_READS", 5, minimum=1)
except Exception:
    _INGEST_MIN_READS = 5  # 脱离仓库单跑时的兜底


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default=f"agent-integration-{int(time.time())}-{secrets.token_hex(4)}")
    args = parser.parse_args()
    # v20.3.1（九份审计 P2-1）：测试脚本不许对任意租户清库。
    # --tenant 若被诱导传 default（或部署的真实默认租户），一次「验收」就是一次清库。
    tenant = args.tenant
    if tenant == "default" or tenant == os.environ.get("AIDUMEM_DEFAULT_USER_ID"):
        print(json.dumps({"status": "fail", "error": "refusing to run against the default tenant"}))
        return 2
    if not (tenant.startswith("agent-integration-") or tenant.startswith("e2e-smoke-")):
        print(json.dumps({"status": "fail", "error": "tenant must start with agent-integration- or e2e-smoke-"}))
        return 2
    try:
        status, health = request("GET", "/health")
        check("health", status == 200 and health.get("health_status") == "ok", health)
        nonce = f"integration-{secrets.token_hex(8)}"
        status, added = request("POST", "/add", {
            "messages": [{"role": "user", "content": f"{nonce} is the integration handshake."}],
            "user_id": tenant, "bank_id": "default", "infer": False,
        })
        check("add", status == 200, added)
        status, gate = request("GET", "/gate", {"query": f"remember {nonce}", "user_id": tenant, "bank_id": "default"})
        # v20.3.1：只看 200 是假绿灯 —— empty_query 也是 200。闸门必须
        # 真的对本次 nonce 做过相关性判定。
        check("gate",
              status == 200 and gate.get("needs_memory") is True and gate.get("reason") != "empty_query",
              gate)
        status, searched = request("POST", "/search", {"query": nonce, "user_id": tenant, "bank_id": "default", "limit": 5})
        check("search", status == 200 and searched.get("recall_verdict") == "found", searched)
        status, raw = request("POST", "/add/raw", {"content": f"integration raw {nonce}", "user_id": tenant, "bank_id": "default"})
        check("raw", status == 200 and raw.get("status") in {"ok", "partial"}, raw)
        status, injected = request("POST", "/api/core-memory/inject", {"query": nonce, "user_id": tenant, "bank_id": "default"})
        check("core-inject", status == 200, injected)
        status, session_start = request("POST", "/session/start", {"user_id": tenant, "bank_id": "default"})
        check("session-start", status == 200, session_start)
        session_id = session_start.get("session_id") if isinstance(session_start, dict) else None
        if session_id:
            status, session_end = request("POST", f"/session/end?session_id={session_id}&user_id={tenant}&bank_id=default")
            check("session-end", status == 200, session_end)
        results = searched.get("results", [])
        values = [r.get("memory") or r.get("content") or r.get("fact_value") for r in results]
        # v20.3.1：整串相等改为子串包含 —— 写入的是 f"{nonce} is the handshake."，
        # 检索回来的 memory 字段几乎不可能恰好是裸 nonce。count()==0 恒过
        # 的旧判据抓不到任何东西；子串计数才是「同一条记忆被注入几次」。
        dup_count = sum(1 for v in values if v and nonce in str(v))
        check("no-duplicate-injection", dup_count <= 1, {"values": values[:10], "dup_count": dup_count})
        status, cleanup = request("POST", "/delete_all", {"user_id": tenant, "bank_id": "default", "confirm": True})
        check("cleanup", status in (200, 207) and cleanup.get("status") == "committed", cleanup)

        # ── 宿主接线（v21.2.0：这一段是一次真实事故的产物）──────────────
        #
        # 上面每一项检查的都是「aiduMEI 的 API 能不能用」—— 本脚本自己调
        # /add、自己调 /search，当然全绿。但它们**从不回答**真正要紧的那个
        # 问题：**宿主到底有没有在调这些接口。**
        #
        # 2026-09-17 我们在自己的生产部署上吃了这个亏：读钩子挂着、写钩子
        # 从没挂过，这个脚本照样 pass，/health 照样全绿，持续一个月无人察觉
        # —— 因为判据测的是被集成方，不是集成本身。
        #
        # 判据用真实流量（不是本脚本造的临时租户）：在读、却完全不在写，
        # 是接线错误的铁证；没有读，说明还没真用起来，如实说「还判断不了」。
        _probes = (health or {}).get("probes") or {}
        _reads = _probes.get("ingest_reads_24h")
        # 与 /health 同源：判据看「来自对话的检索」，不是检索总数。总数里
        # 混着 e2e_smoke 每小时一次的巡检心跳，拿它当「有人在用」会必然误报。
        _conv_reads = _probes.get("ingest_conv_reads_24h")
        if _conv_reads is None:
            _conv_reads = _reads
        _writes = _probes.get("ingest_writes_24h")
        # 判据用「来自对话的写入」，不是写入总数：总数里混着 cron 整合器与
        # MEMORY.md 同步引擎等后台通路，实测那台出事的机器每天有 6~18 条
        # 后台写入、对话写入恒为 0 —— 拿总数判会在真事故上恒绿。
        # 旧服务端没有这个键时退回总数（宁可少报，不假红）。
        _turn_writes = _probes.get("ingest_turn_writes_24h")
        if _turn_writes is None:
            _turn_writes = _writes
        if _reads is None:
            check("host-wiring", True,
                  {"verdict": "unknown",
                   "why": "服务端无写入活性探针（旧版本）",
                   "next": "升级后运行 scripts/check_ingest_wiring.py"})
        elif _conv_reads < _INGEST_MIN_READS:
            # 刚部署没有流量是正常的 —— 不许拿假红灯挡住新用户，
            # 但必须把「这件事还没验」明明白白说出来，不许沉默放行。
            check("host-wiring", True,
                  {"verdict": "not_yet_verifiable",
                   "reads_24h": _reads, "conv_reads_24h": _conv_reads,
                   "writes_24h": _writes, "turn_writes_24h": _turn_writes,
                   "next": "⚠️ 真实用过几轮对话后，务必运行 "
                           "scripts/check_ingest_wiring.py 确认写入钩子真的在工作 —— "
                           "只挂读钩子不挂写钩子时，一切看起来都正常，但新对话一句都不会被记住"})
        else:
            check("host-wiring", (_turn_writes or 0) > 0,
                  {"verdict": "wired" if (_turn_writes or 0) > 0 else "READ_ONLY",
                   "reads_24h": _reads, "conv_reads_24h": _conv_reads,
                   "writes_24h": _writes, "turn_writes_24h": _turn_writes,
                   "fix": "宿主在读，但没有一条来自对话的写入。现成脚本："
                          "integrations/aidumem-ingest.sh（Hermes post_llm_call）或 "
                          "integrations/cursor-hook/claude-code-stop-hook.py（Claude Code Stop）；"
                          "已挂上还红就是没透传 _origin_session_id。见 docs/AGENT_INTEGRATION.md"})
    except AssertionError as exc:
        print(json.dumps({"status": "fail", "steps": STEPS, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"status": "pass", "steps": STEPS}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
