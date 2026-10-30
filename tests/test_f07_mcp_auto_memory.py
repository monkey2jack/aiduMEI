"""MCP 后台自动记忆（mcp_server.run_auto_memory）对真实宿主 state.db 的行为。

宿主会话库的结构以 Hermes 的 hermes_state_common.py 为准：sessions 主键列是 `id`，
messages.session_id 引用 sessions(id)。此前 _resolve_session_owner 查
`WHERE session_id=?`，在这种库上必报 no such column，被吞成「owner 不明」，于是每个
会话都被拒写；而游标照样推进到最新，这段对话永久不会再被处理。全套件此前没有任何
用例调用过 run_auto_memory。
"""
from __future__ import annotations

import sqlite3

import pytest


@pytest.fixture()
def host(tmp_path, monkeypatch):
    import mcp_server as m

    db = tmp_path / "state.db"
    with sqlite3.connect(db) as c:
        c.executescript("""
            CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, user_id TEXT);
            CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
                                   session_id TEXT NOT NULL REFERENCES sessions(id),
                                   role TEXT NOT NULL, content TEXT, timestamp REAL NOT NULL);
            INSERT INTO sessions VALUES ('sess-alice', 'telegram', 'alice');
            INSERT INTO messages(session_id, role, content, timestamp) VALUES
              ('sess-alice', 'user', 'please remember my dentist is on Friday', 1),
              ('sess-alice', 'assistant', 'Noted: dentist appointment on Friday.', 2);
        """)
    monkeypatch.setattr(m, "STATE_DB", str(db))
    monkeypatch.setattr(m, "LAST_ID_FILE", str(tmp_path / "last_id.txt"))
    monkeypatch.setattr(m, "_auto_retry_counts", {})
    monkeypatch.setattr(m, "_auto_stored", set())
    posted = []
    reply = {"value": {"results": [{"id": "x"}]}}

    def fake_post(path, body=None, **_kw):
        posted.append((path, body))
        return reply["value"]

    monkeypatch.setattr(m, "_api_post", fake_post)
    return m, db, posted, reply


def test_owner_comes_from_the_host_sessions_id_column(host):
    m, _db, posted, _ = host
    assert m._resolve_session_owner("sess-alice") == "alice"
    m.run_auto_memory()
    assert [(p, b["user_id"]) for p, b in posted] == [("/add", "alice")]
    assert m._read_last_id() == 2


def test_transient_failure_keeps_the_cursor_for_a_retry(host):
    m, _db, posted, reply = host
    reply["value"] = {"error": "HTTP 503", "detail": "warming up"}
    m.run_auto_memory()
    assert posted, "应当尝试写入"
    assert m._read_last_id() == 0, "暂时性失败的会话被游标越过 = 永久丢失"


def test_permanent_rejection_is_skipped_not_retried_forever(host):
    m, _db, _posted, reply = host
    reply["value"] = {"error": "HTTP 403", "detail": "cross hall"}
    m.run_auto_memory()
    assert m._read_last_id() == 2


def test_host_state_db_is_opened_read_only(host, monkeypatch):
    m, db, _posted, _ = host
    real_connect, targets = sqlite3.connect, []

    def spy(target, *a, **kw):
        targets.append((str(target), kw.get("uri")))
        return real_connect(target, *a, **kw)

    monkeypatch.setattr(sqlite3, "connect", spy)
    m.run_auto_memory()
    host_opens = [t for t in targets if str(db.name) in t[0]]
    assert host_opens, "没有打开宿主库"
    assert all(uri and t.endswith("?mode=ro") for t, uri in host_opens), host_opens


def test_persistent_transient_failure_releases_cursor_after_retry_cap(host):
    """f0.7.1：某会话持续 5xx 时游标不能永远卡住 —— 达到上限后放行。"""
    m, _db, posted, reply = host
    reply["value"] = {"error": "HTTP 500", "detail": "boom"}
    for _ in range(m._AUTO_MAX_TRANSIENT_RETRIES - 1):
        m.run_auto_memory()
        assert m._read_last_id() == 0
    m.run_auto_memory()
    assert m._read_last_id() == 2
    assert len(posted) == m._AUTO_MAX_TRANSIENT_RETRIES


def test_retry_does_not_rewrite_sessions_that_already_succeeded(host):
    """f0.7.1：游标因别的会话回退时，已写成功的会话下一轮不再重写。"""
    m, db, posted, reply = host
    with sqlite3.connect(db) as c:
        c.executescript("""
            INSERT INTO sessions VALUES ('sess-bob', 'telegram', 'bob');
            INSERT INTO messages(session_id, role, content, timestamp) VALUES
              ('sess-bob', 'user', 'bob likes tea', 3),
              ('sess-bob', 'assistant', 'Noted: tea.', 4);
        """)

    def flaky(path, body=None, **_kw):
        posted.append((path, body))
        return {"error": "HTTP 503"} if body["user_id"] == "alice" else {"results": [{"id": "y"}]}

    m._api_post = flaky
    m.run_auto_memory()
    m.run_auto_memory()
    bob_posts = [b for _p, b in posted if b["user_id"] == "bob"]
    assert len(bob_posts) == 1, "已成功的会话被重写"
