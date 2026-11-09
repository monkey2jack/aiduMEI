"""f0.8 ③-10 收尾：/forget 进墓碑可恢复、墓碑元数据过 P7、键语境打码、路由钥匙检查普查（反向断言）。"""
from __future__ import annotations

import json
import uuid

from ducky import write_policy as wp

_SEED = ("abandon ability able about above absent absorb abstract absurd abuse access accident")  # BIP39 合成样例


# ── 打码：secret is X / mnemonic is X，值像秘密才打 ─────────────────────────

def test_secret_and_mnemonic_forms_redacted():
    for text in ("my secret is Xq9vR2mZ7pLk3", "secret is 12345678", "secret: 12345678",
                 f"mnemonic is {_SEED}", f"my wallet mnemonic: {_SEED}"):
        out, kinds = wp.redact_forbidden(text)
        assert kinds and "[redacted:" in out, text
        assert "Xq9vR2" not in out and "12345678" not in out and "absurd" not in out, out


def test_false_positive_guard_plain_words_not_redacted():
    for text in ("cookie: chocolate chip", "cookie is oatmeal raisin", "the secret is love",
                 "the secret is 42", "secret ingredient is cinnamon", "my mnemonic is lamp river",
                 "secret is a surprise party"):
        out, kinds = wp.redact_forbidden(text)
        assert not kinds and out == text, (text, out)


def test_metadata_redaction_is_key_aware():
    md = wp.redact_metadata({"secret": "Xq9vR2mZ7pLk3", "mnemonic": _SEED,
                             "session_cookie": "a8f3k2j9d0s8f7g6h5j4k3l2",
                             "cookie": "chocolate chip", "category": "pref", "fact_key": "favorite_color",
                             "nested": {"api_secret": "Zk82mQp0vX7c"}})
    # 修复前：值里没有键语境，三者原样落库
    assert md["secret"].startswith("[redacted:") and md["mnemonic"].startswith("[redacted:")
    assert md["session_cookie"].startswith("[redacted:") and md["nested"]["api_secret"].startswith("[redacted:")
    assert md["cookie"] == "chocolate chip" and md["category"] == "pref" and md["fact_key"] == "favorite_color"


def test_tombstone_snapshot_metadata_goes_through_p7():
    from ducky.tombstone import _p7_meta
    assert _p7_meta({"secret": "Xq9vR2mZ7pLk3", "category": "pref"}) == {
        "secret": "[redacted:credential]", "category": "pref"}


# ── /forget → 墓碑 → 恢复 ──────────────────────────────────────────────

def _client(monkeypatch):
    import api_server
    from fastapi.testclient import TestClient
    monkeypatch.setenv("AIDUMEM_API_TOKEN", "forget-f08-token")
    monkeypatch.setenv("AIDUMEI_ENGINE_MODE", "local")
    return TestClient(api_server.app, headers={"Authorization": "Bearer forget-f08-token"})


def _ids(c, u, q):
    r = c.post("/search", json={"query": q, "user_id": u, "caller_user_id": u})
    assert r.status_code == 200, r.text
    return [h["id"] for h in r.json()["results"]]


def test_forget_goes_to_tombstone_and_is_restorable(monkeypatch):
    c = _client(monkeypatch)
    u = f"fg-{uuid.uuid4().hex[:8]}"
    r = c.post("/add", json={"messages": [{"role": "user", "content": "synthetic: I keep bees on the roof"}],
                             "user_id": u, "caller_user_id": u, "infer": False,
                             "metadata": {"secret": "Xq9vR2mZ7pLk3"}})
    assert r.status_code == 200, r.text
    mid = _ids(c, u, "bees")[0]
    r = c.post("/api/forget", json={"user_id": u, "caller_user_id": u, "memory_ids": [mid]})
    assert r.status_code == 200, r.text
    row = r.json()["deleted"][0]
    assert row["status"] == "committed" and row["tombstone_id"], r.text
    assert _ids(c, u, "bees") == []
    tl = c.get("/api/tombstones", params={"user_id": u, "caller_user_id": u}).json()["results"]
    t = next(x for x in tl if x.get("tombstone_id") == row["tombstone_id"])
    assert t.get("reason") == "forget", t
    assert "Xq9vR2mZ7pLk3" not in json.dumps(tl)
    r = c.post("/api/tombstone/restore", json={"tombstone_id": row["tombstone_id"],
                                               "user_id": u, "caller_user_id": u})
    assert r.status_code == 200, r.text
    assert _ids(c, u, "bees"), "restored memory must be searchable again"


# ── 钥匙检查普查：所有业务路由都挂在 ScopeRegistrar 上 ─────────────────────

def test_every_business_route_carries_key_check():
    import api_server
    from starlette.routing import Mount
    allowed_unscoped = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc", "/",
                        "/api/openapi.json", "/api/docs", "/api/docs/oauth2-redirect", "/api/redoc",
                        "/api/login", "/api/login/hint"}
    missing = []

    def walk(app, prefix=""):
        for r in app.routes:
            if isinstance(r, Mount):
                if hasattr(getattr(r, "app", None), "routes"):
                    walk(r.app, prefix + r.path)
                continue
            path = prefix + getattr(r, "path", "")
            if path not in allowed_unscoped and not getattr(r.endpoint, "__aidumei_scoped__", False):
                missing.append(path)
    walk(api_server.app)
    assert missing == [], missing
