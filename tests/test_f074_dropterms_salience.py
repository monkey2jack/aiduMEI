"""f0.7.4：过滤词只挡别的 agent（豁免本人身份）+ salience 行带真实身份。

① AIDUMEI_CC_DROP_TERMS 原先对所有身份生效：主代理自己含其名字的记忆在它自己的会话里也被丢
   （生产实测：能搜到 0.878，却不注入）。现在 AIDUMEI_CC_DROP_TERMS_EXEMPT 列出的身份不过滤，
   默认空 = 照旧全部过滤。
② /search 提权补插 salience 行不带作用域 → 被记成 default（生产 109 行）。现在带戳；
   原文句柄 verbatim:N 删除时连 salience 行一起清；存量用 scripts/salience_relabel.py 改回。
"""
import json
import os
import sqlite3
import subprocess
import threading
import time
from http.server import HTTPServer
from pathlib import Path

import pytest

import ducky.utils as utils
from tests.test_f07_p1_security import CODEX_HOOK, HOOK, _Stub

DROP = "secret-box-01|10.0.0.9"


def _run(hook, tmp_path, uid, exempt=None, env_file_exempt=None):
    srv = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    envf = tmp_path / "svc.env"
    envf.write_text(f"AIDUMEI_CC_DROP_TERMS_EXEMPT={env_file_exempt}\n" if env_file_exempt else "", encoding="utf-8")
    try:
        env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
               "AIDUMEM_DATA_DIR": str(tmp_path), "AIDUMEM_LOG_DIR": str(tmp_path),
               "AIDUMEM_ENV_FILE": str(envf),
               "AIDUMEM_URL": f"http://127.0.0.1:{srv.server_address[1]}",
               "AIDUMEM_USER_ID": uid, "AIDUMEI_USER_ID": uid, "AIDUMEI_CC_DROP_TERMS": DROP}
        if exempt is not None:
            env["AIDUMEI_CC_DROP_TERMS_EXEMPT"] = exempt
        p = subprocess.run(["bash", hook], input=json.dumps({"prompt": "我喜欢喝什么", "session_id": "s1"}),
                           capture_output=True, text=True, env=env, timeout=30)
    finally:
        srv.shutdown()
    assert "用户喜欢喝茶" in p.stdout, p.stdout + p.stderr     # 注入本身照常
    return p.stdout


@pytest.mark.parametrize("hook", [HOOK, CODEX_HOOK])
def test_exempt_identity_keeps_its_own_memories(hook, tmp_path):
    assert "secret-box-01" in _run(hook, tmp_path, "dudu", exempt="dudu")
    assert "secret-box-01" in _run(hook, tmp_path, "dudu", exempt=" xiaohou | dudu ")   # 空格不影响


@pytest.mark.parametrize("hook", [HOOK, CODEX_HOOK])
def test_other_identities_still_filtered(hook, tmp_path):
    assert "secret-box-01" not in _run(hook, tmp_path, "xiaohou", exempt="dudu")
    assert "secret-box-01" not in _run(hook, tmp_path, "dudu-x", exempt="dudu")       # 整词比对，不是前缀
    assert "secret-box-01" not in _run(hook, tmp_path, "dudu")                         # 默认空 = 全部过滤


def test_cc_hook_reads_exempt_from_service_env(tmp_path):
    """CC 读线和过滤词一样能从服务 .env 兜底读豁免名单（生产就是这么配的）。"""
    assert "secret-box-01" in _run(HOOK, tmp_path, "dudu", env_file_exempt="dudu|hark_test")
    assert "secret-box-01" not in _run(HOOK, tmp_path, "xiaohou", env_file_exempt="dudu|hark_test")


# ── ② salience 身份戳 ──────────────────────────────────────────────
@pytest.fixture
def sal_db(monkeypatch, tmp_path):
    from ducky.salience.db import _ensure_db
    db = tmp_path / "salience.db"
    monkeypatch.setattr(utils, "SALIENCE_DB", str(db))
    _ensure_db()
    return db


def _row(db, mid):
    c = sqlite3.connect(str(db))
    try:
        return c.execute("SELECT user_id, bank_id, access_count FROM salience WHERE memory_id=?", (mid,)).fetchone()
    finally:
        c.close()


def test_search_boost_stamps_caller_identity(sal_db):
    from ducky.mem0_runtime import boost_salience_for_results
    boost_salience_for_results([{"id": "verbatim:7"}, {"memory_id": "m-x"}], "dudu", "default")
    assert _row(sal_db, "verbatim:7")[:2] == ("dudu", "default")
    assert _row(sal_db, "m-x")[:2] == ("dudu", "default")
    boost_salience_for_results([{"id": "m-old"}])                     # 不传作用域 = 旧行为
    assert _row(sal_db, "m-old")[:2] == ("default", "default")


def test_access_heals_default_stamp_but_never_steals(sal_db):
    import ducky.salience.core as sc
    now = time.time()
    c = sqlite3.connect(str(sal_db))
    c.execute("INSERT INTO salience (memory_id, salience, last_access, access_count, created_at, user_id) "
              "VALUES ('a', 0.5, ?, 1, ?, 'default'), ('b', 0.5, ?, 1, ?, 'xiaohou')", (now, now, now, now))
    c.commit()
    c.close()
    sc.on_memory_accessed("a", user_id="dudu")
    sc.on_memory_accessed("b", user_id="dudu")
    assert _row(sal_db, "a") == ("dudu", "default", 2)                # default 戳改成检索方
    assert _row(sal_db, "b")[0] == "xiaohou"                          # 已有真实主人的不动


@pytest.fixture
def vb(sal_db, monkeypatch, tmp_path):
    from ducky.verbatim_vault import ensure_verbatim_schema
    monkeypatch.setattr(utils, "FACTS_DB", str(tmp_path / "facts.db"))
    monkeypatch.setattr(utils, "TEXT_FTS_DB", str(tmp_path / "text_fts.db"))
    monkeypatch.setattr(utils, "OBS_DB", str(tmp_path / "observations.db"))
    ensure_verbatim_schema()
    return sal_db


def _verbatim_handle(uid, text, word):
    from ducky.mem0_runtime import boost_salience_for_results
    from ducky.verbatim_vault import store_verbatim, verbatim_search
    store_verbatim(uid, [{"role": "user", "content": text}], {"session_id": "s"})
    handle = verbatim_search(word, user_id=uid, limit=5)[0]["id"]
    boost_salience_for_results([{"id": handle}], uid, "default")      # /search 命中原文 → 记一行显著性
    return handle


def test_verbatim_handle_delete_clears_salience_row(vb):
    from ducky.wal_engine import cascade_delete_memory
    handle = _verbatim_handle("f074_u", "F074 原文显著性级联测试 青铜钟", "青铜钟")
    assert _row(vb, handle)[0] == "f074_u"
    res = cascade_delete_memory(handle, user_id="f074_u")
    assert res["details"]["verbatim"] == 1
    assert res["details"].get("salience") == 1 and _row(vb, handle) is None


def test_verbatim_handle_delete_by_other_tenant_leaves_salience(vb):
    from ducky.wal_engine import cascade_delete_memory
    handle = _verbatim_handle("f074_a", "F074 越权删原文显著性 白银塔", "白银塔")
    cascade_delete_memory(handle, user_id="f074_b")
    assert _row(vb, handle)[0] == "f074_a"                            # 别人拿句柄删，碰不到


# ── 存量修复脚本 ──────────────────────────────────────────────────
def test_relabel_script_dry_run_then_apply(sal_db, monkeypatch, tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "salience_relabel", Path(__file__).resolve().parents[1] / "scripts" / "salience_relabel.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    facts, fts = tmp_path / "facts.db", tmp_path / "text_fts.db"
    monkeypatch.setattr(utils, "FACTS_DB", str(facts))
    monkeypatch.setattr(utils, "TEXT_FTS_DB", str(fts))
    monkeypatch.setattr(utils, "DATA_DIR", str(tmp_path))
    c = sqlite3.connect(str(facts))
    c.execute("CREATE TABLE verbatim_turns (id INTEGER PRIMARY KEY, user_id TEXT, bank_id TEXT, content TEXT)")
    c.execute("INSERT INTO verbatim_turns VALUES (5, 'dudu', 'default', 'x')")
    c.commit()
    c.close()
    c = sqlite3.connect(str(fts))
    c.execute("CREATE TABLE memories (id TEXT, content TEXT, user_id TEXT, bank_id TEXT)")
    c.executemany("INSERT INTO memories VALUES (?, '', ?, 'default')",
                  [("m-xh", "xiaohou"), ("m-two", "dudu"), ("m-two", "xiaohou"), ("m-def", "default")])
    c.commit()
    c.close()
    now = time.time()
    c = sqlite3.connect(str(sal_db))
    c.executemany("INSERT INTO salience (memory_id, salience, last_access, access_count, created_at, user_id) "
                  "VALUES (?, 0.5, ?, 0, ?, ?)",
                  [(m, now, now, u) for m, u in [("verbatim:5", "default"), ("m-xh", "default"), ("m-gone", "default"),
                                                 ("m-two", "default"), ("m-def", "default"), ("m-ok", "dudu")]])
    c.commit()
    c.close()

    p = mod.plan()
    s = mod.summary(p)
    assert s == {"rows_default": 5, "relabel_by_user": {"dudu": 1, "xiaohou": 1},
                 "orphans": 1, "ambiguous": 1, "owner_is_default": 1}
    assert _row(sal_db, "verbatim:5")[0] == "default"                 # dry-run 不改
    r = mod.apply(p)
    assert r["updated"] == 2 and os.path.exists(r["backup"])
    assert _row(sal_db, "verbatim:5")[0] == "dudu" and _row(sal_db, "m-xh")[0] == "xiaohou"
    assert _row(sal_db, "m-gone")[0] == "default"                     # 孤儿只报告，不删
    assert _row(sal_db, "m-two")[0] == "default"                      # 歧义不猜
