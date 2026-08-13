"""
tests/test_audit_fixes_v19_1_1.py — v19.1.1 audit patch regression tests
Lock down the P0/P1/P2 fixes from the two-source audit:
  P0-2  UpdateRequest accepts legacy `data` without wiping content
  P0-3  delete_all takes user_id from a body model (not query scalar)
  P2-1  SearchRequest accepts top_k so MCP callers are no longer ignored
  P2-3  memory type annotation surfaces on search results
  P3-3  _normalize_user_id no longer hardcodes admin/user mapping
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import ducky.utils as utils

_tmp = tempfile.mkdtemp(prefix="aidumem_audit_19_1_1_")
_TEST_DB = os.path.join(_tmp, "facts.db")
utils.FACTS_DB = _TEST_DB


@pytest.fixture(autouse=True)
def _bind_db():
    utils.FACTS_DB = _TEST_DB
    yield


# ── P0-2: UpdateRequest legacy data must not be dropped ───────────────
def test_update_request_keeps_legacy_data():
    from ducky.api_models import UpdateRequest

    req = UpdateRequest(memory_id="m1", data="新的记忆内容")
    extra = getattr(req, "model_extra", None) or {}
    assert extra.get("data") == "新的记忆内容", "data 字段应被 extra=allow 保留"

    # REST /update 的回退逻辑：content 为空时读 data，绝不更新成空
    content = req.content or extra.get("data", "")
    assert content == "新的记忆内容"


# ── P0-3: delete_all consumes a body model ────────────────────────────
def test_delete_all_uses_body_model():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from ducky.api_models import DeleteAllRequest

    app = FastAPI()
    seen = {}

    @app.post("/delete_all")
    def delete_all(req: DeleteAllRequest):
        seen["user_id"] = req.user_id
        return {"status": "ok"}

    client = TestClient(app)
    r = client.post("/delete_all", json={"user_id": "agentA"})
    assert r.status_code == 200
    assert seen["user_id"] == "agentA", "body 里的 user_id 必须被读到"


# ── P2-1: SearchRequest top_k is explicit and search uses it ──────────
def test_search_request_accepts_top_k():
    from ducky.api_models import SearchRequest

    req = SearchRequest(query="q", top_k=3)
    assert req.top_k == 3
    effective = req.top_k if req.top_k and req.top_k > 0 else req.limit
    assert effective == 3


# ── P2-3: memory type annotation helper ───────────────────────────────
def test_annotate_memory_types():
    import ducky.hot.search as hs

    from ducky.memory_types import classify_and_record, ensure_memory_types_schema

    ensure_memory_types_schema()
    classify_and_record("fact:1", "用户偏好 Python", use_llm=False)

    results = [
        {"id": "uuid-1", "metadata": {"fact_id": 1}},
        {"id": "uuid-2", "metadata": {}},
        {"id": "uuid-3"},
        "not-a-dict",
    ]
    # 分类账本可能为空；只验证 helper 对非 dict 元素不崩、dict 元素有字段
    hs._annotate_memory_types(results)
    for item in results[:3]:
        assert "memory_type" in item
    assert results[3] == "not-a-dict"
    # 自审发现：fact_id 应优先命中账本（fact:{id}），不是拿 UUID 空查
    assert results[0]["memory_type"] == "PREFERENCES"
    assert results[1]["memory_type"] == "FACTS"


# ── P3-3: _normalize_user_id no hardcoded admin/user mapping ──────────
def test_normalize_user_id_no_hardcoded_legacy():
    import ducky.mem0_runtime as mr

    # 默认空环境：admin/user 不应再被吞进 default
    os.environ.pop("AIDUMEM_LEGACY_USER_IDS", None)
    assert mr._normalize_user_id("admin") == "admin"
    assert mr._normalize_user_id("user") == "user"

    # 部署方显式声明历史映射后才映射
    os.environ["AIDUMEM_LEGACY_USER_IDS"] = "admin,user"
    assert mr._normalize_user_id("admin") == "default"
    assert mr._normalize_user_id("user") == "default"
    os.environ.pop("AIDUMEM_LEGACY_USER_IDS", None)
