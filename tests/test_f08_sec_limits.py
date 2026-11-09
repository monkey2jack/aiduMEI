"""f0.8 ③-6 列表 limit 钳制 + 读路径按钥匙限流（外审 muse M-19 / M-20 / M-21 反向断言）。"""
from __future__ import annotations

import uuid

import pytest

from ducky import request_limits as RL


# ── 纯函数 ────────────────────────────────────────────────────────────────

def test_clamp_limit_bounds(monkeypatch):
    monkeypatch.delenv("AIDUMEI_LIST_LIMIT_MAX", raising=False)
    assert RL.clamp_limit(0) == 1
    assert RL.clamp_limit(-5) == 1               # 修复前：SQLite LIMIT -5 = 无上限
    assert RL.clamp_limit(10**6) == 500
    assert RL.clamp_limit(37) == 37
    assert RL.clamp_limit("abc", default=50) == 50
    assert RL.clamp_limit(None, default=20) == 20
    monkeypatch.setenv("AIDUMEI_LIST_LIMIT_MAX", "40")
    assert RL.clamp_limit(10**6) == 40


def test_clamp_query_string(monkeypatch):
    monkeypatch.delenv("AIDUMEI_LIST_LIMIT_MAX", raising=False)
    out = RL.clamp_query_string(b"limit=-3&offset=-9&q=x")
    assert out == b"limit=1&offset=0&q=x"
    assert RL.clamp_query_string(b"top_k=99999&k=0") == b"top_k=500&k=1"
    assert RL.clamp_query_string(b"limit=abc") == b"limit=abc"      # 非整数原样交给路由 422
    assert RL.clamp_query_string(b"limit=20&offset=3") == b"limit=20&offset=3"
    assert RL.clamp_query_string(b"depth=-1") == b"depth=-1"         # depth 不归这里管


def test_is_read_request():
    assert RL.is_read_request("GET", "/facts")
    assert RL.is_read_request("POST", "/search")
    assert RL.is_read_request("POST", "/api/search/")
    assert not RL.is_read_request("POST", "/add")
    assert not RL.is_read_request("POST", "/delete")


# ── HTTP ────────────────────────────────────────────────────────────────

def _client(monkeypatch):
    import api_server
    from fastapi.testclient import TestClient
    tok = f"limits-f08-{uuid.uuid4().hex[:12]}"       # 每条用例一个新钥匙 = 一个新桶
    monkeypatch.setenv("AIDUMEM_API_TOKEN", tok)
    monkeypatch.setenv("AIDUMEI_ENGINE_MODE", "local")
    monkeypatch.delenv("AIDUMEI_LIST_LIMIT_MAX", raising=False)
    return TestClient(api_server.app, headers={"Authorization": f"Bearer {tok}"})


@pytest.fixture()
def seeded(monkeypatch):
    # 其他用例会改 utils.FACTS_DB，legacy 路由却用导入时那一份——让路由读本用例播种的同一个库
    import ducky.utils as utils
    from ducky.hot import legacy_helpers
    monkeypatch.setattr(legacy_helpers, "FACTS_DB", utils.FACTS_DB)
    from ducky.utils import get_facts_conn
    from ducky.schema_bootstrap import ensure_core_schema
    ensure_core_schema(force=True)
    user = f"lim-{uuid.uuid4().hex[:8]}"
    conn = get_facts_conn()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(facts)")}
    for i in range(3):
        row = {"category": "pref", "fact_key": f"k{i}", "fact_value": f"synthetic value {i}",
               "user_id": user, "agent_id": user}
        if "bank_id" in cols:
            row["bank_id"] = "default"
        conn.execute(f"INSERT INTO facts ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                     list(row.values()))
    conn.commit()
    conn.close()
    return user


def _facts(c, user, **params):
    r = c.get("/api/facts", params={"user_id": user, "caller_user_id": user, **params})
    assert r.status_code == 200, r.text
    return r.json()


def test_facts_limit_offset_truncated(monkeypatch, seeded):
    c = _client(monkeypatch)
    body = _facts(c, seeded, limit=2)
    assert body["count"] == 2 and body["truncated"] is True and body["limit"] == 2
    body = _facts(c, seeded, limit=2, offset=2)
    assert body["count"] == 1 and body["truncated"] is False and body["offset"] == 2
    body = _facts(c, seeded)
    assert body["count"] == 3 and body["truncated"] is False and body["limit"] == 500


def test_facts_negative_and_huge_values_are_clamped(monkeypatch, seeded):
    c = _client(monkeypatch)
    body = _facts(c, seeded, limit=-1)                 # 修复前：无上限全量回包
    assert body["limit"] == 1 and body["count"] == 1 and body["truncated"] is True
    body = _facts(c, seeded, limit=10**7, offset=-5)
    assert body["limit"] == 500 and body["offset"] == 0 and body["count"] == 3


def test_entities_negative_limit_is_clamped(monkeypatch, seeded):
    c = _client(monkeypatch)
    r = c.get("/api/facts/entities/list", params={"user_id": seeded, "caller_user_id": seeded, "limit": -1})
    assert r.status_code == 200, r.text


def test_read_rate_limit_429_when_set_low(monkeypatch, seeded):
    c = _client(monkeypatch)
    monkeypatch.setenv("AIDUMEI_RATE_READ_PER_MIN", "3")
    codes = [c.get("/api/facts", params={"user_id": seeded, "caller_user_id": seeded}).status_code
             for _ in range(5)]
    # 固定自然分钟窗口：极少数情况下跨分钟会重置一次，最多多放行 3 次
    assert 429 in codes, codes
    r = c.get("/api/facts", params={"user_id": seeded, "caller_user_id": seeded})
    if r.status_code == 429:
        assert int(r.headers["Retry-After"]) >= 1
        assert r.json()["code"] == "read_rate_limited"
    # 写路径不吃读配额
    w = c.post("/add", json={"messages": [{"role": "user", "content": "synthetic write"}],
                             "user_id": seeded, "caller_user_id": seeded, "infer": False})
    assert w.status_code == 200, w.text


def test_read_rate_limit_zero_disables(monkeypatch, seeded):
    c = _client(monkeypatch)
    monkeypatch.setenv("AIDUMEI_RATE_READ_PER_MIN", "0")
    codes = {c.get("/api/facts", params={"user_id": seeded, "caller_user_id": seeded}).status_code
             for _ in range(30)}
    assert codes == {200}, codes


def test_unauthorized_request_never_reaches_limiter(monkeypatch, seeded):
    import api_server
    from fastapi.testclient import TestClient
    _client(monkeypatch)
    monkeypatch.setenv("AIDUMEI_RATE_READ_PER_MIN", "1")
    anon = TestClient(api_server.app)
    codes = {anon.get("/api/facts", params={"user_id": seeded}).status_code for _ in range(3)}
    assert 429 not in codes and codes <= {401, 403}, codes
