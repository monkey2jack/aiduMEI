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
    except AssertionError as exc:
        print(json.dumps({"status": "fail", "steps": STEPS, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"status": "pass", "steps": STEPS}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
