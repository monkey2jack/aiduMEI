"""f0.3 A6 -- authorized /health: consolidator probe + git_sha; public view unchanged.

* probes.consolidator is read from <DATA_DIR>/consolidator_last_run.json:
  mode, candidates, per-status counts, a mismatch warning when an apply run's
  candidates != deleted + already_gone, and a stale warning after 36h.
* git_sha: AIDUMEI_BUILD_SHA, else `git rev-parse HEAD`, else "unknown"; resolved once.
* Neither may reach the anonymous public view -- not even indirectly by
  flipping its status/degraded fields.
"""
from __future__ import annotations

import json
import re
import subprocess
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ducky.hot.health as health_mod
from ducky import utils

_TOKEN = "f03-probe-token"
_AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_SHA = "0123456789abcdef0123456789abcdef01234567"


def _client():
    app = FastAPI()
    health_mod.register_health_routes(app)
    return TestClient(app)


@pytest.fixture
def data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("AIDUMEM_API_TOKEN", _TOKEN)
    monkeypatch.setenv("AIDUMEI_BUILD_SHA", _SHA)
    monkeypatch.setattr(utils, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(health_mod, "_GIT_SHA_CACHE", {})
    monkeypatch.setitem(health_mod._PUBLIC_CACHE, "full", None)
    monkeypatch.setitem(health_mod._PUBLIC_CACHE, "ts", 0.0)
    return tmp_path


def _write(data_dir, **overrides):
    summary = {
        "schema_version": 1, "status": "ok", "mode": "apply", "candidates": 5,
        "deletion": {"deleted": 4, "already_gone": 1, "partial": 0, "failed": 0},
        "timestamp": time.time() - 3600, "finished_at": "2026-09-29T02:30:00+00:00",
        "conflicts": {"mode": "warn", "pairs_found": 2, "pairs_applied": 0,
                      "memories_penalized": 0, "truncated": False},
    }
    summary.update(overrides)
    (data_dir / "consolidator_last_run.json").write_text(json.dumps(summary), encoding="utf-8")


def _consolidator_warnings(body):
    return [w for w in body["warnings"] if "consolidator" in w]


def test_authorized_view_carries_probe_and_git_sha(data_dir):
    _write(data_dir)
    body = _client().get("/health", headers=_AUTH).json()
    assert body["git_sha"] == _SHA
    probe = body["probes"]["consolidator"]
    assert probe["mode"] == "apply" and probe["candidates"] == 5
    assert probe["deletion"] == {"deleted": 4, "already_gone": 1, "partial": 0, "failed": 0}
    assert probe["mismatch"] is False and probe["stale"] is False
    assert probe["conflicts"]["pairs_found"] == 2
    assert _consolidator_warnings(body) == []
    assert "consolidator" not in body["degraded"]


def test_anonymous_public_view_carries_neither(data_dir):
    _write(data_dir, deletion={"deleted": 1, "already_gone": 0, "partial": 2, "failed": 2},
           timestamp=time.time() - 50 * 3600)          # mismatching AND stale
    anonymous = _client().get("/health")
    assert anonymous.status_code == 200
    assert "consolidator" not in anonymous.text
    assert "git_sha" not in anonymous.text and _SHA not in anonymous.text
    # negative control: the authorized view of the same instance does carry both
    authorized = _client().get("/health", headers=_AUTH).text
    assert "consolidator" in authorized and _SHA in authorized


def test_bad_consolidator_state_does_not_change_the_public_verdict(data_dir):
    public_before = _client().get("/health").json()
    _write(data_dir, status="error", error="boom", timestamp=time.time() - 90 * 3600,
           deletion={"deleted": 0, "already_gone": 0, "partial": 0, "failed": 5})
    health_mod._PUBLIC_CACHE.update({"full": None, "ts": 0.0})
    public_after = _client().get("/health").json()
    for key in ("status", "health_status", "degraded", "warming_up"):
        assert public_after[key] == public_before[key], key
    # ... while the authorized view does warn (the probe is not dead)
    assert len(_consolidator_warnings(_client().get("/health", headers=_AUTH).json())) >= 3


def test_apply_mismatch_is_a_warning(data_dir):
    _write(data_dir, deletion={"deleted": 3, "already_gone": 1, "partial": 0, "failed": 1})
    body = _client().get("/health", headers=_AUTH).json()
    assert body["probes"]["consolidator"]["mismatch"] is True
    assert len(_consolidator_warnings(body)) == 1


def test_dry_run_candidates_are_not_a_mismatch(data_dir):
    _write(data_dir, mode="dry-run",
           deletion={"deleted": 0, "already_gone": 0, "partial": 0, "failed": 0})
    body = _client().get("/health", headers=_AUTH).json()
    assert body["probes"]["consolidator"]["mismatch"] is None
    assert _consolidator_warnings(body) == []


def test_stale_summary_is_a_warning_fresh_is_not(data_dir):
    _write(data_dir, timestamp=time.time() - 40 * 3600)
    stale = _client().get("/health", headers=_AUTH).json()
    assert stale["probes"]["consolidator"]["stale"] is True
    assert len(_consolidator_warnings(stale)) == 1
    _write(data_dir, timestamp=time.time() - 35 * 3600)   # negative control: inside 36h
    fresh = _client().get("/health", headers=_AUTH).json()
    assert fresh["probes"]["consolidator"]["stale"] is False
    assert _consolidator_warnings(fresh) == []


def test_aborted_run_and_unreadable_summary_are_warnings(data_dir):
    _write(data_dir, status="aborted", abort_reason="api_unreachable")
    assert len(_consolidator_warnings(_client().get("/health", headers=_AUTH).json())) == 1
    (data_dir / "consolidator_last_run.json").write_text("{broken", encoding="utf-8")
    body = _client().get("/health", headers=_AUTH).json()
    assert body["probes"]["consolidator"].get("error")
    assert len(_consolidator_warnings(body)) == 1


def test_missing_summary_warns_only_after_36h_of_uptime(data_dir, monkeypatch):
    body = _client().get("/health", headers=_AUTH).json()
    assert body["probes"]["consolidator"]["present"] is False
    assert _consolidator_warnings(body) == [], "a fresh install has not reached its first run"
    monkeypatch.setattr(health_mod, "_START_TS", time.time() - 40 * 3600)
    body = _client().get("/health", headers=_AUTH).json()
    assert len(_consolidator_warnings(body)) == 1


# ---------------------------------------------------------------- git_sha resolution
def test_git_sha_falls_back_to_git_then_unknown(monkeypatch):
    monkeypatch.delenv("AIDUMEI_BUILD_SHA", raising=False)
    sha = health_mod._resolve_git_sha()
    real = subprocess.run(["git", "rev-parse", "HEAD"], cwd=health_mod._REPO_ROOT,
                          capture_output=True, text=True).stdout.strip()
    assert sha == (real if re.fullmatch(r"[0-9a-f]{40}", real) else "unknown")

    def no_git(*args, **kwargs):
        raise FileNotFoundError("git")
    monkeypatch.setattr(health_mod.subprocess, "run", no_git)
    assert health_mod._resolve_git_sha() == "unknown"


def test_invalid_build_sha_env_is_ignored(monkeypatch):
    monkeypatch.setenv("AIDUMEI_BUILD_SHA", "not a sha; rm -rf /")
    monkeypatch.setattr(health_mod.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="abcdef1\n"))
    assert health_mod._resolve_git_sha() == "abcdef1"


def test_git_sha_is_resolved_once(monkeypatch):
    monkeypatch.delenv("AIDUMEI_BUILD_SHA", raising=False)
    monkeypatch.setattr(health_mod, "_GIT_SHA_CACHE", {})
    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=_SHA + "\n")
    monkeypatch.setattr(health_mod.subprocess, "run", fake_run)
    assert health_mod._git_sha() == health_mod._git_sha() == _SHA
    assert len(calls) == 1
