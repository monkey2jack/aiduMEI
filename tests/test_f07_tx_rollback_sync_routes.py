"""同步路由的悬挂事务：下一个请求借出连接时必须回滚，不许被它的 commit 一起提交。

W-04 的请求收尾清道夫（rollback_dangling_for_context）由 api_server 中间件在
**事件循环线程**调用，只看得到那个线程的 threading.local；而 add/search/update/delete
都是同步 def，FastAPI 放在 anyio 工作线程执行，连接缓存在工作线程。于是清道夫对
同步路由一个连接也看不到，请求 X「INSERT 后抛异常」留下的半截写，会被同线程下一个
无关请求 Y 的 commit() 一起提交。

这里用真中间件（api_server._record_http_outcome）+ 真 anyio 线程池复现：
没有借出时回滚，第一条用例红；async 路由那条是对照（清道夫本来就够得着）。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import asynccontextmanager

import anyio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _rows(db: str) -> list[str]:
    with sqlite3.connect(db) as c:
        return [r[0] for r in c.execute("SELECT v FROM t ORDER BY rowid")]


@pytest.fixture()
def probe(tmp_path):
    import api_server
    from ducky import utils

    db = str(tmp_path / "tx_probe.db")
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE t(v TEXT)")

    @asynccontextmanager
    async def _one_worker(_app):
        # 单工作线程：保证两个同步请求落在同一线程、借到同一条线程本地连接
        anyio.to_thread.current_default_thread_limiter().total_tokens = 1
        yield

    app = FastAPI(lifespan=_one_worker)
    app.middleware("http")(api_server._record_http_outcome)
    seen: dict[str, int] = {}

    @app.post("/sync_fail")
    def sync_fail():
        seen["fail"] = threading.get_ident()
        utils._get_thread_conn(db).execute("INSERT INTO t VALUES ('half-write')")
        raise RuntimeError("boom after insert")

    @app.post("/sync_ok")
    def sync_ok():
        seen["ok"] = threading.get_ident()
        conn = utils._get_thread_conn(db)
        conn.execute("INSERT INTO t VALUES ('clean')")
        conn.commit()
        return {}

    @app.post("/async_fail")
    async def async_fail():
        utils._get_thread_conn(db).execute("INSERT INTO t VALUES ('half-write-async')")
        raise RuntimeError("boom after insert")

    @app.post("/async_ok")
    async def async_ok():
        conn = utils._get_thread_conn(db)
        conn.execute("INSERT INTO t VALUES ('clean-async')")
        conn.commit()
        return {}

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, db, seen


def test_sync_route_half_write_is_not_committed_by_next_request(probe):
    client, db, seen = probe
    assert client.post("/sync_fail").status_code == 500
    assert client.post("/sync_ok").status_code == 200
    assert seen["fail"] == seen["ok"], "两个请求没落在同一工作线程，本用例失去区分力"
    assert _rows(db) == ["clean"], "上一个请求的半截写被下一个请求的 commit 一起提交了"


def test_async_route_half_write_is_rolled_back_by_janitor(probe):
    """对照：async 路由的连接在事件循环线程，请求收尾清道夫本来就能回滚。"""
    client, db, _ = probe
    assert client.post("/async_fail").status_code == 500
    assert client.post("/async_ok").status_code == 200
    assert _rows(db) == ["clean-async"]
