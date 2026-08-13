#!/usr/bin/env python3
"""
tests/smoke_p0_local.py — v19.0 P0 本地冒烟（无需真实 LLM / Qdrant / 网络）

验证目标：
  1. api_server 应用能组装并暴露 v19.0 P0 路由；
  2. /reflect 在 LLM 不可用时优雅降级为空洞察（不 500）；
  3. /reflect/list、/self-edit/edits 可访问；
  4. P0-1 facts 时间过滤 SQL 级语义正确；
  5. SearchRequest 能解析 before/after（P0-4 入参）。

跑法：cd <仓库根> && .venv/bin/python tests/smoke_p0_local.py
"""
from __future__ import annotations

import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# 必须在 import api_server 前指向临时数据目录，避免碰生产库
_tmp = tempfile.mkdtemp(prefix="aidumem_smoke_")
os.environ["AIDUMEM_DATA_DIR"] = _tmp
os.environ["AIDUMEM_REFLECT_ENABLED"] = "false"  # 冒烟阶段不起后台循环
os.environ["AIDUMEM_SELF_EDIT_ENABLED"] = "false"

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'✅' if cond else '❌'} {name}{(' — ' + detail) if detail else ''}")


def main() -> int:
    import api_server  # noqa: F401  (组装 app + 注册全部路由)

    # 冒烟环境没有启动 _start_background，核心表需要手动建好，
    # 否则 /facts/search 会报 no such table: facts。
    from ducky.schema_bootstrap import ensure_core_schema
    ensure_core_schema()

    from fastapi.testclient import TestClient

    client = TestClient(api_server.app)

    # 1. /reflect POST（JSON body 路径，验证必-1 修复）
    r = client.post("/reflect", json={"user_id": "default", "top_k": 5, "save": False})
    check("POST /reflect 返回 200", r.status_code == 200, f"status={r.status_code}")
    data = r.json()
    check("POST /reflect 降级为 ok + 空洞察", data.get("status") == "ok" and data.get("llm_used") is False,
          f"status={data.get('status')} llm_used={data.get('llm_used')}")
    check("POST /reflect 不抛异常", "detail" not in data or data.get("status") == "ok")

    # 2. /reflect/list
    r = client.get("/reflect/list?user_id=default&limit=5")
    check("GET /reflect/list 返回 200", r.status_code == 200, f"status={r.status_code}")
    check("GET /reflect/list 结构正确", r.json().get("status") == "ok")

    # 3. /reflect/context
    r = client.get("/reflect/context?user_id=default&limit=3")
    check("GET /reflect/context 返回 200", r.status_code == 200, f"status={r.status_code}")

    # 4. /self-edit/edits
    r = client.get("/self-edit/edits?user_id=default&limit=5")
    check("GET /self-edit/edits 返回 200", r.status_code == 200, f"status={r.status_code}")
    check("GET /self-edit/edits 结构正确", r.json().get("status") == "ok")

    # 5. /self-edit/rollback（不存在的 id，应返回 error 结构而非 500）
    r = client.post("/self-edit/rollback", json={"edit_id": 99999})
    check("POST /self-edit/rollback 返回 200", r.status_code == 200, f"status={r.status_code}")
    check("POST /self-edit/rollback 错误语义", r.json().get("status") == "error")

    # 6. /facts/search 时间过滤（P0-1 SQL 级语义）
    r = client.get("/facts/search", params={"query": "偏好", "after": "2026-06", "top_k": 10})
    check("GET /facts/search 返回 200", r.status_code == 200, f"status={r.status_code}")
    check("GET /facts/search 带 time_filter", r.json().get("time_filter") == {"before": "", "after": "2026-06-01"})

    # 7. SearchRequest before/after 解析（P0-4 入参）
    from ducky.api_models import SearchRequest
    req = SearchRequest(query="测试", before="2026-01", after="2025-06")
    check("SearchRequest 解析 before/after", req.before == "2026-01" and req.after == "2025-06")

    # 8. 修复后的 routes_p0 请求模型可被 OpenAPI 正确描述
    openapi = api_server.app.openapi()
    reflect_paths = [p for p in openapi["paths"] if "reflect" in p]
    check("OpenAPI 暴露 reflect 路由", len(reflect_paths) >= 3, f"paths={reflect_paths}")

    # 9. P1 路由冒烟：类型统计 + skill 草稿 + 递归精炼
    r = client.get("/memory/types")
    check("GET /memory/types 返回 200", r.status_code == 200, f"status={r.status_code}")
    r = client.post("/skill/grow", json={
        "trajectory": ["检查状态", "备份数据", "执行升级", "验证健康", "回滚预案"],
        "task_name": "smoke",
        "use_llm": False,
    })
    check("POST /skill/grow 规则降级生成草稿", r.status_code == 200 and r.json().get("status") == "ok",
          f"status={r.status_code} body={r.json().get('status')}")
    r = client.get("/skill/drafts")
    check("GET /skill/drafts 返回 200", r.status_code == 200, f"status={r.status_code}")
    r = client.get("/memory/refinements")
    check("GET /memory/refinements 返回 200", r.status_code == 200, f"status={r.status_code}")

    print(f"\n冒烟结果：{len(PASS)} passed / {len(FAIL)} failed")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
