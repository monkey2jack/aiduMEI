"""
tests/test_persona_memory.py — v19.0 人格记忆基座（Persona Memory Layer）测试
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
覆盖：
  1. 三层结构落库与 version 递增
  2. 合成模式规则降级（LLM 不可用时不崩溃）
  3. 真实模式零幻觉护栏（无 source_ref 的条目被丢弃）
  4. 真实模式缺素材 / LLM 失败 → 拒绝构建（不降级为虚构）
  5. 情境化检索（不同情境召回不同记忆 + context 文本）
  6. 版本化回滚（数据不删，只切状态）
  7. 路由注册与 /persona/* 端点冒烟
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp_dir = tempfile.mkdtemp(prefix="aidumem_persona_test_")

import pytest  # noqa: E402

import ducky.utils as utils  # noqa: E402
import ducky.persona_memory as pm  # noqa: E402

# 把 persona 独立库指到临时目录，避免污染真实 data/
pm.PERSONA_DB = os.path.join(_tmp_dir, "persona.db")
pm._checked = False


@pytest.fixture(autouse=True)
def _fresh_db():
    import sqlite3
    pm._checked = False
    conn = sqlite3.connect(pm.PERSONA_DB)
    for table in ("persona_banks", "persona_memories"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()
    conn.close()
    pm.ensure_persona_schema()
    yield


# ── 1. 合成模式规则降级 + 三层落库 ─────────────────────────
def test_synthesis_rule_fallback_and_levels():
    res = pm.build_persona(
        "一名在生产环境做独立开发的技术顾问，喜欢围棋，常去咖啡馆写作。",
        mode="synthesis",
        persona_key="tech-advisor",
        persona_name="技术顾问",
        use_llm=False,  # 规则降级路径
    )
    assert res["status"] == "ok", res
    assert res["mode"] == "synthesis"
    assert res["version"] == 1
    assert res["llm_used"] is False
    assert res["counts"]["L"] >= 1
    assert res["counts"]["G"] >= 1

    detail = pm.get_bank_detail(res["bank_id"])
    assert detail["status"] == "ok"
    levels = {m["level"] for m in detail["memories"]}
    assert "L" in levels and "G" in levels
    # 合成模式的 provenance 必须是 synthesis
    assert all(m["provenance"] == "synthesis" for m in detail["memories"])


# ── 2. 版本递增 + 旧版 superseded ──────────────────────────
def test_version_increment_supersedes_old():
    r1 = pm.build_persona("第一版人设。", mode="synthesis", persona_key="ver", persona_name="V", use_llm=False)
    r2 = pm.build_persona("第二版人设，内容更丰富。", mode="synthesis", persona_key="ver", persona_name="V", use_llm=False)
    assert r1["version"] == 1
    assert r2["version"] == 2

    banks = pm.list_banks(persona_key="ver")
    ready = [b for b in banks if b["status"] == "ready"]
    superseded = [b for b in banks if b["status"] == "superseded"]
    assert len(ready) == 1 and ready[0]["version"] == 2
    assert len(superseded) == 1 and superseded[0]["version"] == 1


# ── 3. 真实模式零幻觉护栏 ──────────────────────────────────
def test_grounded_drops_items_without_source():
    # 直接注入一条带 source_refs 和一条不带 source_refs 的数据，
    # 绕过 LLM，验证 _store_memories 的硬校验。
    import sqlite3
    conn = sqlite3.connect(pm.PERSONA_DB)
    cur = conn.execute(
        "INSERT INTO persona_banks (persona_key, persona_name, mode, version, status) "
        "VALUES ('g-real', '真实', 'grounded', 1, 'ready')"
    )
    bank_id = int(cur.lastrowid or 0)
    conn.commit()
    conn.close()

    data = {
        "life_periods": [
            {"age_range": "20-30", "theme": "创业", "source_refs": ["[1]"]},
            {"age_range": "30-40", "theme": "无来源期"},  # 无 source_refs → 应被丢弃
        ],
        "general_events": [],
        "experiences": [],
    }
    counts = pm._store_memories(bank_id, data, mode="grounded")
    assert counts["L"] == 1  # 只有带 source 的那条被保留

    detail = pm.get_bank_detail(bank_id)
    assert len(detail["memories"]) == 1
    assert detail["memories"][0]["source_refs"] == ["[1]"]
    assert detail["memories"][0]["provenance"] == "grounded"


# ── 4. 真实模式缺素材 / LLM 失败 → 拒绝构建 ────────────────
def test_grounded_requires_material():
    res = pm.build_persona("", mode="grounded", persona_key="g-empty", use_llm=True)
    assert res["status"] == "error"
    assert "素材" in res["detail"]


def test_grounded_refuses_rule_fallback_on_llm_fail():
    # use_llm=False 且无真实素材 → grounded 必须拒绝，而不是编造
    res = pm.build_persona(
        "", mode="grounded", persona_key="g-no-llm",
        source_material="[1] 用户喜欢围棋", use_llm=False,
    )
    assert res["status"] == "error"


# ── 5. 情境化检索 ──────────────────────────────────────────
def test_retrieve_situation_specific():
    r = pm.build_persona(
        "喜欢围棋，常在西湖边下棋。也爱好烘焙，周末烤面包。",
        mode="synthesis", persona_key="situ", persona_name="情境测试", use_llm=False,
    )
    assert r["status"] == "ok"

    # 规则降级下 G 层会按句号拆出多条事件，检索应返回非空 context
    res = pm.retrieve_persona("围棋", persona_key="situ", k=3)
    assert res["status"] == "ok"
    assert res["count"] >= 1
    assert res["context"].startswith("[Persona Memory")

    # 不同情境命中不同事件，context 文本应不同
    res2 = pm.retrieve_persona("烘焙面包", persona_key="situ", k=3)
    assert res2["status"] == "ok"
    assert res["context"] != res2["context"]


# ── 6. 回滚 ────────────────────────────────────────────────
def test_rollback_switches_version():
    pm.build_persona("版本一内容。", mode="synthesis", persona_key="rb", persona_name="R", use_llm=False)
    pm.build_persona("版本二内容，更多细节。", mode="synthesis", persona_key="rb", persona_name="R", use_llm=False)

    rb = pm.rollback_persona("rb", to_version=1)
    assert rb["status"] == "ok"
    assert rb["restored_version"] == 1

    ready = [b for b in pm.list_banks(persona_key="rb") if b["status"] == "ready"]
    assert len(ready) == 1 and ready[0]["version"] == 1


# ── 7. 中文 key slugify 稳定性 ─────────────────────────────
def test_slugify_chinese_stable():
    a = pm._slugify("维护者")
    b = pm._slugify("维护者")
    assert a == b
    assert a.startswith("persona-")
    assert pm._slugify("tech-advisor") == "tech-advisor"


# ── 8. 路由注册 + 冒烟 ─────────────────────────────────────
def test_routes_registered_and_smoke():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ducky.routes_persona import register_persona_routes

    app = FastAPI()
    register_persona_routes(app)
    client = TestClient(app)

    # 合成模式规则降级（use_llm=False 不依赖真实 LLM）
    r = client.post("/persona/build", json={
        "persona_name": "路由冒烟",
        "persona_key": "route-smoke",
        "persona_card": "一名喜欢写作的独立开发者。",
        "mode": "synthesis",
        "use_llm": False,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok", body

    banks = client.get("/persona/banks", params={"persona_key": "route-smoke"}).json()
    assert banks["status"] == "ok"
    assert any(b["status"] == "ready" for b in banks["banks"])

    det = client.get("/persona/detail", params={"bank_id": body["bank_id"]}).json()
    assert det["status"] == "ok"

    ret = client.post("/persona/retrieve", json={
        "situation": "写作", "persona_key": "route-smoke", "k": 3,
    }).json()
    assert ret["status"] == "ok"

    ctx = client.get("/persona/context", params={
        "persona_key": "route-smoke", "situation": "写作",
    }).json()
    assert ctx["status"] == "ok" and ctx["context"]

    rb = client.post("/persona/rollback", json={
        "persona_key": "route-smoke", "to_version": 1,
    }).json()
    assert rb["status"] == "ok"
