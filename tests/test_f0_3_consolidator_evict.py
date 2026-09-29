"""f0.3 A1 -- consolidator eviction modes + truthful /delete accounting.

Production fact pinned here: `_delete_via_api` counted a delete only when
/delete answered status == "ok", but since v20.2.5-b /delete answers
committed / not_found (200), partial (207) or failed (500) -- never "ok".
Logs said "deleted 0/N" for 11 days while memories really were deleted.

The HTTP double below is a real socket server (so the real urllib path runs,
including HTTPError on 500 and the 207 pass-through). Its status-code table is
itself verified against the real ducky/hot/crud.py route in
test_fake_delete_double_matches_the_real_crud_route -- a double that is
looser or stricter than production manufactures green lights.
"""
from __future__ import annotations

import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import ducky.memory_salience as memory_salience_facade
import scripts.consolidator as cons
from ducky import utils
from ducky.salience import db as salience_db

# status -> HTTP code, exactly as ducky/hot/crud.py::delete answers
CRUD_STATUS_CODE = {"committed": 200, "not_found": 200, "partial": 207, "failed": 500}


def crud_body(status: str, **details) -> dict:
    failed = [{"layer": "fts", "error": "boom"}] if status in ("partial", "failed") else []
    return {"status": status, "details": details, "failed_layers": failed, "not_cleared": {}}


class _FakeApi:
    def __init__(self, delete_table: dict):
        self.delete_table = delete_table
        self.calls: list = []
        api = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep pytest output clean
                pass

            def _send(self, code: int, body: dict) -> None:
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                api.calls.append(("GET", self.path, None))
                self._send(200, {"status": "ok"}) if self.path == "/health" else self._send(404, {})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                api.calls.append(("POST", self.path, body))
                if self.path != "/delete":
                    self._send(404, {})
                    return
                status = api.delete_table.get(body.get("memory_id"), ("failed", {}))
                name, details = status
                self._send(CRUD_STATUS_CODE[name], crud_body(name, **details))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def deletes(self) -> list:
        return [body for method, path, body in self.calls if method == "POST" and path == "/delete"]


# memory id -> (crud status, details) ; details mimic wal_engine's res dict
APPLY_TABLE = {
    "m-del": ("committed", {"mem0_vector": True, "salience": 1}),
    "m-side": ("committed", {"mem0_vector": False, "salience": 1}),
    "m-gone": ("not_found", {}),
    "m-part": ("partial", {"mem0_vector": True}),
    "m-fail": ("failed", {}),
}
SCOPES = {"m-part": ("user_x", "bank_a")}


@pytest.fixture
def env(monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(utils, "SALIENCE_DB", str(tmp_path / "salience.db"))
    monkeypatch.setattr(utils, "DATA_DIR", str(data))
    monkeypatch.setattr(utils, "CONSOLIDATOR_LOCK", str(tmp_path / "consolidator.lock"))
    monkeypatch.setenv("AIDUMEM_ENV_FILE", str(tmp_path / "no-such.env"))
    for var in ("AIDUMEI_CONSOLIDATOR_EVICT", "AIDUMEI_CONFLICT_PENALTY_MODE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cons, "_auth_headers", lambda: {})
    monkeypatch.setattr(cons, "detect_and_crystallize_patterns", lambda: [])
    monkeypatch.setattr(memory_salience_facade, "verify_lessons_closed", lambda: {})
    monkeypatch.setattr(cons.random, "random", lambda: 0.99)  # no nightmare audit
    salience_db._ensure_db()
    _seed()
    return data


def _seed() -> None:
    conn = utils.get_salience_conn()
    now = time.time()
    old = now - 60 * 86400
    for mid in APPLY_TABLE:
        uid, bid = SCOPES.get(mid, ("default", "default"))
        conn.execute(
            "INSERT OR REPLACE INTO salience (memory_id, salience, last_access, access_count, created_at, "
            "lane, content_preview, user_id, bank_id) VALUES (?, 0.05, ?, 0, ?, 'general', ?, ?, ?)",
            (mid, old, old, f"old note {mid}", uid, bid))
    conn.execute(
        "INSERT OR REPLACE INTO salience (memory_id, salience, last_access, access_count, created_at, "
        "lane, content_preview) VALUES ('m-keep', 0.9, ?, 3, ?, 'general', 'fresh note')", (now, now))
    conn.commit()


def _row_exists(mid: str) -> bool:
    conn = utils.get_salience_conn()
    return conn.execute("SELECT 1 FROM salience WHERE memory_id=?", (mid,)).fetchone() is not None


def _summary_file(data) -> dict:
    return json.loads((data / "consolidator_last_run.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- pure classification
@pytest.mark.parametrize("code, body, error, expected", [
    (200, {"status": "committed"}, None, "deleted"),
    (200, {"status": "not_found"}, None, "already_gone"),
    (207, {"status": "partial"}, None, "partial"),
    (500, {"status": "failed"}, None, "failed"),
    (500, {"detail": "boom"}, None, "failed"),
    (401, {}, None, "failed"),
    (None, {}, "URLError: refused", "failed"),
    (200, {"status": "ok"}, None, "failed"),   # unrecognised == unconfirmed, never "deleted"
    (200, {}, None, "failed"),
])
def test_classify_delete_response(code, body, error, expected):
    assert cons.classify_delete_response(code, body, error) == expected


def test_old_predicate_counted_zero_for_every_real_status():
    """Negative control: the pre-f0.3 predicate reproduces the '0/N' production log."""
    old_predicate = [crud_body(name).get("status") == "ok" for name in CRUD_STATUS_CODE]
    assert sum(old_predicate) == 0
    assert cons.classify_delete_response(200, crud_body("committed")) == "deleted"


# ---------------------------------------------------------------- apply
def test_apply_accounts_by_real_status(env, monkeypatch, caplog):
    monkeypatch.setenv("AIDUMEI_CONSOLIDATOR_EVICT", "apply")
    with _FakeApi(APPLY_TABLE) as api, caplog.at_level(logging.INFO):
        monkeypatch.setattr(cons, "API_BASE", api.base)
        summary = cons.run_consolidation()

    assert summary["status"] == "ok" and summary["mode"] == "apply"
    assert summary["candidates"] == 5
    d = summary["deletion"]
    assert (d["deleted"], d["already_gone"], d["partial"], d["failed"]) == (2, 1, 1, 1)
    assert d["attempted"] == 5 and d["deleted_sidecar_only"] == 1
    assert d["stale_salience_rows_removed"] == 1
    assert summary["mismatch"] is True   # 5 candidates != 2 deleted + 1 already gone
    assert {f["memory_id"] for f in summary["failures"]} == {"m-part", "m-fail"}
    assert _summary_file(env)["deletion"] == d

    # the stale local row is removed ONLY for the confirmed not_found
    assert not _row_exists("m-gone")
    assert _row_exists("m-fail") and _row_exists("m-part") and _row_exists("m-keep")

    sent = {b["memory_id"]: b for b in api.deletes()}
    assert set(sent) == set(APPLY_TABLE), "the fresh row must never be sent to /delete"
    assert (sent["m-part"]["user_id"], sent["m-part"]["bank_id"]) == ("user_x", "bank_a")
    assert (sent["m-del"]["user_id"], sent["m-del"]["bank_id"]) == (cons.DEFAULT_USER_ID, "default")

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("apply" in m and "5" in m for m in warnings), "failures must be logged at WARNING"


def test_apply_all_confirmed_is_not_a_mismatch(env, monkeypatch, caplog):
    monkeypatch.setenv("AIDUMEI_CONSOLIDATOR_EVICT", "apply")
    table = {mid: ("committed", {"mem0_vector": True}) for mid in APPLY_TABLE}
    table["m-gone"] = ("not_found", {})
    with _FakeApi(table) as api:
        monkeypatch.setattr(cons, "API_BASE", api.base)
        summary = cons.run_consolidation()
    d = summary["deletion"]
    assert (d["deleted"], d["already_gone"], d["partial"], d["failed"]) == (4, 1, 0, 0)
    assert summary["mismatch"] is False and summary["failures"] == []


# ---------------------------------------------------------------- dry-run / off / invalid
def test_dry_run_is_the_default_and_deletes_nothing(env, monkeypatch, caplog):
    with _FakeApi(APPLY_TABLE) as api, caplog.at_level(logging.INFO):
        monkeypatch.setattr(cons, "API_BASE", api.base)
        summary = cons.run_consolidation()

    assert summary["mode"] == "dry-run" and summary["mode_error"] is None
    assert api.deletes() == [], "dry-run must not call /delete at all"
    assert summary["candidates"] == 5 and summary["mismatch"] is None
    assert all(v == 0 for v in summary["deletion"].values())
    listed = {c["memory_id"]: c for c in summary["candidate_list"]}
    assert set(listed) == set(APPLY_TABLE)
    assert listed["m-part"]["user_id"] == "user_x" and listed["m-part"]["bank_id"] == "bank_a"
    assert listed["m-del"]["salience"] < 0.2 and listed["m-del"]["idle_days"] > 30
    assert all(_row_exists(mid) for mid in APPLY_TABLE)
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert all(mid in logged for mid in APPLY_TABLE), "dry-run must list candidate ids in the log"
    # daily_metrics records what was really evicted (0), not the candidate count (5)
    conn = utils.get_salience_conn()
    assert conn.execute("SELECT evicted_count FROM daily_metrics").fetchone()[0] == 0


def test_off_mode_neither_deletes_nor_lists(env, monkeypatch):
    monkeypatch.setenv("AIDUMEI_CONSOLIDATOR_EVICT", "off")
    with _FakeApi(APPLY_TABLE) as api:
        monkeypatch.setattr(cons, "API_BASE", api.base)
        summary = cons.run_consolidation()
    assert summary["mode"] == "off" and api.deletes() == []
    assert summary["candidates"] == 5 and summary["candidate_list"] == []


@pytest.mark.parametrize("raw", ["yes", "true", "APPLY!", "dryrun"])
def test_invalid_mode_falls_back_to_dry_run_and_says_so(env, monkeypatch, caplog, raw):
    monkeypatch.setenv("AIDUMEI_CONSOLIDATOR_EVICT", raw)
    with _FakeApi(APPLY_TABLE) as api:
        monkeypatch.setattr(cons, "API_BASE", api.base)
        summary = cons.run_consolidation()
    assert summary["mode"] == "dry-run" and api.deletes() == []
    assert raw in (summary["mode_error"] or "")
    assert any("AIDUMEI_CONSOLIDATOR_EVICT" in r.getMessage() for r in caplog.records
               if r.levelno >= logging.WARNING)


def test_mode_parsing_is_case_and_space_tolerant_but_strict_on_words(monkeypatch, tmp_path):
    monkeypatch.setenv("AIDUMEM_ENV_FILE", str(tmp_path / "no-such.env"))
    for raw, expected in ((" Apply ", "apply"), ("OFF", "off"), ("dry-run", "dry-run"),
                          ("", "dry-run"), ("delete", "dry-run")):
        monkeypatch.setenv("AIDUMEI_CONSOLIDATOR_EVICT", raw)
        assert cons.evict_mode_status()["mode"] == expected, raw


def test_mode_is_read_from_env_file_when_cron_has_no_env(monkeypatch, tmp_path):
    """cron does not load .env: a switch that only reads os.environ would never take effect."""
    env_file = tmp_path / ".env"
    env_file.write_text("AIDUMEI_CONSOLIDATOR_EVICT=apply\n", encoding="utf-8")
    monkeypatch.setenv("AIDUMEM_ENV_FILE", str(env_file))
    monkeypatch.delenv("AIDUMEI_CONSOLIDATOR_EVICT", raising=False)
    assert cons.evict_mode_status()["mode"] == "apply"


# ---------------------------------------------------------------- every run writes a summary
def test_unreachable_api_writes_an_aborted_summary(env, monkeypatch):
    with socket.socket() as s:   # grab a free port, then close it: nothing listens there
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(cons, "API_BASE", f"http://127.0.0.1:{port}")
    summary = cons.run_consolidation()
    on_disk = _summary_file(env)
    assert summary["status"] == on_disk["status"] == "aborted"
    assert on_disk["abort_reason"] == "api_unreachable"
    assert on_disk["duration_s"] >= 0 and on_disk["timestamp"] > 0


def test_crash_mid_run_still_writes_an_error_summary(env, monkeypatch):
    def boom():
        raise RuntimeError("decay exploded")
    monkeypatch.setattr(cons, "decay_all", boom)
    with _FakeApi(APPLY_TABLE) as api:
        monkeypatch.setattr(cons, "API_BASE", api.base)
        with pytest.raises(RuntimeError):
            cons.run_consolidation()
    on_disk = _summary_file(env)
    assert on_disk["status"] == "error" and "decay exploded" in on_disk["error"]


def test_summary_carries_conflict_stats(env, monkeypatch):
    with _FakeApi(APPLY_TABLE) as api:
        monkeypatch.setattr(cons, "API_BASE", api.base)
        summary = cons.run_consolidation()
    c = _summary_file(env)["conflicts"]
    assert c["mode"] == "warn" and c["pairs_applied"] == 0
    for key in ("pairs_found", "pairs_compared", "truncated", "max_pairs_per_lane"):
        assert key in c
    assert summary["duration_s"] >= 0


# ---------------------------------------------------------------- double <-> production alignment
def test_fake_delete_double_matches_the_real_crud_route(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import ducky.hot.crud as crud

    app = FastAPI()
    crud.register_crud_routes(app)
    client = TestClient(app)
    expected_outcome = {"committed": "deleted", "not_found": "already_gone",
                        "partial": "partial", "failed": "failed"}
    for name, code in CRUD_STATUS_CODE.items():
        monkeypatch.setattr(crud, "cascade_delete_memory",
                            lambda *a, _n=name, **k: crud_body(_n, mem0_vector=True))
        resp = client.post("/delete", json={"memory_id": "x", "user_id": "default"})
        assert (resp.status_code, resp.json()["status"]) == (code, name)
        assert cons.classify_delete_response(resp.status_code, resp.json()) == expected_outcome[name]
