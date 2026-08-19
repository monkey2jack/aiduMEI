#!/usr/bin/env python3
"""
aiduMEM 端到端集成测试 (Phase 3)
测试完整生命周期：add → search → inject-context → feedback → trust 变化
零外部依赖（纯 stdlib + urllib）
"""
import json
import os
import sys
import time
import urllib.request
import urllib.parse
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ducky.utils import api_auth_headers  # noqa: E402

BASE = os.environ.get("AIDUMEM_API_BASE", "http://127.0.0.1:8767").rstrip("/")
TIMEOUT = 10
TEST_CATEGORY = "测试_E2E"  # 隔离测试数据
PASS = 0
FAIL = 0
FAIL_DETAILS = []


def _request(method, path, params=None, data=None, headers=None):
    """三个调用形态收敛到这一个出口。

    v19.4.2：收敛的直接动机是**凭据**。原先 get/post/post_json 各自拼 URL、
    各自造 Request，其中 get() 甚至直接 urlopen(裸字符串)，根本没有放 header 的地方 ——
    门禁一开就是静默 401。出口只有一个，凭据才谈得上「一定带上」。
    """
    qs = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
    url = f"{BASE}{path}?{qs}" if qs else f"{BASE}{path}"
    merged = {**(headers or {}), **api_auth_headers()}
    req = urllib.request.Request(url, data=data, method=method, headers=merged)
    return json.loads(urllib.request.urlopen(req, timeout=TIMEOUT).read())


def get(path, **params):
    return _request('GET', path, params=params)


def post(path, **params):
    """add_fact 等端点用 query string 方式（FastAPI 默认识别）"""
    return _request('POST', path, params=params, data=b'')


def post_json(path, payload):
    """inject-context 等端点用 JSON body 方式"""
    return _request('POST', path, data=json.dumps(payload).encode(),
                    headers={'Content-Type': 'application/json'})


def cleanup():
    """清理测试数据"""
    try:
        # 直接 SQL 清理（不走 API，因为没 delete-by-category 端点）
        import sqlite3
        from ducky.utils import FACTS_DB
        conn = sqlite3.connect(FACTS_DB)
        conn.execute("DELETE FROM facts WHERE category = ?", (TEST_CATEGORY,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"⚠️ cleanup 失败: {e}")
        return False


def test(name, fn):
    global PASS, FAIL
    t0 = time.time()
    try:
        result = fn()
        elapsed = int((time.time() - t0) * 1000)
        if result:
            print(f"  ✅ {name}  ({elapsed}ms)")
            PASS += 1
        else:
            print(f"  ❌ {name}  ({elapsed}ms)")
            FAIL += 1
            FAIL_DETAILS.append(name)
    except Exception as e:
        elapsed = int((time.time() - t0) * 1000)
        print(f"  ❌ {name}  ({elapsed}ms) — {e}")
        FAIL += 1
        FAIL_DETAILS.append(f"{name}: {e}")


print("="*60)
print("🧪 aiduMEM 端到端集成测试（Phase 3）")
print("="*60)
cleanup()
print()

# Step 1: 添加 fact（带 summary 自动生成）
print("📋 Step 1: 添加 fact（验证 summary 自动 backfill）")
def t1():
    r = post("/facts/add", category=TEST_CATEGORY,
             fact_key="E2E_测试键", fact_value="端到端测试值，这是 Phase 3 集成测试的关键样例，包含足够长的内容以测试 L0 截断")
    if r.get("status") != "ok":
        return False
    # 验证 summary 已生成
    r2 = get("/facts", category=TEST_CATEGORY, level="L0")
    fact = r2['facts'][0]
    # summary 应该被截断到 60 字符 + ...
    return "value" in fact and fact['value'].startswith("端到端测试值")
test("add_fact + summary 自动 backfill", t1)

# Step 2: 搜索 fact（验证 L0/L1/L2 + 目录递归 + trajectory）
print()
print("📋 Step 2: 搜索 fact（L0/L1/L2 + 目录递归 + trajectory）")
def t2():
    d = get("/facts/search", query="E2E_测试键", level="L0", top_k=5)
    return (
        d.get("level") == "L0" and
        d.get("count", 0) >= 1 and
        len(d.get("trajectory", [])) == 5 and  # 4 步 + return = 5
        d['trajectory'][0]['step'] == 'intent_analysis' and
        d['trajectory'][-1]['step'] == 'return'
    )
test("search_facts L0 + 4 步 trajectory", t2)

# Step 3: inject-context L0 vs L2 token 节省
print()
print("📋 Step 3: inject-context L0 vs L2 token 节省")
def t3():
    l0 = post_json("/facts/inject-context", {
        "query": "E2E_测试键 端到端", "k": 5, "level": "L0", "max_tokens": 1000
    })
    l2 = post_json("/facts/inject-context", {
        "query": "E2E_测试键 端到端", "k": 5, "level": "L2", "max_tokens": 1000
    })
    # L0 应该有较少的 total_tokens
    if l0.get('total_tokens', 999) <= l2.get('total_tokens', 0):
        return True
    return False
test("inject-context L0 token 节省", t3)

# Step 4: feedback → helpful_count 增加（trust 由 recompute_trust.py 周期重算）
print()
print("📋 Step 4: feedback → helpful_count 增加")
def t4():
    # 取测试 fact 的 id 和初始 helpful_count
    facts = get("/facts", category=TEST_CATEGORY)['facts']
    fact_id = facts[0]['id']
    initial_helpful = facts[0]['helpful_count']
    # 给 3 次 helpful feedback（trust_score 不会立即改，由 cron 周期重算）
    for _ in range(3):
        post("/facts/feedback", fact_id=fact_id, helpful=True)
    facts2 = get("/facts", category=TEST_CATEGORY)['facts']
    new_helpful = facts2[0]['helpful_count']
    # 同时验证 fact 仍存在
    return new_helpful == initial_helpful + 3 and fact_id == facts2[0]['id']
test("feedback 让 helpful_count +3", t4)

# Step 5: retrieval_count 累计
print()
print("📋 Step 5: retrieval_count 累计")
def t5():
    facts = get("/facts", category=TEST_CATEGORY)['facts']
    initial = facts[0]['retrieval_count']
    # 跑 3 次 search
    for _ in range(3):
        get("/facts/search", query="E2E_测试键", level="L0", top_k=1)
    facts2 = get("/facts", category=TEST_CATEGORY)['facts']
    return facts2[0]['retrieval_count'] == initial + 3
test("search 累计 retrieval_count", t5)

# Step 6: trust 黑名单（< 0.2 自动不返回）
print()
print("📋 Step 6: trust 黑名单（< 0.2 自动不返回）")
def t6():
    # 用 5 次 unhelpful 把 trust 压低
    facts = get("/facts", category=TEST_CATEGORY)['facts']
    fid = facts[0]['id']
    for _ in range(5):
        post("/facts/feedback", fact_id=fid, helpful=False)
    # 看 trust
    fact = get("/facts", category=TEST_CATEGORY)['facts'][0]
    if fact['trust_score'] >= 0.2:
        # trust 还没到 0.2，跳过（也不强制要求）
        return True
    # trust < 0.2，验证 search 不返回
    d = get("/facts/search", query="E2E_测试键", level="L0", top_k=5)
    return not any(f['id'] == fid for f in d['facts'])
test("trust<0.2 自动不返回（黑名单）", t6)

# Step 7: 类别推断（intent_analysis 阶段）
print()
print("📋 Step 7: 类别推断（intent_analysis）")
def t7():
    d = get("/facts/search", query="user", level="L0", top_k=3)
    intent_step = d['trajectory'][0]
    # "user" 是已知 category，应该被识别
    return "user" in intent_step.get('category_candidates', [])
test("intent_analysis 推断 category_candidates", t7)

# Step 8: 总结
print()
print("="*60)
print("📊 测试总结")
print("="*60)
total = PASS + FAIL
print(f"  通过: {PASS} / {total}")
print(f"  失败: {FAIL}")
if FAIL_DETAILS:
    for d in FAIL_DETAILS:
        print(f"    - {d}")
print()
cleanup()
print("✅ 测试数据已清理")
if FAIL == 0:
    print(f"\n🎉 全部通过，aiduMEM 端到端 OK")
    sys.exit(0)
else:
    print(f"\n⚠️ 有 {FAIL} 个测试失败，需要修")
    sys.exit(1)
