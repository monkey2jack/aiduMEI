"""f0.4+ WP2: the essence line fires per turn without repeating itself.

Production evidence (2026-10-14): the host calls `on_session_end` on EVERY
turn, but the hook used to run a synchronous distill-and-write every time, so
one session was re-summarised 2-9 times (79 sessions -> 280 summaries).  Two
independent defects sit in that sentence:

* **the hook blocked the host** for a whole LLM round per turn, against a
  30-second callback budget; and
* **the server summarised the same sources again**, because nothing said
  "this generation already covers these".

So this file pins both halves, plus the cleanup for the generations already
written.  A guard that only checks the hook returns quickly would pass while
the duplicates keep piling up, and a guard that only checks the throttle would
pass while the host waits.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_HOOK = _REPO / "integrations/aidumem-distill.sh"
# The hooks are bash scripts (shebang). Parametrising over whichever shells
# happen to be installed made the collected case count machine-dependent
# (the target host has no zsh), which the case-count guard rightly rejects.
_SHELLS = ["bash"]


# ---------------------------------------------------------------- stub server

class _Stub:
    """Collects (path, body) and answers the two endpoints the hook calls."""

    def __init__(self, summary: str = "One session insight", delay: float = 0.0):
        self.seen: list[tuple[str, dict]] = []
        self.delay = delay
        self.summary = summary
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.seen.append((self.path, body))
                if outer.delay:
                    time.sleep(outer.delay)
                if self.path.startswith("/session/distill"):
                    response = {"status": "ok", "summary": outer.summary,
                                "mode": "llm", "source_count": 5,
                                "user_id": "alice", "bank_id": "work",
                                "metadata": {"kind": "session_distill"}}
                else:
                    response = {"status": "ok"}
                payload = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *args):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.srv.server_port}"

    def distill_paths(self) -> list[str]:
        return [p for p, _ in self.seen if p.startswith("/session/distill")]

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.thread.join(timeout=5)

    def wait_for(self, predicate, timeout: float = 15.0) -> bool:
        """Poll the recorded requests (the background job is another process)."""
        end = time.time() + timeout
        while time.time() < end:
            if predicate():
                return True
            time.sleep(0.05)
        return predicate()


def _env(tmp_path: Path, base: str, **extra) -> dict:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(tmp_path / "home"),
        "TMPDIR": str(tmp_path / "tmp"),
        "AIDUMEM_URL": base,
        "AIDUMEM_USER_ID": "alice", "AIDUMEI_BANK_ID": "work",
        "AIDUMEM_API_TOKEN": "",
        "AIDUMEI_DISTILL_TIMEOUT": "20",
        "AIDUMEM_DATA_DIR": str(tmp_path / "data"),
        "AIDUMEM_LOG_DIR": str(tmp_path / "logs"),
        # Never pick up a real ~/.aidumem/.env from the machine running tests.
        "AIDUMEM_ENV_FILE": str(tmp_path / "absent.env"),
        "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
    }
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tmp").mkdir(parents=True, exist_ok=True)
    env.update(extra)
    return env


def _run(shell: str, argv: list, stdin: str, env: dict, timeout: float = 30):
    return subprocess.run([shell] + argv, input=stdin, text=True,
                          capture_output=True, env=env, timeout=timeout)


_SLOW = 3.0


@pytest.mark.parametrize("shell", _SHELLS)
def test_hook_returns_before_the_distill_finishes(shell, tmp_path):
    """宿主每轮都触发，钩子就不许等 LLM —— 但后台那件事必须真的发生。

    Both halves matter: return-fast alone is satisfied by doing nothing, and
    the assertion below therefore also waits for the background write.
    """
    stub = _Stub(delay=_SLOW)
    try:
        started = time.time()
        proc = _run(shell, [str(_HOOK)], json.dumps({"session_id": "bg-session"}),
                    _env(tmp_path, stub.base, AIDUMEM_HOOK_QUIET="1"))
        elapsed = time.time() - started
    finally:
        pass
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {}, proc.stdout
    assert elapsed < _SLOW / 2, (
        f"钩子等了 {elapsed:.2f}s（服务端人为延迟 {_SLOW}s）：后台化没生效，"
        "宿主每轮都被拖住")
    try:
        assert stub.wait_for(lambda: stub.distill_paths()), \
            "后台作业从未发出提炼请求：快是快了，事没做"
    finally:
        stub.close()


@pytest.mark.parametrize("shell", _SHELLS)
def test_foreground_mode_waits_and_therefore_measures_the_difference(shell, tmp_path):
    """Negative control for the timing assertion above.

    Without this, `elapsed < 1.5s` could be passing because the hook never
    calls the server at all — the control proves the same measurement does
    notice a synchronous run.
    """
    stub = _Stub(delay=_SLOW)
    try:
        started = time.time()
        proc = _run(shell, [str(_HOOK)], json.dumps({"session_id": "fg-session"}),
                    _env(tmp_path, stub.base, AIDUMEI_DISTILL_FOREGROUND="1",
                         AIDUMEM_HOOK_QUIET="1"))
        elapsed = time.time() - started
        served = stub.distill_paths()
    finally:
        stub.close()
    assert proc.returncode == 0, proc.stderr
    assert elapsed >= _SLOW, f"前台模式没有等请求回来：{elapsed:.2f}s"
    assert served, "前台模式没有发出提炼请求"


def test_background_job_writes_a_durable_timestamped_log(tmp_path):
    """stderr 在宿主成功路径上留不下来，诊断必须落到文件里。"""
    stub = _Stub()
    try:
        proc = _run("bash", [str(_HOOK)], json.dumps({"session_id": "log-session"}),
                    _env(tmp_path, stub.base,   # 故意设 QUIET，日志仍须写满
                         AIDUMEM_HOOK_QUIET="1"))
        assert proc.returncode == 0, proc.stderr
        log = tmp_path / "logs" / "aidumem-distill.log"
        assert stub.wait_for(lambda: log.exists() and log.read_text(
            encoding="utf-8").strip()), "后台作业没有留下任何诊断"
        line = log.read_text(encoding="utf-8").strip().splitlines()[-1]
    finally:
        stub.close()
    import re
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ", line), (
        f"日志行没有时间戳，事后无法与宿主事件对齐：{line!r}")
    assert "ok" in line, f"成功路径没有写回执：{line!r}"


def test_background_mode_does_not_keep_the_host_stdin_pipe(tmp_path):
    """后台子进程若攥着宿主给的管道，宿主会一直等到它结束才拿到 EOF。"""
    stub = _Stub(delay=_SLOW)
    try:
        proc = subprocess.run(
            ["bash", str(_HOOK)], input=json.dumps({"session_id": "pipe-session"}),
            text=True, capture_output=True, timeout=_SLOW * 3,
            env=_env(tmp_path, stub.base))
    finally:
        stub.close()
    # subprocess.run only returns once our write end is closed AND our read
    # ends hit EOF, so a leaked pipe shows up as the timeout above.
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {}


# ------------------------------------------------------------- event targeting

_RESET_PAYLOAD = {"hook_event_name": "on_session_reset", "session_id": "new-session",
                  "extra": {"old_session_id": "ended-session"}}


@pytest.mark.parametrize("shell", _SHELLS)
@pytest.mark.parametrize("payload,expect_session,expect_force", [
    ({"session_id": "turn-session"}, "turn-session", False),
    (json.dumps({"hook_event_name": "on_session_end", "session_id": "turn-session"}),
     "turn-session", False),
    (json.dumps({"hook_event_name": "on_session_finalize", "session_id": "fin-session"}),
     "fin-session", True),
    (json.dumps(_RESET_PAYLOAD), "ended-session", True),
])
def test_event_selects_the_session_and_whether_to_force(
        shell, tmp_path, payload, expect_session, expect_force):
    stub = _Stub()
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    try:
        proc = _run(shell, [str(_HOOK)], stdin,
                    _env(tmp_path, stub.base, AIDUMEI_DISTILL_FOREGROUND="1",
                         AIDUMEM_HOOK_QUIET="1"))
        seen = list(stub.seen)
    finally:
        stub.close()
    assert proc.returncode == 0, proc.stderr
    paths = [p for p, _ in seen if p.startswith("/session/distill")]
    assert paths, f"没有发出提炼请求：{proc.stderr[:300]}"
    assert f"session_id={expect_session}" in paths[-1], (
        f"圈错了会话：{paths[-1]!r}（reset 的 session_id 是**新**会话，"
        f"被结束的那个在 extra.old_session_id）")
    forced = "force=true" in paths[-1]
    assert forced is expect_force, f"force 口径不对：{paths[-1]!r}"


@pytest.mark.parametrize("case", [
    {"hook_event_name": "on_session_reset", "session_id": "new-session"},  # 没有 old
    {"session_id": ""},
    {},
])
def test_no_target_session_means_no_request_at_all(tmp_path, case):
    """圈不出这一程就别猜：宁可没有精华，也不许把别人的会话提炼一遍。"""
    stub = _Stub()
    try:
        proc = _run("bash", [str(_HOOK)], json.dumps(case),
                    _env(tmp_path, stub.base, AIDUMEM_HOOK_QUIET="1"))
        seen = list(stub.seen)
    finally:
        stub.close()
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {}
    assert seen == [], f"没有会话标识却发了请求：{seen}"


# ------------------------------------------------------------------ job locking

def test_concurrent_background_runs_do_not_double_distill(tmp_path):
    """同一会话两个后台作业同时被拉起时，只有一个真跑（服务端幂等键是第二道）。

    Driven through the real entry point twice, so both background jobs write
    their diagnostics where production does (the durable log). The lock needs
    util-linux ``flock`` (present on the Linux hosts we deploy to). Where it is
    absent the hook degrades to "no lock": both jobs run and the server's
    idempotency key is the only guard. Both branches are asserted, so this case
    never silently skips.
    """
    has_flock = bool(shutil.which("flock"))
    stub = _Stub(delay=1.5)
    env = _env(tmp_path, stub.base)
    log = tmp_path / "logs" / "aidumem-distill.log"
    try:
        for _ in range(2):
            proc = _run("bash", [str(_HOOK)], json.dumps({"session_id": "locked-session"}), env)
            assert proc.returncode == 0 and json.loads(proc.stdout) == {}, proc.stderr
        assert stub.wait_for(lambda: len(stub.distill_paths()) >= 1, timeout=10)
        expected = 1 if has_flock else 2
        assert stub.wait_for(lambda: log.exists() and log.read_text(encoding="utf-8").count("\n") >= 2,
                             timeout=15), "两个后台作业没有都留下诊断"
        time.sleep(0.5)
        served = len(stub.distill_paths())
        text = log.read_text(encoding="utf-8")
    finally:
        stub.close()
    if has_flock:
        assert served == expected, f"同一会话跑了 {served} 次提炼（锁没起作用）"
        assert "busy" in text, f"被锁挡下的那次没有留下诊断：{text!r}"
    else:
        assert served == expected, "没有 flock 时两次都该照常跑（降级是「不加锁」，不是「不干活」）"


# --------------------------------------------------------- server: cadence

_Q = ("CREATE TABLE IF NOT EXISTS memory_epistemic ("
      " memory_ref TEXT, origin_session_id TEXT, origin_turn INTEGER,"
      " created_at TEXT, user_id TEXT, bank_id TEXT, origin_agent TEXT)")
_T = ("CREATE TABLE IF NOT EXISTS verbatim_turns ("
      " id INTEGER PRIMARY KEY, user_id TEXT, bank_id TEXT, session_id TEXT,"
      " role TEXT, content TEXT, recorded_at TEXT, created_at TEXT)")


@pytest.fixture
def session_db(tmp_path, monkeypatch):
    import sqlite3
    import ducky.utils as utils
    monkeypatch.setattr(utils, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(utils, "FACTS_DB", str(tmp_path / "facts.db"))
    con = sqlite3.connect(tmp_path / "facts.db")
    con.executescript(_Q + ";" + _T)
    con.commit()
    con.close()
    return tmp_path


def _turn(db: Path, tid: int, text: str, *, session: str = "s1",
          user: str = "alice", bank: str = "work"):
    import sqlite3
    con = sqlite3.connect(db / "facts.db")
    con.execute("INSERT INTO verbatim_turns VALUES (?,?,?,?,?,?,?,?)",
                (tid, user, bank, session, "user", text,
                 "2026-10-14T00:00:00Z", "2026-10-14T00:00:00Z"))
    con.commit()
    con.close()


def _age_ledger(db: Path, seconds: float):
    """Backdate the ledger: production timestamps are written by /add."""
    import sqlite3
    from datetime import datetime, timedelta, timezone
    stamp = (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()
    con = sqlite3.connect(db / "facts.db")
    con.execute("UPDATE distill_sources SET created_at=?", (stamp,))
    con.commit()
    con.close()


@pytest.fixture
def no_llm(monkeypatch):
    import ducky.llm_client as llm
    monkeypatch.setattr(llm, "call_llm", lambda *a, **kw: None)


def _record(out):
    """Emulate what POST /add does with a produced summary."""
    import ducky.session_distill as distill
    assert distill.record_summary_sources(
        out["user_id"], out["bank_id"], out["metadata"], out["summary"]) > 0


def test_cadence_for_every_turn_without_repeating_itself(session_db, no_llm):
    """按「新增来源」出代，且每一代只覆盖它自己那段 —— 不是层层套娃。"""
    import ducky.session_distill as distill
    db = session_db
    for i in range(1, 5):
        _turn(db, i, f"turn {i}")

    first = distill.distill_session("s1", user_id="alice", bank_id="work")
    assert first["status"] == "ok" and first["source_refs"] == [f"verbatim:{i}" for i in range(1, 5)]
    _record(first)

    same = distill.distill_session("s1", user_id="alice", bank_id="work")
    assert (same["status"], same["reason"]) == ("skipped", "unchanged")
    assert same["unseen_sources"] == 0

    _turn(db, 5, "turn 5")
    _turn(db, 6, "turn 6")
    two_more = distill.distill_session("s1", user_id="alice", bank_id="work")
    assert (two_more["status"], two_more["reason"]) == ("skipped", "throttled"), two_more
    assert two_more["unseen_sources"] == 2 and two_more["min_new_sources"] == 6

    # 真实会话边界（finalize/reset）：有一条没见过的来源就出新一代。
    forced = distill.distill_session("s1", user_id="alice", bank_id="work", force=True)
    assert forced["status"] == "ok"
    assert forced["metadata"]["distill_source_count"] == 3, forced["source_refs"]
    assert forced["source_refs"] == ["verbatim:4", "verbatim:5", "verbatim:6"], (
        "新代只该覆盖新那段（不足 MIN_MEMORIES 时最多回带一条上文）")
    _record(forced)

    # 每轮触发：攒够阈值自己出代，不需要 force。
    for i in range(7, 13):
        _turn(db, i, f"turn {i}")
    auto = distill.distill_session("s1", user_id="alice", bank_id="work")
    assert auto["status"] == "ok", auto
    assert auto["source_refs"] == [f"verbatim:{i}" for i in range(7, 13)], auto["source_refs"]
    _record(auto)

    # 长了的老会话：至少一条新来源 + 上一代够老 -> 出一代「近两小时」。
    _turn(db, 13, "turn 13")
    assert distill.distill_session("s1", user_id="alice", bank_id="work")["reason"] == "throttled"
    _age_ledger(db, 3 * 3600)
    aged = distill.distill_session("s1", user_id="alice", bank_id="work")
    assert aged["status"] == "ok" and aged["source_refs"] == ["verbatim:11", "verbatim:12",
                                                             "verbatim:13"], aged["source_refs"]
    _record(aged)

    # 无来源变化时，force 也不许把同一段再提炼一遍。
    assert distill.distill_session("s1", user_id="alice", bank_id="work",
                                   force=True)["reason"] == "unchanged"


def test_missing_ledger_is_not_read_as_unchanged(session_db, no_llm, monkeypatch):
    """老部署没有 distill_sources 表：「没有前一代」不等于「没变」。

    这是负向对照：把「读不到」当成「已覆盖」，第一次提炼就会被节流掉，
    而现象是「精华线整个不工作」——最难查的那种安静。
    """
    import sqlite3
    import ducky.session_distill as distill
    for i in range(1, 5):
        _turn(session_db, i, f"turn {i}")
    con = sqlite3.connect(session_db / "facts.db")
    con.execute("DROP TABLE IF EXISTS distill_sources")
    con.commit()
    con.close()
    out = distill.distill_session("s1", user_id="alice", bank_id="work")
    assert out["status"] == "ok", out


# ------------------------------------------------------- server: prune tool

def _summary(mid, session, refs, created, *, truncated=False):
    md = {"kind": "session_distill", "lane": "distill",
          "_origin_agent": "session-distill", "_origin_session_id": session,
          "distill_source_refs": list(refs)}
    if truncated:
        md["distill_source_refs_truncated"] = True
    return {"id": mid, "created_at": created, "metadata": md}


def test_only_fully_covered_generations_are_superseded():
    import ducky.session_distill as distill
    items = [
        _summary("g1", "s1", ["a", "b"], "2026-10-14T01:00:00+00:00"),
        _summary("g2", "s1", ["a", "b", "c"], "2026-10-14T02:00:00+00:00"),
        _summary("g3", "s1", ["a", "c", "d"], "2026-10-14T03:00:00+00:00"),
        _summary("g4", "s1", ["x", "y"], "2026-10-14T04:00:00+00:00"),
        _summary("g5", "s1", ["x", "y"], "2026-10-14T05:00:00+00:00"),
        _summary("g6", "s1", ["a"], "2026-10-14T06:00:00+00:00", truncated=True),
        _summary("h1", "s2", ["p"], "2026-10-14T07:00:00+00:00"),
        _summary("h2", "s2", ["p", "q"], "2026-10-14T08:00:00+00:00"),
        _summary("n1", "s3", ["k"], "2026-10-14T09:00:00+00:00"),
    ]
    doomed = {d["id"]: d["superseded_by"] for d in distill.superseded_summaries(items)}
    assert doomed == {"g1": "g2", "g4": "g5", "h1": "h2"}, doomed
    # g3 partially overlaps (a long session's window slid forward): 保留。
    # g6 的来源清单被截断：证明不了覆盖率，一律保留。
    # n1 是这一会话唯一一代：保留。


def test_partial_ledger_view_is_never_treated_as_nothing_to_do(tmp_path, monkeypatch):
    """枚举不完整 -> 拒绝，不许当成「清理完成」（与 delete_all 同一纪律）。"""
    import ducky.session_distill as distill
    calls = []
    monkeypatch.setattr("ducky.wal_engine._scoped_vector_items",
                        lambda mem, scope: ([], False))
    monkeypatch.setattr("ducky.wal_engine.cascade_delete_memory",
                        lambda *a, **kw: calls.append(a) or {"status": "committed"})
    out = distill.prune_superseded_summaries("alice", "work", apply=True, mem=object())
    assert out["status"] == "refused" and out["reason"] == "vector_enumeration_incomplete"
    assert calls == []


def test_dry_run_lists_exactly_what_apply_deletes(tmp_path, monkeypatch):
    import ducky.session_distill as distill
    items = [
        _summary("g1", "s1", ["a", "b"], "2026-10-14T01:00:00+00:00"),
        _summary("g2", "s1", ["a", "b", "c"], "2026-10-14T02:00:00+00:00"),
        _summary("g3", "s1", ["a", "c", "d"], "2026-10-14T03:00:00+00:00"),
    ]
    monkeypatch.setattr("ducky.wal_engine._scoped_vector_items",
                        lambda mem, scope: (items, True))
    deleted = []

    def _cascade(mid, user_id="", bank_id=""):
        deleted.append(mid)
        return {"status": "committed"}

    monkeypatch.setattr("ducky.wal_engine.cascade_delete_memory", _cascade)

    dry = distill.prune_superseded_summaries("alice", "work", mem=object())
    assert dry["status"] == "dry_run" and dry["summaries_scanned"] == 3
    listed = [c["id"] for c in dry["candidates"]]
    assert listed == ["g1"], dry
    assert deleted == [], "dry-run 不许删任何东西"

    applied = distill.prune_superseded_summaries("alice", "work", apply=True, mem=object())
    assert applied["status"] == "ok"
    assert [d["id"] for d in applied["deleted"]] == listed, (
        "实际删除集与 dry-run 清单不一致 —— 验收基准被偷换了")
    assert deleted == listed and applied["failed"] == []


def test_apply_reports_layer_failures_instead_of_calling_it_done(monkeypatch):
    import ducky.session_distill as distill
    items = [
        _summary("g1", "s1", ["a"], "2026-10-14T01:00:00+00:00"),
        _summary("g2", "s1", ["a", "b", "c"], "2026-10-14T02:00:00+00:00"),
    ]
    monkeypatch.setattr("ducky.wal_engine._scoped_vector_items",
                        lambda mem, scope: (items, True))
    monkeypatch.setattr("ducky.wal_engine.cascade_delete_memory",
                        lambda *a, **kw: {"status": "repair_required",
                                          "failed_layers": ["vector"]})
    out = distill.prune_superseded_summaries("alice", "work", apply=True, mem=object())
    assert out["status"] == "partial" and out["deleted"] == []
    assert out["failed"][0]["id"] == "g1"


# ------------------------------------------------- WP3: unattended sessions

_INGEST = _REPO / "integrations/aidumem-ingest.sh"


def _ingest_payload(platform):
    extra = {"user_message": "Please remember the rollout decision for Friday",
             "assistant_response": "Noted: rollout on Friday.", "turn_id": 3,
             "conversation_history": []}
    if platform is not None:
        extra["platform"] = platform
    return json.dumps({"hook_event_name": "post_llm_call", "session_id": "plat-session",
                       "extra": extra})


@pytest.mark.parametrize("platform,skip_env,expect_write", [
    ("cron", None, False),
    ("subagent", None, False),
    ("telegram", None, True),          # 负向对照：普通会话照写
    (None, None, True),                # 没有 platform 字段：不猜，照写
    ("cron", "", True),                # 显式置空 = 关掉这条策略
    ("telegram", "telegram", False),   # 策略可配，不是写死的 cron
])
def test_unattended_sessions_do_not_write_memories(tmp_path, platform, skip_env, expect_write):
    """定时任务自发自收的独白不许混进召回池（与宿主自己的 cron/subagent 分档同源）。"""
    stub = _Stub()
    extra = {"AIDUMEM_MIN_HISTORY": "0"}
    if skip_env is not None:
        extra["AIDUMEI_SKIP_PLATFORMS"] = skip_env
    try:
        proc = _run("bash", [str(_INGEST)], _ingest_payload(platform),
                    _env(tmp_path, stub.base, **extra))
        wrote = stub.wait_for(lambda: any(p == "/add" for p, _ in stub.seen), timeout=3)
    finally:
        stub.close()
    assert proc.returncode == 0, proc.stderr
    assert wrote is expect_write, (platform, skip_env, proc.stderr[-300:])
    if not expect_write:
        assert "定时/子代理" in proc.stderr, "跳过必须留痕（安静≠失声）"


@pytest.mark.parametrize("platform,expect_request", [
    ("cron", False), ("subagent", False), ("discord", True), (None, True)])
def test_unattended_sessions_are_not_distilled_either(tmp_path, platform, expect_request):
    """两条线口径一致：只挡写不挡提炼，会长出「没有原文来源的精华」。"""
    stub = _Stub()
    payload = {"hook_event_name": "on_session_end", "session_id": "plat-session",
               "extra": {} if platform is None else {"platform": platform}}
    try:
        proc = _run("bash", [str(_HOOK)], json.dumps(payload),
                    _env(tmp_path, stub.base, AIDUMEI_DISTILL_FOREGROUND="1"))
        seen = list(stub.seen)
    finally:
        stub.close()
    assert proc.returncode == 0, proc.stderr
    assert bool(seen) is expect_request, (platform, seen, proc.stderr[-300:])


# ------------------------------------------- WP3: deployment checker scoping

def test_checker_attributes_commands_to_the_event_they_sit_under(tmp_path):
    """同级的未知事件不许把它的 command 记到上一个事件名下。

    f0.4+ 之前解析器只在**顶格**键处退出事件作用域，于是 hooks 下任何一个
    不认识的同级事件（例如 v0.22 的 on_session_finalize）都会被算成上一个
    事件的又一条命令 —— 换个脚本挂上去，就会被误判成「萃取线漂移」。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "_chk_f04p", _REPO / "scripts" / "check_hook_deployment.py")
    chk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(chk)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "hooks:\n"
        "  on_session_end:\n"
        "    - command: \"/x/aidumem-distill.sh\"\n"
        "  on_tool_call:\n"
        "    - command: \"/x/unrelated-tool-hook.sh\"\n"
        "  on_session_reset:\n"
        "    - command: \"/x/aidumem-distill.sh --flag\"\n"
        "hooks_auto_accept: true\n", encoding="utf-8")
    parsed = chk.parse_hook_commands(str(cfg))
    assert parsed["on_session_end"] == ["/x/aidumem-distill.sh"], parsed
    assert parsed["on_session_reset"] == ["/x/aidumem-distill.sh"], parsed
    assert all("unrelated" not in p for paths in parsed.values() for p in paths), parsed


# --------------------------------------- WP3: write-line failures are durable

class _StatusStub(_Stub):
    """Answers /add with a fixed status (409 = the 2026-10-14 scope lock)."""

    def __init__(self, add_status: int):
        self.add_status = add_status
        super().__init__()
        outer = self
        handler = self.srv.RequestHandlerClass
        original = handler.do_POST

        def do_post(inner):  # noqa: N802
            if inner.path.startswith("/add") and outer.add_status != 200:
                n = int(inner.headers.get("Content-Length") or 0)
                outer.seen.append((inner.path, json.loads(inner.rfile.read(n) or b"{}")))
                inner.send_error(outer.add_status, "synthetic")
                return
            original(inner)

        handler.do_POST = do_post


@pytest.mark.parametrize("status,expect_logged", [(409, True), (503, True), (200, False)])
def test_ingest_failures_leave_a_durable_timestamped_line(tmp_path, status, expect_logged):
    stub = _StatusStub(status)
    try:
        proc = _run("bash", [str(_INGEST)], _ingest_payload("telegram"),
                    _env(tmp_path, stub.base, AIDUMEM_MIN_HISTORY="0",
                         AIDUMEM_HOOK_QUIET="1"))   # 安静模式也必须留下持久记录
        sent = list(stub.seen)
    finally:
        stub.close()
    assert proc.returncode == 0 and json.loads(proc.stdout) == {}, proc.stderr
    assert any(p == "/add" for p, _ in sent), "写线没有发出请求，用例失去意义"
    log = tmp_path / "logs" / "aidumem-ingest.log"
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    if expect_logged:
        assert lines and f"status={status}" in lines[-1], lines
        import re
        assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \[aidumem-ingest\]", lines[-1]), lines
    else:
        assert lines == [], f"正常轮次不该写失败日志：{lines}"
