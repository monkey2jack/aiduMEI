"""v20.3.2 正式版 · 一致性与底层：P1-8 中文词元 / P1-9 事务卫生 / P1-10 幂等 / P1-12 Host / P1-17 WAL compaction。

这批是结构性的：外审 Gemini 3.8 与 Codex 各挖出一半。共同点是**在串行、正常、
凭据齐全的路上全绿；换到并发 / 异常 / 中文 / 浏览器 / 长期运行，边界不成立。**
"""
import json
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request

# 回环请求不许受宿主 *_PROXY 环境影响（生产 .env 带代理时 127.0.0.1 会被送进代理 → 超时/失败）
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

import pytest


# ══════════════════════════════════════════════════════════════
# P1-8 · 中文词元覆盖率必须有区分度（Gemini P0-4）
# ══════════════════════════════════════════════════════════════

def test_multiword_chinese_query_has_nonzero_overlap():
    """**P1-8 靶心**：原正则 `[一-鿿]+` 把整句连续汉字当**一个** token → 多词查询恒 0。"""
    from ducky.scoring import calc_token_overlap_score as f
    s = f("用户喜欢吃苹果", "用户非常喜欢吃红富士苹果")
    assert s >= 0.5, f"多词中文查询词元分 {s}（修复前 0.0）—— 融合分里 0.25 权重的词法支路对中文失效"
    s2 = f("怎么配置数据库密码", "数据库密码配置在 .env 文件里")
    assert s2 >= 0.4, f"{s2}"


def test_unrelated_text_stays_low():
    """**负向对照**：无关文本不许因切细而虚高。"""
    from ducky.scoring import calc_token_overlap_score as f
    assert f("网关端口是22012", "今天天气很好我们去爬山") < 0.2
    assert f("用户喜欢吃苹果", "服务器重启后需要检查日志") < 0.2


def test_substring_and_ascii_paths_are_unchanged():
    """**回归**：子串命中仍满分；英文按词。"""
    from ducky.scoring import calc_token_overlap_score as f
    assert f("网关端口", "网关端口是22012") == 1.0
    assert f("gateway port", "the gateway port is 22012") == 1.0
    assert f("祖母", "我的祖母很和善") == 1.0
    assert f("", "x") == 0.0 and f("x", "") == 0.0


def test_old_name_is_not_used_by_our_own_code():
    """用户审计 F：旧名 calc_bm25_score 只许作为别名**定义**存在，自家不许再调用它。"""
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parents[1] / "ducky"
    uses = []
    for p in root.rglob("*.py"):
        if p.name == "version.py":
            continue  # 版本叙事里故意写着旧名
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if "calc_bm25_score" in line and not line.strip().startswith("#"):
                if re.match(r"\s*calc_bm25_score\s*=\s*calc_token_overlap_score", line):
                    continue  # 别名定义行
                if '"""' in line or "原名" in line or "`calc_bm25_score`" in line:
                    continue  # docstring 叙事
                uses.append(f"{p.relative_to(root.parent)}:{i}")
    assert not uses, f"自家代码仍用旧名（读者会以为那 0.25 权重来自真 BM25）：{uses}"


# ══════════════════════════════════════════════════════════════
# P1-9 · 事务卫生：异常退出不许留下悬挂事务（Gemini P0-2）
# ══════════════════════════════════════════════════════════════

@pytest.fixture()
def facts_conn(tmp_path, monkeypatch):
    import ducky.utils as u
    monkeypatch.setattr(u, "FACTS_DB", str(tmp_path / "f.db"), raising=False)
    # 换库后线程本地缓存必须失效：直接清掉本线程的缓存键
    key = f"conn_{u.FACTS_DB}"
    if hasattr(u._thread_local, key):
        delattr(u._thread_local, key)
    return u


def test_context_manager_exit_rolls_back_on_exception(facts_conn):
    """**P1-9 靶心①**：`with conn:` 块内抛异常 → 底层连接不许留在 in_transaction。

    原 `_ConnProxy.__exit__` 是裸 pass，close() 也是 pass —— 语言级契约被抹掉，
    异常后连接永久持有写锁，其他线程排队到 busy_timeout 后 `database is locked`。
    """
    u = facts_conn
    conn = u.get_facts_conn()
    conn.execute("CREATE TABLE IF NOT EXISTS t_cp(x INTEGER PRIMARY KEY)")
    conn.execute("INSERT OR IGNORE INTO t_cp VALUES(1)")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            conn.execute("INSERT INTO t_cp VALUES(2)")
            conn.execute("INSERT INTO t_cp VALUES(1)")  # UNIQUE 冲突
    assert conn._conn.in_transaction is False, "with 块异常退出后事务仍悬挂"
    rows = conn.execute("SELECT COUNT(*) FROM t_cp").fetchone()[0]
    assert rows == 1, "回滚没生效：半截写入留在了库里"


def test_context_manager_exit_commits_on_success(facts_conn):
    """**回归**：正常退出 `with conn:` 要像标准 sqlite3 一样提交。"""
    u = facts_conn
    conn = u.get_facts_conn()
    conn.execute("CREATE TABLE IF NOT EXISTS t_ok(x INTEGER)")
    conn.commit()
    with conn:
        conn.execute("INSERT INTO t_ok VALUES(7)")
    assert conn._conn.in_transaction is False
    assert conn.execute("SELECT COUNT(*) FROM t_ok").fetchone()[0] == 1


def test_leaked_transaction_is_visible_at_borrow_time(facts_conn, caplog):
    """**P1-9 靶心②**：借出连接时若发现上一位借用者留下悬挂事务，必须**出声**（不静默）。

    不在借出时自动回滚：同一线程内的嵌套借用（helper 再拿一次连接）是合法的，
    自动回滚会毁掉外层在途写入。出声 + /health 计数，让泄漏可被定位。
    """
    import logging
    u = facts_conn
    conn = u.get_facts_conn()
    conn.execute("CREATE TABLE IF NOT EXISTS t_leak(x INTEGER)")
    conn.commit()
    # 请求 A 开了事务没收尾；请求 B（另一个上下文戳）借到同一线程连接 → 这才是悬挂
    tok_a = u.BORROW_CONTEXT.set("request-A")
    conn = u.get_facts_conn()
    conn.execute("INSERT INTO t_leak VALUES(1)")  # 开事务、不提交、模拟 except: pass 后返回
    u.BORROW_CONTEXT.reset(tok_a)
    before = u.leaked_transaction_count()
    monkeypatch_attr = getattr(u._thread_local, "tx_warned_at", None)
    u._thread_local.tx_warned_at = 0.0  # 每线程 60s 限流：前序用例可能刚出过声
    tok_b = u.BORROW_CONTEXT.set("request-B")
    with caplog.at_level(logging.WARNING):
        u.get_facts_conn()
    u.BORROW_CONTEXT.reset(tok_b)
    if monkeypatch_attr is not None:
        u._thread_local.tx_warned_at = monkeypatch_attr
    assert u.leaked_transaction_count() == before + 1, "另一个请求留下的悬挂事务没计数"
    assert any("悬挂" in r.getMessage() for r in caplog.records), "借出时发现悬挂事务却一声不响"
    last = u.leaked_transaction_last()
    assert last.get("previous_borrower") and last.get("current_borrower"), f"现场没记调用点：{last}"
    conn.rollback()


def test_nested_borrow_in_same_request_is_not_counted_as_leak(facts_conn, caplog):
    """**收口后修正（生产首日 2 次「悬挂」的真相）**：同一请求内 INSERT 后再借一次连接 = 嵌套借用，不计数。

    现场：write_fact 执行 INSERT（事务在途）→ 治理钩子 ensure_governance_schema 再借同一线程连接。
    第一版探测器把它计成泄漏 —— 假红。仪器必须分得清「谁开的事务」。
    """
    import logging
    u = facts_conn
    tok = u.BORROW_CONTEXT.set("request-same")
    try:
        conn = u.get_facts_conn()
        conn.execute("CREATE TABLE IF NOT EXISTS t_nest(x INTEGER)")
        conn.commit()
        before = u.leaked_transaction_count()
        conn.execute("INSERT INTO t_nest VALUES(1)")   # 事务在途
        u._thread_local.tx_warned_at = 0.0
        with caplog.at_level(logging.WARNING):
            inner = u.get_facts_conn()                 # 同一请求嵌套借用
        inner.execute("INSERT INTO t_nest VALUES(2)")
        conn.commit()
        assert u.leaked_transaction_count() == before, "同一请求的嵌套借用被计成了悬挂事务（假红）"
        assert not any("悬挂" in r.getMessage() for r in caplog.records)
        assert conn._conn.in_transaction is False
    finally:
        u.BORROW_CONTEXT.reset(tok)


def test_request_middleware_stamps_borrow_context():
    """定义了不接线 = 没做：最外层中间件必须给每个请求盖戳。"""
    import ast as _ast
    import pathlib as _pl
    src = (_pl.Path(__file__).resolve().parents[1] / "api_server.py").read_text(encoding="utf-8")
    tree = _ast.parse(src)
    fn = next(n for n in _ast.walk(tree) if isinstance(n, _ast.AsyncFunctionDef) and n.name == "_record_http_outcome")
    seg = _ast.get_source_segment(src, fn) or ""
    assert "BORROW_CONTEXT" in seg and ".set(" in seg, "请求入口没有盖借用上下文戳，探测器分不出嵌套与悬挂"


def test_write_functions_without_rollback_only_decrease():
    """**棘轮**：ducky/ 内「含写 SQL + try 且无 rollback」的函数数只许降。基线见常量。"""
    import ast
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    n = 0
    for p in (root / "ducky").rglob("*.py"):
        text = p.read_text(encoding="utf-8")
        try:
            tree = ast.parse(text)
        except Exception:
            continue
        for fn in [x for x in ast.walk(tree) if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            src = ast.get_source_segment(text, fn) or ""
            writes = any(k in src for k in ("INSERT", "UPDATE ", "DELETE FROM", "ALTER TABLE", "CREATE "))
            if writes and ".execute(" in src and any(isinstance(x, ast.Try) for x in ast.walk(fn)) and "rollback" not in src:
                n += 1
    BASELINE = 93   # 2026-09-03 实测 96 → 幂等层三函数补 rollback 后 93；只降不升
    assert n <= BASELINE, f"无 rollback 的写函数从 {BASELINE} 涨到 {n} —— 新代码请用 with conn: 或显式 rollback"


# ══════════════════════════════════════════════════════════════
# P1-10 · 幂等：pending 不许继续写；finalize 失败不许永久 409（Codex F-01 / Gemini P1-3）
# ══════════════════════════════════════════════════════════════

@pytest.fixture()
def idem_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDUMEM_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("AIDUMEM_LOG_DIR", str(tmp_path / "l"))
    import ducky.utils as u
    monkeypatch.setattr(u, "FACTS_DB", str(tmp_path / "f.db"), raising=False)
    key = f"conn_{u.FACTS_DB}"
    if hasattr(u._thread_local, key):
        delattr(u._thread_local, key)
    from ducky import idempotency as I
    return I


def test_concurrent_claims_never_both_get_new(idem_env):
    """**P1-10 靶心①**：两个连接 barrier 同时 claim 同 key，最多一个拿到 new。"""
    I = idem_env
    I.claim("warm-up", "u", "default", {"c": 0})  # 先建库建表：两线程并发首开空库不是本条要测的
    results = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        results.append(I.claim("race-key", "u", "default", {"c": "same"})["action"])
    ts = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in ts]; [t.join(5) for t in ts]
    assert results.count("new") <= 1, f"双 new：两个请求都会进业务写 → 重复落库：{results}"


def test_pending_is_refused_by_the_write_routes(tmp_path, monkeypatch):
    """**P1-10 靶心②**：第二个请求拿到 pending 时，/add 与 /add/raw 必须拒绝（409），不许继续写。"""
    monkeypatch.setenv("AIDUMEM_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("AIDUMEM_LOG_DIR", str(tmp_path / "l"))
    from fastapi.testclient import TestClient
    import api_server as A
    monkeypatch.setenv("AIDUMEM_API_TOKEN", "probe-token-not-a-real-secret")
    from ducky import idempotency as I
    monkeypatch.setattr(I, "claim", lambda *a, **k: {"action": "pending", "key": "k1"})
    c = TestClient(A.app, raise_server_exceptions=False,
                   headers={"Authorization": "Bearer probe-token-not-a-real-secret"})
    r = c.post("/add/raw", json={"content": "x", "user_id": "u", "bank_id": "default"},
               headers={"Idempotency-Key": "k1"})
    assert r.status_code == 409, f"pending 状态下 /add/raw 回 {r.status_code}，应 409 且不写"
    r2 = c.post("/add", json={"messages": "x", "user_id": "u", "bank_id": "default", "infer": False},
                headers={"Idempotency-Key": "k1"})
    assert r2.status_code == 409, f"pending 状态下 /add 回 {r2.status_code}"


def test_finalize_failure_releases_the_key_instead_of_locking_it(idem_env, monkeypatch):
    """**P1-10 靶心③**：finalize 失败（如 database is locked）不许把 key 留成 NULL → 客户端永久 409。"""
    I = idem_env
    st = I.claim("fin-key", "u", "default", {"c": 1})
    assert st["action"] == "new"
    import ducky.utils as u
    real = u.get_facts_conn

    class _Boom:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("database is locked")
        def commit(self): pass
        def rollback(self): pass
        def close(self): pass
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        return _Boom() if calls["n"] == 1 else real()
    monkeypatch.setattr(u, "get_facts_conn", flaky)
    monkeypatch.setattr(I, "get_facts_conn", flaky, raising=False)
    I.finalize("fin-key", "u", "default", {"ok": True})
    monkeypatch.setattr(u, "get_facts_conn", real)
    monkeypatch.setattr(I, "get_facts_conn", real, raising=False)
    again = I.claim("fin-key", "u", "default", {"c": 1})
    assert again["action"] in ("new", "replay"), (
        f"finalize 失败后重试拿到 {again['action']} —— key 被锁死，合法重试永久 409")


# ══════════════════════════════════════════════════════════════
# P1-12 · 无凭据模式 Host 校验（Codex F-04 · DNS rebinding）
# ══════════════════════════════════════════════════════════════

class _RealServer:
    def __init__(self, app):
        import uvicorn
        s = socket.socket(); s.bind(("127.0.0.1", 0)); self.port = s.getsockname()[1]; s.close()
        cfg = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error",
                             lifespan="off", proxy_headers=False)
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(100):  # 就绪 = 端口接受 TCP 连接（/health 在真后端配置下可能很慢，见 real_asgi 同名注释）
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return self
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("uvicorn 没起来（端口 10s 内未接受连接）")

    def __exit__(self, *_):
        self.server.should_exit = True; self.thread.join(5)

    def req(self, method, path, headers=None, body=None):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                   headers=headers or {}, data=body)
        try:
            return _NO_PROXY_OPENER.open(r, timeout=5).status
        except urllib.error.HTTPError as e:
            return e.code


@pytest.fixture()
def bare_app(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDUMEM_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("AIDUMEM_LOG_DIR", str(tmp_path / "l"))
    import api_server as A  # noqa: F401
    for k in ("AIDUMEM_API_TOKEN", "AIDUMEM_UI_PASSWORD", "AIDUMEI_TRUST_PROXY", "AIDUMEI_TRUSTED_HOSTS"):
        monkeypatch.delenv(k, raising=False)
    import ducky.security.auth as _auth
    monkeypatch.setattr(_auth, "password_hash_path", lambda: str(tmp_path / "h"))
    A._ensure_ui_password()
    assert A._auth_enabled() is False
    return A


def test_foreign_host_header_is_refused_without_credentials(bare_app):
    """**P1-12 靶心**：对端是回环、Host 却是攻击者域名 → DNS rebinding 形态，必须拒绝。"""
    with _RealServer(bare_app.app) as s:
        code = s.req("GET", "/facts?user_id=p&bank_id=default", headers={"Host": "attacker.example"})
    assert code in (400, 421), f"Host: attacker.example 被放行（{code}）—— 浏览器可借用户之手打本地 API"


def test_loopback_hosts_are_allowed(bare_app):
    """**回归**：localhost / 127.0.0.1（带端口）照旧放行。"""
    with _RealServer(bare_app.app) as s:
        assert s.req("GET", "/facts?user_id=p&bank_id=default", headers={"Host": f"127.0.0.1:{s.port}"}) not in (400, 421)
        assert s.req("GET", "/facts?user_id=p&bank_id=default", headers={"Host": "localhost"}) not in (400, 421)
        assert s.req("GET", "/facts?user_id=p&bank_id=default", headers={"Host": "[::1]:8767"}) not in (400, 421)


def test_cross_site_browser_write_is_refused(bare_app):
    """浏览器跨站写请求（Origin 非本机 / Sec-Fetch-Site: cross-site）→ 拒绝；无 Origin 的 CLI 放行。"""
    with _RealServer(bare_app.app) as s:
        code = s.req("POST", "/add/raw", body=b'{"content":"x"}',
                     headers={"Host": "127.0.0.1", "Content-Type": "application/json",
                              "Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site"})
        assert code == 403, f"跨站写被放行（{code}）"
        ok = s.req("POST", "/add/raw", body=b'{"content":"x"}',
                   headers={"Host": "127.0.0.1", "Content-Type": "application/json"})
        assert ok != 403, "无 Origin 的 CLI/MCP 写被误拒"


def test_trusted_hosts_env_extends_the_allowlist(bare_app, monkeypatch):
    monkeypatch.setenv("AIDUMEI_TRUSTED_HOSTS", "memory.internal, 10.0.0.5")
    with _RealServer(bare_app.app) as s:
        code = s.req("GET", "/facts?user_id=p&bank_id=default", headers={"Host": "memory.internal"})
    assert code not in (400, 421), f"AIDUMEI_TRUSTED_HOSTS 里的名字仍被拒（{code}）"


def test_trust_proxy_hands_host_check_to_the_proxy(bare_app, monkeypatch):
    """AIDUMEI_TRUST_PROXY=1 时反代转发的是对外域名 —— Host 校验必须让位，否则逃生阀再次失效。"""
    monkeypatch.setenv("AIDUMEI_TRUST_PROXY", "1")
    with _RealServer(bare_app.app) as s:
        code = s.req("GET", "/facts?user_id=p&bank_id=default", headers={"Host": "memory.example.com"})
    assert code not in (400, 421), f"声明了信任反代仍按 Host 拒绝（{code}）"


def test_credentialed_instance_does_not_host_check(tmp_path, monkeypatch):
    """有凭据时鉴权已守住写面；Host 校验只在无凭据模式生效（不打断反代部署）。"""
    monkeypatch.setenv("AIDUMEM_DATA_DIR", str(tmp_path / "d2"))
    monkeypatch.setenv("AIDUMEM_LOG_DIR", str(tmp_path / "l2"))
    import api_server as A  # noqa: F401
    monkeypatch.setenv("AIDUMEM_API_TOKEN", "probe-token-not-a-real-secret")
    with _RealServer(A.app) as s:
        code = s.req("GET", "/facts?user_id=p&bank_id=default",
                     headers={"Host": "memory.example.com", "Authorization": "Bearer probe-token-not-a-real-secret"})
    assert code == 200, f"有凭据 + 反代域名 Host 被误拒（{code}）"


# ══════════════════════════════════════════════════════════════
# P1-17 · WAL compaction（Gemini P0-1 → 我下调 P1）
# ══════════════════════════════════════════════════════════════

@pytest.fixture()
def wal(tmp_path, monkeypatch):
    monkeypatch.setenv("AIDUMEM_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("AIDUMEM_LOG_DIR", str(tmp_path / "l"))
    import ducky.wal_engine as we
    # 单例跨用例泄漏：每个用例钉一个指向本用例 tmp 的新实例
    monkeypatch.setattr(we.WALEngine, "_instance", we.WALEngine(str(tmp_path / "wal")))
    return we


def test_compact_drops_settled_entries_but_keeps_every_pending(wal):
    """**P1-17 靶心**：1000 终态 + 3 pending → compact 后 pending 一条不丢，终态被清。"""
    we = wal
    eng = we.WALEngine.get_instance()
    for i in range(1000):
        eng.append(we.WALEntry(wal_id=f"done-{i}", operation="delete", user_id="u", bank_id="default",
                               payload={"memory_id": f"m{i}"}, status="pending"))
        eng.mark_status(f"done-{i}", "committed")
    for i in range(3):
        eng.append(we.WALEntry(wal_id=f"keep-{i}", operation="delete", user_id="u", bank_id="default",
                               payload={"memory_id": f"k{i}"}, status="pending"))
    before = eng.wal_file.stat().st_size
    report = eng.compact(keep_recent_seconds=0)
    after = eng.wal_file.stat().st_size
    pending = sorted(e.wal_id for e in eng.get_pending_entries())
    assert pending == ["keep-0", "keep-1", "keep-2"], f"pending 丢了：{pending}"
    assert after < before / 10, f"compact 没瘦身：{before} → {after}"
    assert report["dropped"] >= 1000 and report["kept"] >= 3


def test_compact_is_atomic_and_idempotent(wal):
    we = wal
    eng = we.WALEngine.get_instance()
    eng.append(we.WALEntry(wal_id="p1", operation="delete_all", user_id="u", bank_id="default",
                           payload={}, status="pending"))
    eng.compact(keep_recent_seconds=0); eng.compact(keep_recent_seconds=0)
    assert [e.wal_id for e in eng.get_pending_entries()] == ["p1"]
    assert not list(eng.wal_file.parent.glob("*.tmp")), "残留临时文件"


def test_startup_reconcile_compacts(wal):
    """对账收尾必须顺手 compact —— 否则重放账本永远只增不减（两条返回路径共用 _finish_reconcile）。"""
    we = wal
    eng = we.WALEngine.get_instance()
    for i in range(50):
        eng.append(we.WALEntry(wal_id=f"x{i}", operation="delete", user_id="u", bank_id="default",
                               payload={"memory_id": f"m{i}"}, status="pending"))
        eng.mark_status(f"x{i}", "committed")
    size_before = eng.wal_file.stat().st_size
    rep = we._finish_reconcile({})
    assert isinstance(rep.get("compacted"), dict) and "kept" in rep["compacted"], rep.get("compacted")
    assert eng.wal_file.stat().st_size < size_before, "状态行没有折叠进条目 —— 账本没有收敛"


def test_append_triggers_compaction_when_large(wal, monkeypatch):
    """体积触发：超过阈值的下一次 append 之后自动 compact（锁外调用，不许死锁）。"""
    we = wal
    eng = we.WALEngine.get_instance()
    monkeypatch.setattr(we.WALEngine, "COMPACT_SIZE_TRIGGER", 2000)
    old = time.time() - 90000  # 比默认保留窗（24h）更老的终态条目，收敛时应被丢弃
    for i in range(40):
        eng.append(we.WALEntry(wal_id=f"big{i}", timestamp=old, operation="delete", user_id="u",
                               bank_id="default", payload={"memory_id": "m" * 50}, status="pending"))
        eng.mark_status(f"big{i}", "committed")
    assert eng.compactions >= 1, "体积超阈值却一次 compaction 都没触发"
    assert eng.wal_file.stat().st_size < 2500, f"体积触发没收敛：{eng.wal_file.stat().st_size}"
