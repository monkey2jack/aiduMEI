"""f0.4++ cc 载体适配回归 —— 把本轮每个缺陷写成断言锁死。

覆盖（对应计划任务书 WP 编号）：
  WP1  journal 遗留收养工具：fail-closed / 收养 / 幂等 / 活体守卫（负向对照全带）
  WP4  SessionEnd 适配层：payload 口径转换（cc- 前缀 + on_session_end），
       与写线 _origin_session_id 逐字一致 —— 萃取线圈定来源的命根子
  WP6  Stop 写线：含三引号/反斜杠的 transcript 不再破坏语法（原 r'''$INPUT'''
       拼接必破），且 /add 载荷带 _origin_session_id
  WP7  注入读线：探活超时不得回到 0.3s（高峰期写向量误熔断实证）

测试纪律（对齐 SOP）：
  - 每条正向断言配负向对照：旧实现的破法必须真的会被本断言抓住；
  - WP1 只在 tmp_path 隔离数据目录跑，绝不触碰任何生产 data/；
  - hook 是 shell 真源（integrations/claude-code/），用子进程真跑，
    不 mock bash —— 「测试只测被集成方」是 check_ingest_wiring 头注释里
    写明的历史教训；
  - 身份一律经环境变量显式传入（AIDUMEM_USER_ID 等），真源的 .env 兜底链
    因此不会触达 —— 测试与部署机的 .env 完全隔离。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


_REPO = Path(__file__).resolve().parents[1]
_CC_HOOKS = _REPO / "integrations" / "claude-code"
_PY = str(_REPO / "venv" / "bin" / "python")
if not os.path.exists(_PY):
    _PY = sys.executable

# 测试租户：绝不用生产租户，破坏性断言全部圈在这里
_TEST_UID = "test-cc-hook"
_TEST_BID = "default"


# ═══════════════════════════ 共用工具 ═══════════════════════════

def _ok_json(handler, obj):
    payload = json.dumps(obj, ensure_ascii=False).encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


@contextmanager
def _capture_server(route):
    """起一个本地 mock 服务。route: path 前缀 → 响应 dict 的映射函数。
    yield (port, calls)，calls 收集 (path, body_bytes)。"""
    calls: list = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            calls.append((self.path, body))
            _ok_json(self, route(self.path))

        def do_GET(self):  # noqa: N802
            calls.append((self.path, b""))
            if self.path.startswith("/health"):
                _ok_json(self, {"status": "ok"})
            else:
                self.send_error(404)

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv.server_port, calls
    finally:
        srv.shutdown()
        t.join(timeout=5)
        srv.server_close()


def _hook_env(port: int, tmp_path: Path, **extra) -> dict:
    """三线同源的测试环境：身份/凭据/地址全部显式注入，与部署机 .env 隔离。"""
    env = {
        "AIDUMEM_URL": f"http://127.0.0.1:{port}",
        "AIDUMEM_API_TOKEN": "test-token",
        "AIDUMEM_USER_ID": _TEST_UID,
        "AIDUMEI_BANK_ID": _TEST_BID,
        "AIDUMEM_ENV_FILE": str(tmp_path / "nonexistent.env"),
        "AIDUMEM_LOG_DIR": str(tmp_path / "logs"),
        "AIDUMEI_INSTALL_DIR": str(_REPO),
    }
    env.update(extra)
    return env


def _run_hook(script: Path, stdin_payload: dict, env_extra: dict,
              timeout: int = 25) -> subprocess.CompletedProcess:
    env = {**os.environ, **env_extra}
    return subprocess.run(
        ["bash", str(script)],
        input=json.dumps(stdin_payload).encode(),
        capture_output=True, timeout=timeout, env=env,
    )


def _make_transcript(tmp_path: Path, user_text: str, asst_text: str) -> Path:
    tp = tmp_path / "transcript.jsonl"
    lines = [
        json.dumps({"type": "user", "message": {"role": "user", "content": user_text}},
                   ensure_ascii=False),
        json.dumps({"type": "assistant",
                    "message": {"role": "assistant", "content": asst_text}},
                   ensure_ascii=False),
    ]
    tp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tp


def _wait_for(calls, pred, tries=50, interval=0.1):
    for _ in range(tries):
        if any(pred(p) for p, _ in calls):
            return True
        time.sleep(interval)
    return False


# ═══════════════════ WP1 · journal 收养工具（隔离数据目录）═══════════════════

def _build_legacy_journal(data_dir: Path) -> Path:
    """构造一个「有库无 identity」的遗留 journal —— 跨宿主迁移场景的最小复现。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    p = data_dir / "mutation_journal.sqlite3"
    conn = sqlite3.connect(p)
    conn.execute("""CREATE TABLE mutations (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL,
        user_id TEXT NOT NULL, bank_id TEXT NOT NULL,
        target_id TEXT NOT NULL DEFAULT '',
        state TEXT NOT NULL CHECK(state IN
          ('queued','started','acknowledged','repair_required','not_applied','forgotten')),
        input_json TEXT, result_json TEXT, target_ids_json TEXT NOT NULL DEFAULT '[]',
        request_key TEXT NOT NULL DEFAULT '', fingerprint TEXT NOT NULL DEFAULT '',
        reason TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL
    )""")
    conn.execute("CREATE INDEX mutations_scope ON mutations(user_id,bank_id,state)")
    conn.execute("CREATE INDEX mutations_key ON mutations(user_id,bank_id,request_key)")
    conn.execute("PRAGMA user_version=1")
    conn.execute(
        "INSERT INTO mutations VALUES ('legacy1','add',?,'default','','acknowledged',"
        "'{}','{}','[]','','','',1791532630.0,1791532630.0)", (_TEST_UID,))
    conn.commit()
    conn.close()
    return p


def _journal_env(data_dir: Path, dead_port: int) -> dict:
    return {
        "AIDUMEM_DATA_DIR": str(data_dir),
        "AIDUMEM_LOG_DIR": str(data_dir.parent / "logs"),
        # 收养工具读 AIDUMEI_URL（f0.4++ 统一新前缀；旧 AIDUMEM_URL 会撞
        # FROZEN_LEGACY_AIDUMEM 冻结守卫）——指向必然无服务的端口，
        # 让活体守卫放行，才能测收养本身。
        "AIDUMEI_URL": f"http://127.0.0.1:{dead_port}",
    }


def test_wp1_legacy_journal_fails_closed_before_adoption(tmp_path):
    """负向对照（改造前实锤）：未收养的遗留库必须让 startup_recover 抛
    JournalEvidenceError —— 2026-10-16 09:39 生产连续 3 次启动失败就是这个。
    收养工具的存在不许软化 fail-closed 本身。"""
    data = tmp_path / "data"
    _build_legacy_journal(data)
    env = _journal_env(data, 59998)
    r = subprocess.run(
        [_PY, "-c",
         "from ducky.mutation_journal import startup_recover; startup_recover()"],
        capture_output=True, env={**os.environ, **env}, timeout=30, cwd=_REPO)
    assert r.returncode != 0
    assert "journal_legacy_adoption_required" in r.stderr.decode(errors="replace")


def test_wp1_adoption_tool_enrolls_legacy_journal(tmp_path):
    """正向：收养工具对遗留库建立 identity 配对，历史记录原样保留，
    收养后 startup_recover 成功。"""
    data = tmp_path / "data"
    jpath = _build_legacy_journal(data)
    env = _journal_env(data, 59998)
    r = subprocess.run(
        [_PY, str(_REPO / "scripts" / "adopt_mutation_journal.py"), "--json"],
        capture_output=True, env={**os.environ, **env}, timeout=60, cwd=_REPO)
    assert r.returncode == 0, r.stderr.decode(errors="replace")
    receipt = json.loads(r.stdout.decode())
    assert receipt["status"] == "adopted"
    assert receipt["mutations_count"] == 1
    assert receipt["post_adoption_health"] == "ok"
    # identity 配对：外部标记与库内表都存在且一致
    marker = json.loads((data / "mutation_journal.identity.json").read_text())
    assert marker["format"] == 1 and len(marker["identity"]) == 32
    conn = sqlite3.connect(jpath)
    rows = conn.execute("SELECT identity FROM journal_identity").fetchall()
    n = conn.execute("SELECT COUNT(*) FROM mutations").fetchone()[0]
    conn.close()
    assert rows == [(marker["identity"],)]
    assert n == 1, "历史记录必须原样保留，收养不是重建"
    # 收养后 startup_recover 成功
    r2 = subprocess.run(
        [_PY, "-c",
         "from ducky.mutation_journal import startup_recover;"
         "print(startup_recover().get('integrity'))"],
        capture_output=True, env={**os.environ, **env}, timeout=30, cwd=_REPO)
    assert r2.returncode == 0
    assert "ok" in r2.stdout.decode()


def test_wp1_adoption_is_idempotent(tmp_path):
    """已收养库再跑：跳过，不重复建、不报错。"""
    data = tmp_path / "data"
    _build_legacy_journal(data)
    env = _journal_env(data, 59998)
    script = str(_REPO / "scripts" / "adopt_mutation_journal.py")
    subprocess.run([_PY, script, "--json"], capture_output=True,
                   env={**os.environ, **env}, timeout=60, cwd=_REPO)
    r = subprocess.run([_PY, script, "--json"], capture_output=True,
                       env={**os.environ, **env}, timeout=60, cwd=_REPO)
    assert r.returncode == 0
    assert json.loads(r.stdout.decode())["status"] == "already_adopted"


def test_wp1_adoption_refuses_live_api(tmp_path):
    """负向对照：服务活着时收养必须被拒（写入者未停 = 不许动证据链）。"""
    data = tmp_path / "data"
    _build_legacy_journal(data)
    with _capture_server(lambda path: {"status": "ok"}) as (port, _calls):
        env = _journal_env(data, port)
        r = subprocess.run(
            [_PY, str(_REPO / "scripts" / "adopt_mutation_journal.py"), "--json"],
            capture_output=True, env={**os.environ, **env}, timeout=60, cwd=_REPO)
    assert r.returncode == 1
    assert json.loads(r.stdout.decode())["status"] == "refused_api_alive"


# ═══════════════════ WP6 · Stop 写线（注入安全 + session_id）═══════════════════

def test_wp6_stop_ingest_survives_hostile_transcript(tmp_path):
    """原实现用 r'''$INPUT''' 把 payload 拼进 python heredoc —— transcript
    含三引号即破语法（注入 + 静默失败）。环境变量传递必须扛住全部毒样本，
    且 /add 载荷完整。"""
    hostile = "'''三引号''' \"双引号\" \\反斜杠 ${PIPE} $(cmd) {大括号} '''"
    tp = _make_transcript(tmp_path, hostile, "收到 '''测试'''")

    def route(path):
        return {"status": "accepted", "job_id": "t1"}

    with _capture_server(route) as (port, calls):
        r = _run_hook(_CC_HOOKS / "aidumem-cc-ingest.sh",
                      {"session_id": "wp6-test", "transcript_path": str(tp),
                       "hook_event_name": "Stop"},
                      _hook_env(port, tmp_path))
        assert r.returncode == 0, r.stderr.decode(errors="replace")
        assert _wait_for(calls, lambda p: p.startswith("/add")), \
            "毒样本 transcript 导致写线静默失败 —— WP6 回归"
        adds = [(p, b) for p, b in calls if p.startswith("/add")]

    body = json.loads(adds[0][1].decode())
    # 毒样本内容逐字节完整送达（未被 shell/python 拼接吞掉或展开）
    assert body["messages"][0]["content"] == hostile[:500]
    assert body["user_id"] == _TEST_UID and body["caller_user_id"] == _TEST_UID
    # WP4 联动：session_id 口径 cc- 前缀（萃取线圈定来源靠它）
    assert body["metadata"]["_origin_session_id"] == "cc-wp6-test"
    assert body["metadata"]["source"] == "cc-ingest"


def test_wp6_stop_ingest_no_session_no_origin(tmp_path):
    """payload 没有 session_id 时不造假值 —— 空串如实传递，
    服务端 verbatim 落空 session_id（萃取线自己会 skip，不圈错程）。"""
    tp = _make_transcript(tmp_path, "无会话测试", "好的")
    with _capture_server(lambda path: {"status": "accepted"}) as (port, calls):
        r = _run_hook(_CC_HOOKS / "aidumem-cc-ingest.sh",
                      {"transcript_path": str(tp), "hook_event_name": "Stop"},
                      _hook_env(port, tmp_path))
        assert r.returncode == 0
        assert _wait_for(calls, lambda p: p.startswith("/add"))
        adds = [(p, b) for p, b in calls if p.startswith("/add")]
    body = json.loads(adds[0][1].decode())
    assert body["metadata"]["_origin_session_id"] == ""


def _stop_ingest_messages(tmp_path: Path, records: list) -> list:
    tp = tmp_path / "transcript.jsonl"
    tp.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
                  encoding="utf-8")
    with _capture_server(lambda path: {"status": "accepted"}) as (port, calls):
        r = _run_hook(_CC_HOOKS / "aidumem-cc-ingest.sh",
                      {"session_id": "pair", "transcript_path": str(tp), "hook_event_name": "Stop"},
                      _hook_env(port, tmp_path))
        assert r.returncode == 0, r.stderr.decode(errors="replace")
        assert _wait_for(calls, lambda p: p.startswith("/add"))
        adds = [b for p, b in calls if p.startswith("/add")]
    return json.loads(adds[0].decode())["messages"]


def _turn(role: str, content) -> dict:
    return {"type": role, "message": {"role": role, "content": content}}


def test_stop_ingest_never_pairs_a_question_with_the_previous_answer(tmp_path):
    """本轮没有文字回答（只调了工具）时只存问句。此前问句与回答各取「最后一条」、
    不看先后，于是这一问被配上上一轮的回答，污染萃取与召回。"""
    messages = _stop_ingest_messages(tmp_path, [
        _turn("user", "Q1: what is the capital of France?"),
        _turn("assistant", [{"type": "text", "text": "A1: Paris."}]),
        _turn("user", "Q2: list the files in my repo"),
        _turn("assistant", [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]),
        _turn("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "a.txt"}]),
    ])
    assert messages == [{"role": "user", "content": "Q2: list the files in my repo"}]


def test_stop_ingest_pairs_the_answer_given_after_tool_use(tmp_path):
    """对照：工具调用之后给出的文字回答仍属于这一问。"""
    messages = _stop_ingest_messages(tmp_path, [
        _turn("user", "Q1: what is the capital of France?"),
        _turn("assistant", [{"type": "text", "text": "A1: Paris."}]),
        _turn("user", "Q2: list the files in my repo"),
        _turn("assistant", [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]),
        _turn("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "a.txt"}]),
        _turn("assistant", [{"type": "text", "text": "A2: there is one file, a.txt."}]),
    ])
    assert [m["content"] for m in messages] == ["Q2: list the files in my repo",
                                                "A2: there is one file, a.txt."]


# ═══════════════════ WP4 · SessionEnd 萃取线适配层 ═══════════════════

def test_wp4_sessionend_adapts_payload_to_distill_source(tmp_path):
    """适配层职责：cc payload → distill 真源认的口径。
    ① session_id 加 cc- 前缀（与写线 _origin_session_id 逐字一致 ——
       差一个字符萃取线就圈不到 verbatim 来源，cc 精华线继续断着）；
    ② hook_event_name 转 on_session_end；
    ③ 复用生产验证的 distill 真源（不重造提炼逻辑）。"""
    def route(path):
        if path.startswith("/session/distill"):
            # skipped（短会话）即完成口径验证：真源收到请求本身就是证据
            return {"status": "skipped", "reason": "too_short",
                    "source_count": 0, "min_required": 3}
        return {"status": "ok"}

    with _capture_server(route) as (port, calls):
        r = _run_hook(_CC_HOOKS / "aidumem-cc-distill.sh",
                      {"session_id": "wp4-sess", "hook_event_name": "SessionEnd",
                       "transcript_path": "/nonexistent"},
                      _hook_env(port, tmp_path, AIDUMEI_DISTILL_FOREGROUND="1"),
                      timeout=40)
        assert r.returncode == 0, r.stderr.decode(errors="replace")
        assert _wait_for(calls, lambda p: p.startswith("/session/distill")), \
            "适配层没有把请求送到 /session/distill —— 萃取线仍是断的"

    distill = [p for p, _ in calls if p.startswith("/session/distill")]
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(distill[0]).query)
    assert q["session_id"] == ["cc-wp4-sess"], (
        "session_id 口径必须与写线 _origin_session_id 逐字一致（cc- 前缀）")
    assert q["user_id"] == [_TEST_UID] and q["bank_id"] == [_TEST_BID]


def test_wp4_sessionend_never_calls_session_start(tmp_path):
    """负向对照钉死旧缺陷：适配层调 session/start 就是「重复建会话、
    永不提炼」的原始病灶 —— 任何情况下都不许再出现。

    只看**非注释行**：头注释里记载「曾误调 /session/start」是有意保留的
    文档价值（说明为何这么改），负向对照要防的是代码里实际发起该调用。
    """
    src = (_CC_HOOKS / "aidumem-cc-distill.sh").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "session/start" not in code, \
        "旧病灶回归：SessionEnd 适配层代码里又在调用 session/start"
    assert "aidumem-distill.sh" in src, "必须复用 distill 真源，不许重造提炼逻辑"


def test_wp4_write_and_distill_session_id_same_literal(tmp_path):
    """写线与萃取线的 session_id 前缀必须是同一个字面量口径。
    两条线各改各的（一边 cc- 一边裸 sid）= 萃取永远圈不到来源，
    而且两边测试各自全绿 —— 假绿灯铁律的典型形态，故在一条断言里钉死。"""
    tp = _make_transcript(tmp_path, "口径联动测试", "好")
    distill_paths: list = []

    def route(path):
        if path.startswith("/session/distill"):
            return {"status": "skipped", "reason": "too_short",
                    "source_count": 0, "min_required": 3}
        return {"status": "accepted"}

    sid = "same-literal-sess"
    with _capture_server(route) as (port, calls):
        env_common = _hook_env(port, tmp_path)
        _run_hook(_CC_HOOKS / "aidumem-cc-ingest.sh",
                  {"session_id": sid, "transcript_path": str(tp),
                   "hook_event_name": "Stop"}, env_common)
        _run_hook(_CC_HOOKS / "aidumem-cc-distill.sh",
                  {"session_id": sid, "hook_event_name": "SessionEnd"},
                  {**env_common, "AIDUMEI_DISTILL_FOREGROUND": "1"}, timeout=40)
        _wait_for(calls, lambda p: p.startswith("/session/distill"))
        _wait_for(calls, lambda p: p.startswith("/add"))

    distill_paths = [p for p, _ in calls if p.startswith("/session/distill")]
    adds = [(p, b) for p, b in calls if p.startswith("/add")]
    assert adds and distill_paths, "两条线都应打到 mock 服务"
    write_sid = json.loads(adds[0][1].decode())["metadata"]["_origin_session_id"]
    from urllib.parse import parse_qs, urlparse
    distill_sid = parse_qs(urlparse(distill_paths[0]).query)["session_id"][0]
    assert write_sid == distill_sid == f"cc-{sid}", (
        f"写线={write_sid!r} 萃取线={distill_sid!r} —— 口径必须逐字一致")


# ═══════════════════ WP7 · 注入读线（探活余量 + 注入安全）═══════════════════

def test_wp7_inject_probe_timeout_not_too_tight():
    """0.3s 探活在服务高峰期写向量时会误超时触发熔断（实机实证）。
    判据钉「形态」：探活 /health 的 curl 超时 -m 值 ≥ 1
    （允许中间夹 AUTH_ARGS 等参数 —— 钉的是超时值，不是整行形状）。"""
    import re
    src = (_CC_HOOKS / "aidumem-cc-inject.sh").read_text(encoding="utf-8")
    probes = re.findall(r'curl\s+-s\s+-m\s+([\d.]+)[^\n]*?/health', src)
    assert probes, "找不到探活 curl —— 形态变了，守卫失去着力点"
    for p in probes:
        assert float(p) >= 1.0, f"探活超时 {p}s 过紧（历史病灶 0.3s），高峰期会误熔断"


def test_wp7_inject_no_string_spliced_query():
    """注入安全：读线的相关性检索 query 不许再用 shell 字符串拼进
    -d '{"query":"$PROMPT"}'（prompt 含双引号即破 JSON）——必须走
    环境变量/stdin 传递。"""
    src = (_CC_HOOKS / "aidumem-cc-inject.sh").read_text(encoding="utf-8")
    assert '"query":"$PROMPT"' not in src
    assert "AIDUMEI_HOOK_QUERY" in src, "query 必须经环境变量传给 python"


def test_wp7_inject_outputs_recall_and_stays_silent_on_dead_service(tmp_path):
    """端到端双态：服务在场 → stdout 有注入块；服务不在 → 静默 exit 0
    （熔断，绝不阻塞对话）。"""
    def route(path):
        if path.startswith("/search"):
            return {"results": [{"memory": "例会定在周五下午三点"}]}
        if path.startswith("/api/core-memory"):
            return {"blocks": {}}
        return {"status": "ok"}

    with _capture_server(route) as (port, _calls):
        r = _run_hook(_CC_HOOKS / "aidumem-cc-inject.sh",
                      {"prompt": "例会什么时候开", "session_id": "wp7"},
                      _hook_env(port, tmp_path), timeout=20)
    assert r.returncode == 0
    out = r.stdout.decode()
    assert "周五下午三点" in out, "相关记忆应注入 stdout"

    # 死服务 → 静默放行 + 熔断标记（标记路径指到 tmp，不碰部署机 /tmp）
    circuit = tmp_path / "circuit"
    src = (_CC_HOOKS / "aidumem-cc-inject.sh").read_text(encoding="utf-8")
    patched = src.replace('CIRCUIT="${TMPDIR:-/tmp}/.aidumem_cc_circuit"',
                          f'CIRCUIT="{circuit}"')
    hook_copy = tmp_path / "inject_copy.sh"
    hook_copy.write_text(patched, encoding="utf-8")
    dead_env = _hook_env(59997, tmp_path)
    r2 = _run_hook(hook_copy, {"prompt": "任意", "session_id": "wp7-dead"},
                   dead_env, timeout=20)
    assert r2.returncode == 0 and r2.stdout == b"", "死服务必须静默放行"
    assert circuit.exists(), "熔断标记必须落地（防雪崩）"

def test_f06_inject_surfaces_degraded_not_silence(tmp_path):
    """f0.5（fork 审计 F06）：服务端明确报 recall_verdict=degraded 时，
    读线必须透出降级注记（灯坏了≠没记忆）；真 not_found 仍安静——
    两个负向对照钉住边界，不许滑向「什么都喊」或「什么都不说」。"""
    def degraded_route(path):
        if path.startswith("/search"):
            return {"results": [], "recall_verdict": "degraded",
                    "degraded": ["vector unavailable"]}
        return {"blocks": {}}

    with _capture_server(degraded_route) as (port, _calls):
        r = _run_hook(_CC_HOOKS / "aidumem-cc-inject.sh",
                      {"prompt": "降级演练查询", "session_id": "f06-deg"},
                      _hook_env(port, tmp_path), timeout=20)
    assert r.returncode == 0
    out = r.stdout.decode()
    assert "检索降级" in out, out
    assert "vector unavailable" in out, out
    assert "不代表没有相关记忆" in out, out

    def notfound_route(path):
        if path.startswith("/search"):
            return {"results": [], "recall_verdict": "not_found"}
        return {"blocks": {}}

    with _capture_server(notfound_route) as (port, _calls):
        r2 = _run_hook(_CC_HOOKS / "aidumem-cc-inject.sh",
                       {"prompt": "真没货查询", "session_id": "f06-nf"},
                       _hook_env(port, tmp_path), timeout=20)
    assert r2.returncode == 0
    assert "检索降级" not in r2.stdout.decode(), "not_found 不许喊降级"
