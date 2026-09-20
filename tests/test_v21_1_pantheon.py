"""v21.1 众神殿：殿管理 + 跨殿借阅 —— 红→绿守卫。

覆盖：殿 CRUD（建/列/查/软删）、借阅 grant/check/revoke/过期 fail-closed/自借阅拒、
authorize_cross_hall 四态，以及**借阅在 core 搜索路径真生效**（recall_chain 跨殿无借阅
拒、有借阅通、不带 caller 主人直连通）——证明借阅非「写了没人读」的假闭环。
"""
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.mkdtemp(prefix="aidumem_v21_pantheon_")
_DB = os.path.join(_tmp, "facts.db")

import ducky.utils as utils  # noqa: E402
utils.FACTS_DB = _DB


@pytest.fixture(autouse=True)
def _db():
    utils.FACTS_DB = _DB
    c = sqlite3.connect(_DB)
    c.executescript(
        "DROP TABLE IF EXISTS pantheon_halls; DROP TABLE IF EXISTS hall_grants; "
        "DROP TABLE IF EXISTS facts; "
        # user_version 是 db 级持久：DROP 表后必须一并归零，否则 apply_migrations
        # 见旧 user_version=8 会跳过重建（共享 _DB 的状态污染）。
        "PRAGMA user_version=0; "
        "CREATE TABLE facts(id INTEGER PRIMARY KEY, user_id TEXT, bank_id TEXT);")
    from ducky.schema_bootstrap import apply_migrations
    apply_migrations(c)
    c.commit()
    c.close()
    yield


def test_hall_crud():
    from ducky import pantheon as p
    p.create_hall("athena", "雅典娜", "智慧殿")
    p.create_hall("zeus", "宙斯")
    assert {h["user_id"] for h in p.list_halls()} == {"athena", "zeus"}
    assert p.get_hall("athena")["display_name"] == "雅典娜"
    # 停用是软删：只熄灯不删殿、更不碰记忆（生产数据生命线）
    p.deactivate_hall("zeus")
    assert {h["user_id"] for h in p.list_halls()} == {"athena"}
    assert {h["user_id"] for h in p.list_halls(include_inactive=True)} == {"athena", "zeus"}


def test_grant_check_revoke():
    from ducky import pantheon as p
    assert p.check_hall_access("zeus", "athena", "read") is False   # 默认隔离
    assert p.check_hall_access("athena", "athena", "read") is True  # 本殿自访
    p.grant_hall_access("zeus", "athena", actions="read")
    assert p.check_hall_access("zeus", "athena", "read") is True
    assert p.check_hall_access("zeus", "athena", "export") is False  # action 隔离
    g = p.grant_hall_access("zeus", "hera", actions="read,export")
    assert p.check_hall_access("zeus", "hera", "export") is True
    p.revoke_hall_grant(g["grant_id"])
    assert p.check_hall_access("zeus", "hera", "read") is False      # 撤销即终态


def test_grant_expiry_fail_closed():
    from ducky import pantheon as p
    with pytest.raises(p.HallError):
        p.grant_hall_access("zeus", "athena", expires_at="2020-01-01T00:00:00")  # 过去时刻拒
    p.grant_hall_access("zeus", "athena", expires_at="2099-01-01T00:00:00")
    assert p.check_hall_access("zeus", "athena", "read") is True


def test_self_grant_and_illegal_action_rejected():
    from ducky import pantheon as p
    with pytest.raises(p.HallError):
        p.grant_hall_access("athena", "athena")            # 自借阅拒
    with pytest.raises(p.HallError):
        p.grant_hall_access("zeus", "athena", actions="write")  # 非法动作拒


def test_authorize_cross_hall_four_states():
    from ducky import pantheon as p
    assert p.authorize_cross_hall("zeus", "") is True          # caller 空=主人直连
    assert p.authorize_cross_hall("zeus", "zeus") is True      # 读自己殿
    with pytest.raises(p.HallError):
        p.authorize_cross_hall("zeus", "athena")              # 跨殿无借阅拒
    p.grant_hall_access("zeus", "athena", actions="read")
    assert p.authorize_cross_hall("zeus", "athena", action="read") is True  # 跨殿有借阅通


def test_pantheon_endpoints():
    """v22.0：grant 空 caller 被拒（管理面零匿名）。带 caller 才走通。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ducky.routes_pantheon import register_pantheon_routes
    app = FastAPI()
    register_pantheon_routes(app)
    client = TestClient(app)
    assert client.post("/pantheon/hall", params={"user_id": "athena", "display_name": "雅典娜"}).json()["status"] == "ok"
    client.post("/pantheon/hall", params={"user_id": "zeus"})
    assert any(h["user_id"] == "athena" for h in client.get("/pantheon/halls").json()["halls"])
    # v22.0：grant 空 caller → 403；带 caller=zeus（本人）→ ok
    r = client.post("/pantheon/grant", params={"grantor_user_id": "zeus", "grantee_user_id": "athena", "actions": "read"})
    assert r.json()["status"] == "error", "空 caller 必须被拒"
    r = client.post("/pantheon/grant", params={"grantor_user_id": "zeus", "grantee_user_id": "athena", "actions": "read", "caller": "zeus"})
    assert r.json()["status"] == "ok"
    # 自借阅经端点也被拦成 error dict（不 500）
    assert client.post("/pantheon/grant", params={"grantor_user_id": "x", "grantee_user_id": "x"}).json()["status"] == "error"


def test_pantheon_grant_negative_controls():
    """v22.0 负向对照：陌生人不能给他人发借阅。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ducky.routes_pantheon import register_pantheon_routes
    app = FastAPI()
    register_pantheon_routes(app)
    client = TestClient(app)
    client.post("/pantheon/hall", params={"user_id": "zeus"})
    client.post("/pantheon/hall", params={"user_id": "athena"})
    # 非本人非 admin 给他人发借阅 → 拒
    r = client.post("/pantheon/grant", params={"grantor_user_id": "zeus", "grantee_user_id": "athena", "caller": "stranger"})
    assert r.json()["status"] == "error", "陌生人不能代他人签发"
    # revoke 非 admin → 拒
    r = client.post("/pantheon/grant", params={"grantor_user_id": "zeus", "grantee_user_id": "athena", "caller": "zeus"})
    gid = r.json()["grant"]["grant_id"]
    r = client.post(f"/pantheon/grant/{gid}/revoke", params={"caller": "athena"})
    assert r.json()["status"] == "error", "非 admin 不能 revoke"
    # list_grants 越域 → 拒
    r = client.get("/pantheon/grants", params={"user_id": "zeus", "caller": "athena"})
    assert r.json()["status"] == "error"


def test_recall_chain_cross_hall_gated(monkeypatch):
    """借阅在 core 搜索路径真生效——否则 check_hall_access 没人调=假闭环。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import ducky.routes_v8 as routes_v8

    class _Mem:
        def search(self, *a, **k):
            return {"results": []}
        def get_all(self, *a, **k):
            return {"results": []}

    monkeypatch.setattr(routes_v8, "get_memory", lambda: _Mem())
    app = FastAPI()
    routes_v8.register_v8_routes(app)
    client = TestClient(app)

    # athena 跨殿搜 zeus，无借阅 → 被借阅门挡下
    #
    # v21.2.0 审计整改轮**设计变更**：拒绝从 `{"status":"error"}`（HTTP 200）
    # 改为 **HTTP 403**。原来的形态把「你没有借阅」和「服务端出故障」混成
    # 同一个响应 —— 调用方的重试逻辑会一直重试一个永远不会成功的请求。
    r = client.post("/recall_chain", json={"query": "x", "user_id": "zeus", "caller_user_id": "athena"})
    assert r.status_code == 403, f"授权拒绝必须是 403，实得 {r.status_code}"
    assert "借阅" in r.json()["detail"]
    # 负向对照：故障仍走 200 + status:error，两者不许再混为一谈
    assert "status" not in r.json(), "403 响应体不该再带业务态 status 字段"
    # 建借阅后 → 放行
    from ducky import pantheon as p
    p.grant_hall_access("zeus", "athena", actions="read")
    assert client.post("/recall_chain", json={"query": "x", "user_id": "zeus", "caller_user_id": "athena"}).json()["status"] == "ok"
    # 不带 caller（主人直连）→ 放行
    assert client.post("/recall_chain", json={"query": "x", "user_id": "zeus"}).json()["status"] == "ok"
