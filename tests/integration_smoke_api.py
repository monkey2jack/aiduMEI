#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# aiduMEM API 烟雾测试
# 2026-06-14
#
# 用途: 跑 5 个核心端点的冒烟测试，验证升级前后接口正常
# 调用: python3 tests/integration_smoke_api.py（需 API 服务已启动）
# 设计: 零外部依赖（不依赖 pytest），纯 stdlib + requests
# 风格: 每个测试打印 emoji + 耗时（ms），末尾统计通过率
# ============================================================================

"""
注意:
- 任务清单说「POST /facts/search?query=user」, 但实际 api_server.py 里
  /facts/search 是 GET（api_server.py:462）。本测试按实际 API 写（GET），
  跟生产对齐。如果以后 api_server.py 改了，本测试要同步改。
- 5 个端点:
    1. GET  /health
    2. GET  /stats
    3. GET  /facts/categories
    4. GET  /facts?category=user
    5. GET  /facts/search?query=user
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from typing import Tuple, Optional

# --- 路径常量 ---------------------------------------------------------------
API_BASE = os.environ.get("AIDUMEM_API_BASE", "http://127.0.0.1:8767").rstrip("/")
TIMEOUT = 10  # 秒

# --- 统计 --------------------------------------------------------------------
PASS = 0
FAIL = 0
RESULTS = []  # [(emoji, name, detail)]


def _http(method: str, path: str, body: Optional[dict] = None) -> Tuple[int, dict, int]:
    """
    简单的 HTTP 调用，返回 (status_code, json_body, elapsed_ms)
    用 urllib 而不是 requests — 避免在没装 requests 的 venv 里炸
    """
    url = f"{API_BASE}{path}"
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            code = resp.getcode()
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace") if e.fp else ""
        code = e.code
    except Exception as e:
        raw = f"<<请求失败: {e}>>"
        code = 0
    elapsed_ms = int((time.time() - t0) * 1000)

    # 尝试解析 JSON
    try:
        body_json = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        body_json = {"_raw": raw[:200]}

    return code, body_json, elapsed_ms


def _check(condition: bool, name: str, detail: str = "") -> bool:
    """打印一个测试结果并累计计数"""
    global PASS, FAIL
    if condition:
        print(f"  ✅ PASS  {name}  ({detail})")
        PASS += 1
        RESULTS.append(("✅", name, detail))
    else:
        print(f"  ❌ FAIL  {name}  ({detail})")
        FAIL += 1
        RESULTS.append(("❌", name, detail))
    return condition


# ============================================================================
# 5 个端点测试
# ============================================================================

def test_01_health() -> bool:
    """1. GET /health → 200 + status=ok"""
    print("\n────────────────────────────────────────")
    print("🧪 测试 1/5: GET /health")
    code, body, ms = _http("GET", "/health")
    ok = _check(
        code == 200 and body.get("status") == "ok",
        "/health",
        f"{code} in {ms}ms, status={body.get('status')!r}"
    )
    if not ok:
        print(f"     响应: {body}")
    return ok


def test_02_stats() -> bool:
    """2. GET /stats → 200 + total_memories >= 0"""
    print("\n────────────────────────────────────────")
    print("🧪 测试 2/5: GET /stats")
    code, body, ms = _http("GET", "/stats")
    total = body.get("total_memories")
    if total is None:
        total = body.get("total")
    ok = _check(
        code == 200 and isinstance(total, int) and total >= 0,
        "/stats",
        f"{code} in {ms}ms, total_memories={total}"
    )
    if ok:
        print(f"     user_id: {body.get('user_id')}, unique_hashes: {body.get('unique_hashes')}")
    else:
        print(f"     响应: {body}")
    return ok


def test_03_categories() -> bool:
    """3. GET /facts/categories → 200 + 至少 1 个 category"""
    print("\n────────────────────────────────────────")
    print("🧪 测试 3/5: GET /facts/categories")
    code, body, ms = _http("GET", "/facts/categories")
    cats = body.get("categories", [])
    ok = _check(
        code == 200 and isinstance(cats, list) and len(cats) >= 1,
        "/facts/categories",
        f"{code} in {ms}ms, 共 {len(cats)} 个 category"
    )
    if ok:
        cat_names = [c.get("category", "?") for c in cats]
        print(f"     category 列表: {cat_names[:5]}{'...' if len(cat_names) > 5 else ''}")
    else:
        print(f"     响应: {body}")
    return ok


def test_04_list_facts() -> bool:
    """4. GET /facts?category=user → 200 + list"""
    print("\n────────────────────────────────────────")
    print("🧪 测试 4/5: GET /facts?category=user")
    cat_encoded = urllib.parse.quote("user")
    code, body, ms = _http("GET", f"/facts?category={cat_encoded}")
    facts = body.get("facts", [])
    ok = _check(
        code == 200 and isinstance(facts, list),
        "/facts?category=user",
        f"{code} in {ms}ms, count={body.get('count')}, 返回 list"
    )
    if ok:
        if facts:
            sample = facts[0]
            print(f"     示例: key={sample.get('fact_key')!r}, value={sample.get('fact_value', '')[:40]!r}")
        else:
            print(f"     (user category 当前为空，但接口正常)")
    else:
        print(f"     响应: {body}")
    return ok


def test_05_search_facts() -> bool:
    """5. GET /facts/search?query=user → 200 + results list（可能空，但要正常返回）"""
    print("\n────────────────────────────────────────")
    print("🧪 测试 5/5: GET /facts/search?query=user")
    q_encoded = urllib.parse.quote("user")
    code, body, ms = _http("GET", f"/facts/search?query={q_encoded}&top_k=5")
    facts = body.get("facts", [])
    ok = _check(
        code == 200 and isinstance(facts, list),
        "/facts/search?query=user",
        f"{code} in {ms}ms, hits={len(facts)}（允许 0，但要正常返回 list）"
    )
    if ok:
        if facts:
            for f in facts[:3]:
                print(f"     命中: [{f.get('category')}/{f.get('fact_key')}] {f.get('fact_value', '')[:40]}")
        else:
            print(f"     (无命中，但 search 接口正常)")
    else:
        print(f"     响应: {body}")
    return ok


# ============================================================================
# 主入口
# ============================================================================

def main() -> int:
    print("════════════════════════════════════════")
    print("🧪 aiduMEM API 烟雾测试")
    print(f"   target: {API_BASE}")
    print(f"   timeout: {TIMEOUT}s")
    print("════════════════════════════════════════")

    # 先做一次 connectivity 预检
    pre_code, _, pre_ms = _http("GET", "/health")
    if pre_code != 200:
        print(f"\n❌ 预检失败: GET /health → {pre_code} ({pre_ms}ms)")
        print(f"   请先启动 aidumem-api 服务:")
        print(f"     systemctl status aidumem-api")
        print(f"     # 或: cd <仓库根> && source venv/bin/activate && python3 api_server.py")
        return 1

    # 跑 5 个测试
    test_01_health()
    test_02_stats()
    test_03_categories()
    test_04_list_facts()
    test_05_search_facts()

    # 摘要
    total = PASS + FAIL
    rate = (PASS / total * 100) if total > 0 else 0
    print("\n════════════════════════════════════════")
    print("📊 测试摘要")
    print("════════════════════════════════════════")
    for emoji, name, detail in RESULTS:
        print(f"  {emoji} {name}  — {detail}")
    print("")
    print(f"  通过: {PASS} / {total}  ({rate:.0f}%)")
    print("")

    if FAIL == 0:
        print("✅ 全部通过，aidumem-api 健康")
        return 0
    else:
        print(f"❌ 有 {FAIL} 项失败，请查 api_server.py 日志: <仓库根>/logs/")
        return 1


if __name__ == "__main__":
    sys.exit(main())
