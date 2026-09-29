#!/usr/bin/env python3
"""aiduMEI end-to-end smoke: prove memory actually works, not merely that /health responds.

The script intentionally talks to a running service over HTTP. It never imports
the runtime or opens a production database directly.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

import requests

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from ducky.utils import api_auth_headers as auth_headers  # noqa: E402
from ducky.utils import mem0_config_path  # noqa: E402
from ducky.env_config import int_env  # noqa: E402
from ducky.engine_mode import configured_mode  # noqa: E402
from ducky.mem0_runtime import _resolve_api_keys  # noqa: E402


def _default_api() -> str:
    if os.environ.get("AIDUMEM_API_BASE"):
        return os.environ["AIDUMEM_API_BASE"].rstrip("/")
    # Match api_server.main(), including invalid-value fallback and bounds.
    for name in ("AIDUMEM_API_PORT", "MEM0_API_PORT", "PORT"):
        raw = os.environ.get(name)
        if raw:
            break
    port = int_env(name, 8767, minimum=1, maximum=65535, raw=raw)
    return f"http://127.0.0.1:{port}"


class Smoke:
    def __init__(self, api: str, tenant: str, wait_seconds: float, json_only: bool):
        self.api = api
        self.tenant = tenant
        self.wait_seconds = wait_seconds
        self.json_only = json_only
        self.headers = auth_headers()
        self.results: list[dict[str, Any]] = []
        self.failures = 0
        self.warnings = 0
        self.health_data: dict[str, Any] = {}

    def record(self, name: str, status: str, detail: str = "", data: Any = None) -> bool:
        item = {
            "step": name,
            "status": status,
            "detail": detail,
        }
        if data is not None:
            item["data"] = data
        self.results.append(item)
        if status == "FAIL":
            self.failures += 1
            return False
        if status == "WARN":
            self.warnings += 1
        return status != "FAIL"

    def request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("headers", self.headers)
        return requests.request(method, f"{self.api}{path}", timeout=30, **kwargs)

    def _get_json(self, response: requests.Response) -> dict[str, Any]:
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(f"non-JSON response ({response.status_code}): {response.text[:200]}") from exc

    def health(self) -> None:
        try:
            response = self.request("GET", "/health")
            data = self._get_json(response)
            self.health_data = data
        except Exception as exc:
            self.record("health", "FAIL", f"cannot reach /health: {exc}")
            return
        if response.status_code != 200:
            self.record("health", "FAIL", f"/health returned {response.status_code}", data)
            return
        if data.get("health_status") != "ok":
            self.record("health", "FAIL", "health_status is not ok", data)
            return
        probes = data.get("probes") or {}
        required = ("facts_db", "text_fts_db", "mem0_singleton", "port_service")
        missing = [name for name in required if probes.get(name) is not True]
        if missing:
            self.record("health", "FAIL", f"failed probes: {missing}", probes)
            return
        self.record("health", "PASS", "service is reachable and core probes are true", {
            "version": data.get("version"),
            "degraded": data.get("degraded"),
            "runtime_paths": probes.get("runtime_paths"),
        })

    def config(self) -> None:
        data_path = Path(mem0_config_path())
        config_source = "AIDUMEM_CONFIG_FILE" if os.environ.get("AIDUMEM_CONFIG_FILE") else "repo_default"
        server_mode = ((self.health_data.get("probes") or {})
                       .get("engine_mode_policy") or {}).get("configured")
        mode = server_mode if server_mode in {"auto", "cloud", "local"} else configured_mode()
        if mode == "local":
            # Local gear has no cloud credentials by design. The live health,
            # write, recall and cleanup steps in run() verify actual readiness.
            self.record("config", "PASS", "local mode requires no cloud keys", {
                "mode": mode, "config_path": str(data_path), "config_source": config_source,
            })
            return
        if not data_path.exists():
            self.record("config", "WARN", "mem0 config is absent; cloud gears may be unavailable", {
                "config_path": str(data_path),
                "config_source": config_source,
            })
            return
        try:
            config = json.loads(data_path.read_text(encoding="utf-8"))
            if not isinstance(config, dict):
                raise ValueError("top-level config must be an object")
            config = _resolve_api_keys(config)
        except Exception as exc:
            self.record("config", "FAIL", f"mem0 config is invalid: {exc}", {
                "config_path": str(data_path),
                "config_source": config_source,
            })
            return
        llm_key = str((config.get("llm") or {}).get("config", {}).get("api_key") or "").strip()
        embed_key = str((config.get("embedder") or {}).get("config", {}).get("api_key") or "").strip()
        placeholder_keys = sorted(
            name for name, value in (("llm", llm_key), ("embedder", embed_key))
            if self._is_placeholder(value)
        )
        missing_keys = sorted(
            name for name, value in (("llm", llm_key), ("embedder", embed_key))
            if not value
        )
        if placeholder_keys or missing_keys:
            self.record(
                "config",
                "WARN",
                "cloud credentials contain placeholder keys or are absent; "
                "set real keys or use AIDUMEI_ENGINE_MODE=local.",
                {
                    "config_path": str(data_path),
                    "config_source": config_source,
                    "placeholder_keys": placeholder_keys,
                    "missing_keys": missing_keys,
                    "mode": mode,
                },
            )
            return
        self.record("config", "PASS", "effective cloud credentials are non-placeholder", {
            "config_path": str(data_path),
            "config_source": config_source,
            "mode": mode,
        })

    @staticmethod
    def _is_placeholder(value: str) -> bool:
        normalized = (value or "").strip().lower()
        hints = ("your_", "replace_", "change_me", "<", "xxx", "placeholder", "__llm_key__", "__sf_key__")
        return bool(normalized) and any(hint in normalized for hint in hints)

    def add_and_recall(self) -> None:
        stamp = dt.datetime.now().strftime("%Y%m%d%H%M%S")
        nonce = f"aidumei-smoke-{stamp}"
        content = f"{nonce} 的验收暗号是 ORANGE-HORIZON-2026。"
        body = {
            "messages": [{"role": "user", "content": content}],
            "user_id": self.tenant,
            "bank_id": "default",
            "async_mode": False,
            "infer": False,
        }
        try:
            response = self.request("POST", "/add", json=body)
            data = self._get_json(response)
        except Exception as exc:
            self.record("add", "FAIL", f"/add failed: {exc}")
            return
        if response.status_code != 200:
            self.record("add", "FAIL", f"/add returned {response.status_code}", data)
            return
        action = data.get("action")
        if action in {"coalesce_buffered", "coalesce_flushed"}:
            self.record("add", "WARN", "write is still in the coalesce queue", {"action": action})
        else:
            self.record("add", "PASS", "write accepted", {"action": action or "direct"})

        if action in {"coalesce_buffered", "coalesce_flushed"}:
            try:
                response = self.request("POST", "/add/coalesce/flush", params={"user_id": self.tenant, "bank_id": "default", "force": "true"})
                data = self._get_json(response)
                if response.status_code != 200:
                    self.record("flush", "FAIL", f"flush returned {response.status_code}", data)
                else:
                    self.record("flush", "PASS", "coalesce queue flushed", data)
            except Exception as exc:
                self.record("flush", "FAIL", f"flush failed: {exc}")
        if self.wait_seconds:
            time.sleep(self.wait_seconds)

        try:
            response = self.request("POST", "/search", json={
                "query": nonce,
                "user_id": self.tenant,
                # v22.0（A3）：smoke 经 API token 调用，声明自己是谁（读自己殿）
                "caller_user_id": self.tenant,
                "bank_id": "default",
                "limit": 5,
            })
            data = self._get_json(response)
        except Exception as exc:
            self.record("recall", "FAIL", f"/search failed: {exc}")
            return
        if response.status_code != 200:
            self.record("recall", "FAIL", f"/search returned {response.status_code}", data)
            return
        if data.get("recall_verdict") != "found":
            self.record("recall", "FAIL", f"recall verdict is {data.get('recall_verdict')!r}", data)
            return
        results = data.get("results") or []
        if not results:
            self.record("recall", "FAIL", "verdict was found but results are empty", data)
            return
        self.record("recall", "PASS", "new-session recall found the nonce", {
            "recall_verdict": data.get("recall_verdict"),
            "engine_mode": data.get("engine_mode"),
            "recall_path": data.get("_recall_path"),
            "top_score": (data.get("_recall_strength") or {}).get("top_score"),
        })

    def trace(self) -> None:
        try:
            response = self.request("POST", "/search_trace", json={
                "query": f"aidumei-smoke-{dt.datetime.now().strftime('%Y%m%d')}",
                "user_id": self.tenant,
                "caller_user_id": self.tenant,
                "bank_id": "default",
                "limit": 5,
            })
            data = self._get_json(response)
        except Exception as exc:
            self.record("trace", "FAIL", f"/search_trace failed: {exc}")
            return
        if response.status_code != 200:
            self.record("trace", "FAIL", f"/search_trace returned {response.status_code}", data)
            return
        trace = data.get("trace") or data.get("funnel") or {}
        if not trace:
            self.record("trace", "WARN", "trace response has no trace/funnel field", data)
            return
        self.record("trace", "PASS", "trace endpoint returned a trace object", {
            "keys": sorted(trace)[:10] if isinstance(trace, dict) else str(type(trace)),
        })

    def cleanup(self) -> None:
        try:
            response = self.request("POST", "/delete_all", json={
                "user_id": self.tenant,
                "bank_id": "default",
                "confirm": True,
            })
            data = self._get_json(response)
        except Exception as exc:
            self.record("cleanup", "FAIL", f"cleanup failed: {exc}")
            return
        if response.status_code not in (200, 207):
            self.record("cleanup", "FAIL", f"cleanup returned {response.status_code}", data)
            return
        if data.get("status") != "committed":
            self.record("cleanup", "FAIL", "cleanup did not fully commit", data)
            return
        self.record("cleanup", "PASS", "smoke tenant cleared", {"status": data.get("status")})

    def run(self) -> dict[str, Any]:
        self.health()
        self.config()
        self.add_and_recall()
        self.trace()
        self.cleanup()
        return {
            "status": "FAIL" if self.failures else ("WARN" if self.warnings else "PASS"),
            "failures": self.failures,
            "warnings": self.warnings,
            "steps": self.results,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default=_default_api())
    parser.add_argument("--tenant", default=f"e2e-smoke-{int(time.time())}-{secrets.token_hex(4)}")
    parser.add_argument("--wait", type=float, default=0.0)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON only")
    args = parser.parse_args()
    smoke = Smoke(args.api, args.tenant, args.wait, args.json)
    report = smoke.run()
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
