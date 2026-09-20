"""v22.0 雷霆审计 D4 / Qwen M-03 · 鉴权负向对照模板

新授权端点默认必须答「陌生人能给他人发借阅吗」——这是 v21.1 众神殿
零鉴权能溜过 7 个测试的原因：测了「门后的逻辑对不对」，没测「门在不在」。

本模板固化这条纪律：凡新增授权/管理端点，必须带一条「陌生人必拒」的
负向对照。改坏判据必须让测试红。
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

_tmp = tempfile.mkdtemp(prefix="aidumem_v22_authz_")
_DB = os.path.join(_tmp, "facts.db")

import ducky.utils as utils  # noqa: E402
utils.FACTS_DB = _DB


@pytest.fixture(autouse=True)
def _db():
    utils.FACTS_DB = _DB
    c = sqlite3.connect(_DB)
    c.executescript(
        "DROP TABLE IF EXISTS pantheon_halls; DROP TABLE IF EXISTS hall_grants; "
        "DROP TABLE IF EXISTS facts; PRAGMA user_version=0; "
        "CREATE TABLE facts(id INTEGER PRIMARY KEY, user_id TEXT, bank_id TEXT);")
    from ducky.schema_bootstrap import apply_migrations
    apply_migrations(c)
    c.commit()
    c.close()
    yield


class TestPantheonNegativeTemplate:
    """众神殿端点的负向对照（D1 已普查，本类是行为层模板）。"""

    def test_stranger_cannot_grant_for_others(self):
        """陌生人代他人签发借阅必须被拒。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from ducky.routes_pantheon import register_pantheon_routes
        app = FastAPI()
        register_pantheon_routes(app)
        c = TestClient(app)
        r = c.post("/pantheon/grant", params={
            "grantor_user_id": "victim", "grantee_user_id": "attacker",
            "caller": "attacker",  # 非本人非 admin
        })
        assert r.json()["status"] == "error", "陌生人代他人签发必须被拒"

    def test_stranger_cannot_revoke(self, monkeypatch):
        """非 admin 不能撤销他人借阅。"""
        monkeypatch.setenv("AIDUMEI_FEDERATION_ADMINS", "admin")
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from ducky.routes_pantheon import register_pantheon_routes
        app = FastAPI()
        register_pantheon_routes(app)
        c = TestClient(app)
        # 先由 admin 签发（caller=admin 在 AIDUMEI_FEDERATION_ADMINS 名单里）
        r = c.post("/pantheon/grant", params={
            "grantor_user_id": "alice", "grantee_user_id": "bob",
            "caller": "admin",
        })
        assert r.json()["status"] == "ok"
        gid = r.json()["grant"]["grant_id"]
        # 陌生人撤销
        r = c.post(f"/pantheon/grant/{gid}/revoke", params={"caller": "mallory"})
        assert r.json()["status"] == "error", "非 admin 不能撤销"

    def test_empty_caller_denied_on_management(self):
        """管理面空 caller 一律拒（v22.0 零匿名）。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from ducky.routes_pantheon import register_pantheon_routes
        app = FastAPI()
        register_pantheon_routes(app)
        c = TestClient(app)
        for ep in ["/pantheon/grant", "/pantheon/hall/x/deactivate"]:
            r = c.post(ep, params={"grantor_user_id": "a", "grantee_user_id": "b"})
            assert r.json()["status"] == "error", f"{ep} 空 caller 必须被拒"


class TestCallerBindingNegativeTemplate:
    """caller↔凭据绑定的负向对照（A2 已修，本类是模板）。"""

    def test_unregistered_token_denied_in_strict(self, monkeypatch):
        """strict 模式下未登记指纹必须 403。"""
        from fastapi import HTTPException
        monkeypatch.setenv("AIDUMEI_CALLER_BINDINGS", '{"fp1": ["agent_a"]}')
        monkeypatch.setenv("AIDUMEI_CALLER_BINDING_MODE", "strict")
        import ducky.security.auth as auth_mod
        monkeypatch.setattr(auth_mod, "current_request_token_fingerprint",
                            lambda: "unregistered", raising=False)
        from ducky.federation.routes import _enforce_caller_binding
        with pytest.raises(HTTPException) as ei:
            _enforce_caller_binding("agent_b", "test")
        assert ei.value.status_code == 403
