#!/usr/bin/env python3
"""Validate the host-agent lifecycle against a running aiduMEI instance."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ducky.utils import api_auth_headers

BASE_URL = os.environ.get("AIDUMEM_API_BASE", "http://127.0.0.1:8767").rstrip("/")
STEPS: list[dict] = []


def request(method: str, path: str, body=None, expect=(200,)):
    headers = {"Content-Type": "application/json"}
    headers.update(api_auth_headers())
    data = json.dumps(body).encode() if body is not None else None
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
    parser.add_argument("--tenant", default=f"agent-integration-{int(time.time())}")
    args = parser.parse_args()
    try:
        status, health = request("GET", "/health")
        check("health", status == 200 and health.get("health_status") == "ok", health)
        nonce = f"integration-{secrets.token_hex(8)}"
        status, added = request("POST", "/add", {
            "messages": [{"role": "user", "content": f"{nonce} is the integration handshake."}],
            "user_id": args.tenant, "bank_id": "default", "infer": False,
        })
        check("add", status == 200, added)
        status, gate = request("GET", "/gate", {"query": nonce, "user_id": args.tenant, "bank_id": "default"})
        check("gate", status == 200, gate)
        status, searched = request("POST", "/search", {"query": nonce, "user_id": args.tenant, "bank_id": "default", "limit": 5})
        check("search", status == 200 and searched.get("recall_verdict") == "found", searched)
        status, raw = request("POST", "/add/raw", {"content": f"integration raw {nonce}", "user_id": args.tenant, "bank_id": "default"})
        check("raw", status == 200 and raw.get("status") in {"ok", "partial"}, raw)
        status, injected = request("POST", "/api/core-memory/inject", {"query": nonce, "user_id": args.tenant, "bank_id": "default"})
        check("core-inject", status == 200, injected)
        status, session_start = request("POST", "/session/start", {"user_id": args.tenant, "bank_id": "default"})
        check("session-start", status == 200, session_start)
        session_id = session_start.get("session_id") if isinstance(session_start, dict) else None
        if session_id:
            status, session_end = request("POST", "/session/end", {"session_id": session_id, "user_id": args.tenant, "bank_id": "default"})
            check("session-end", status == 200, session_end)
        results = searched.get("results", [])
        values = [r.get("memory") or r.get("content") or r.get("fact_value") for r in results]
        check("no-duplicate-injection", values.count(nonce) <= 1, {"values": values[:10]})
        status, cleanup = request("POST", "/delete_all", {"user_id": args.tenant, "bank_id": "default", "confirm": True})
        check("cleanup", status in (200, 207) and cleanup.get("status") == "committed", cleanup)
    except AssertionError as exc:
        print(json.dumps({"status": "fail", "steps": STEPS, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"status": "pass", "steps": STEPS}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
