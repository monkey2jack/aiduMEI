#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ============================================================================
# aiduMEM API 烟雾测试
# 2026-06-14
#
# 用途: 跑 7 个核心端点的冒烟测试，验证升级前后接口正常
# 调用: python3 tests/integration_smoke_api.py（需 API 服务已启动）
# 设计: 零外部依赖（不依赖 pytest），纯 stdlib + requests
# 风格: 每个测试打印 emoji + 耗时（ms），末尾统计通过率
# ============================================================================

"""
注意:
- 任务清单说「POST /facts/search?query=user」, 但实际 api_server.py 里
  /facts/search 是 GET（api_server.py:462）。本测试按实际 API 写（GET），
  跟生产对齐。如果以后 api_server.py 改了，本测试要同步改。
- 7 个端点:
    1. GET  /health
    2. GET  /stats
    3. GET  /facts/categories
    4. GET  /facts?category=user
    5. GET  /facts/search?query=user
    6. POST /add            ← v20.0 新增，写链路（事故炸点，见 test_06 说明）
    7. POST /search         ← v20.0 新增，读链路
- 6、7 两条会**真的写库再读回**，用的是 __smoke__ 前缀的合成租户，
  跑完自删；清理走精确 /delete，且任何 /delete_all 都不传 confirm。
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from typing import Tuple, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ducky.utils import api_auth_headers  # noqa: E402

# --- 路径常量 ---------------------------------------------------------------
API_BASE = os.environ.get("AIDUMEM_API_BASE", "http://127.0.0.1:8767").rstrip("/")
TIMEOUT = 10  # 秒

# --- 统计 --------------------------------------------------------------------
PASS = 0
FAIL = 0
RESULTS = []  # [(emoji, name, detail)]


_AUTH_WARNED = False


def _warn_auth_once(code: int) -> None:
    """401/403 只提示一次，但必须提示。

    「4 条冒烟全 FAIL」和「4 条冒烟全 401」在排查方向上是两件事：
    前者查服务，后者查凭据。不说清楚就等着把时间花在错的那一头。
    """
    global _AUTH_WARNED
    if _AUTH_WARNED:
        return
    _AUTH_WARNED = True
    print(f"\n  ⚠️  auth_failed: 上游返回 {code} —— 服务端门禁已开，本脚本没取到有效 token。")
    print("      检查 AIDUMEM_API_TOKEN，或 AIDUMEM_ENV_FILE / ~/.aidumem/.env 里的凭据。")


def _http(method: str, path: str, body: Optional[dict] = None) -> Tuple[int, dict, int]:
    """
    简单的 HTTP 调用，返回 (status_code, json_body, elapsed_ms)
    用 urllib 而不是 requests — 避免在没装 requests 的 venv 里炸
    """
    url = f"{API_BASE}{path}"
    data = None
    # 门禁凭据走全仓同一条兜底链；未配 token 时是空 dict，本机零配置行为不变。
    # v19.4.2 之前这里裸奔：门禁一开，冒烟 5 条里有 4 条 401，
    # 而它打印的是「FAIL」而非「未授权」，很容易被读成「接口坏了」。
    headers = {"Accept": "application/json", **api_auth_headers()}
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
    if code in (401, 403):
        _warn_auth_once(code)

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
# 7 个端点测试
# ============================================================================

def test_01_health() -> bool:
    """1. GET /health → 200 + status=ok"""
    print("\n────────────────────────────────────────")
    print("🧪 测试 1/7: GET /health")
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
    print("🧪 测试 2/7: GET /stats")
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
    print("🧪 测试 3/7: GET /facts/categories")
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
    print("🧪 测试 4/7: GET /facts?category=user")
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
    print("🧪 测试 5/7: GET /facts/search?query=user")
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


# ── v20.0 新增：写—读闭环冒烟（合成租户，跑完自删） ──────────────────
# 名字要同时满足两条：不可能撞上任何真实租户，且一眼看得出是冒烟残留。
# 带 pid + 时间戳，是为了并发跑或反复跑时互不踩脚。
_SMOKE_PREFIX = "__smoke__"
_SMOKE_USER = f"{_SMOKE_PREFIX}{os.getpid()}_{int(time.time())}"
_SMOKE_BANK = "default"
_SMOKE_TEXT = "冒烟探针：这条是自动化写入的临时数据，可安全删除。"
_SMOKE_IDS: list = []      # test_06 写成功后把 memory_id 记在这，供清理精确删除


def test_06_add_write_path() -> bool:
    """6. POST /add → 2xx 且 status 非 error（写链路）

    **为什么这一条必须存在。**
    v19.4.3 → v20.0 之间，ducky/hot/add.py 里多写了一行函数作用域的
    `import json`，把模块级的 json 遮蔽成未绑定局部名，/add 连续 13 分钟
    返回 195 次 HTTP 500。而那段时间里 `systemctl is-active` 一直是 active，
    部署后冒烟也一路绿 —— 因为当时的冒烟只打 /health /stats /facts/*，
    **没有一条打过 /add**。进程活着不等于接口活着；/health 那条链路根本
    不碰 add.py，再怎么绿也照不到炸点。这一条打的就是那个函数本身。

    **为什么用 infer=false 仍然守得住。**
    infer 是 v20 的公开契约参数（ducky/api_models.py:AddRequest），
    false 表示跳过 LLM 抽取、原文规范化直写。它不削弱守卫强度：那次事故是
    **函数作用域的名字遮蔽**，整个函数体内 json 全程未绑定，走哪条分支都会
    炸，免抽取路径照样从头穿过 /add 的处理函数。换来的是冒烟不烧 token、
    不依赖外部 LLM 可用性、且「同输入同输出」可重复。
    代价说清楚：它不覆盖 LLM 抽取那一段的语义正确性——那段由 /reflect 等
    用例另行覆盖。这条冒烟负责的是「这个端点还活着吗」。

    判据故意放宽到「2xx 且 status 非 error」而不是断言回执结构：/add 的
    同步回执随 mem0 版本会变，把结构写死只会让冒烟因为无关变更变红，红多了
    人就会把它关掉。而事故当时是 500 —— 这条判据当场就红。
    """
    print("\n────────────────────────────────────────")
    print("🧪 测试 6/7: POST /add（写链路 · 合成租户 · infer=false）")
    body_req = {
        "messages": _SMOKE_TEXT,
        "user_id": _SMOKE_USER,
        "bank_id": _SMOKE_BANK,
        "infer": False,
        "metadata": {"source": "integration_smoke_api", "ephemeral": True},
    }
    code, body, ms = _http("POST", "/add", body_req)
    status = str(body.get("status", "")).lower() if isinstance(body, dict) else ""
    ok = _check(
        200 <= code < 300 and status != "error",
        "/add（写链路）",
        f"{code} in {ms}ms, status={status or '(未回 status)'}",
    )
    if ok:
        # 回执结构不固定，能捞到 id 就记下来供精确删除；捞不到也不判红，
        # 清理会退回到按租户清（且不传 confirm）。
        for key in ("results", "memories", "data"):
            items = body.get(key) if isinstance(body, dict) else None
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, dict):
                        mid = it.get("id") or it.get("memory_id")
                        if mid:
                            _SMOKE_IDS.append(str(mid))
        print(f"     写入租户: {_SMOKE_PREFIX}…（合成，跑完自删），可删 id={len(_SMOKE_IDS)} 条")
    else:
        print(f"     响应: {body}")
    return ok


def test_07_search_read_path() -> bool:
    """7. POST /search → 2xx（读链路）

    紧跟在 test_06 后面，构成「写进去 → 读回来」的闭环。
    同样不断言必须命中：召回受向量后端、阈值、重排是否在线影响，
    要求「必须搜到刚写的那条」会让冒烟在一堆无关配置下变红。
    这里断言的是**端点本身可用**——事故当时 /search 若同样被波及，
    这条会立刻红；而召回质量由 benchmarks 那套单独负责。
    """
    print("\n────────────────────────────────────────")
    print("🧪 测试 7/7: POST /search（读链路 · 合成租户）")
    code, body, ms = _http("POST", "/search", {
        "query": "冒烟探针",
        "user_id": _SMOKE_USER,
        "bank_id": _SMOKE_BANK,
        "limit": 5,
    })
    results = body.get("results", []) if isinstance(body, dict) else []
    hits = len(results) if isinstance(results, list) else 0
    ok = _check(
        200 <= code < 300,
        "/search（读链路）",
        f"{code} in {ms}ms, hits={hits}（允许 0，端点可用即通过）",
    )
    if not ok:
        print(f"     响应: {body}")
    return ok


def _smoke_cleanup() -> None:
    """删掉 test_06 写进去的数据。

    /delete_all 是破坏性接口，所以这里叠三重护栏：
      1. 本地断言租户名带 __smoke__ 前缀，不带就一个请求都不发；
      2. 优先走精确 /delete 按 memory_id 删——爆炸半径就是自己刚写的那几条；
      3. 万不得已按租户清时**不传 confirm**。这一条是关键：万一前两道被
         将来的人改坏、租户名意外归一成了默认租户，服务端那道
         「清默认库必须 confirm」的护栏（ducky/hot/crud.py:/delete_all）
         会返回 400 把请求挡回来，而不是真的清库。
         也就是说，这里故意**不**给自己开二次确认的权限。

    清理失败只告警、不计入 PASS/FAIL：冒烟的职责是回答「接口还活着吗」，
    不是「清干净了吗」。让清理失败去染红部署门禁，最后只会逼着人把整条
    冒烟关掉——那才是真正的损失。
    """
    if not _SMOKE_USER.startswith(_SMOKE_PREFIX):
        print(f"\n⚠️  清理已跳过：租户名不带 {_SMOKE_PREFIX} 前缀，拒绝发删除请求")
        return

    removed, failed = 0, 0
    for mid in _SMOKE_IDS:
        code, _, _ = _http("POST", "/delete", {
            "memory_id": mid,
            "user_id": _SMOKE_USER,
            "bank_id": _SMOKE_BANK,
        })
        if 200 <= code < 300:
            removed += 1
        else:
            failed += 1

    if not _SMOKE_IDS:
        # 没捞到 id，按租户清。注意：不传 confirm，见本函数 docstring 第 3 条。
        code, _, _ = _http("POST", "/delete_all", {
            "user_id": _SMOKE_USER,
            "bank_id": _SMOKE_BANK,
        })
        if 200 <= code < 300:
            print(f"\n🧹 清理完成：已按合成租户清空（未传 confirm）")
            return
        failed += 1

    if failed:
        print(f"\n⚠️  清理未尽：成功 {removed} / 失败 {failed}，残留在 {_SMOKE_PREFIX}… 租户下，不影响生产数据")
    else:
        print(f"\n🧹 清理完成：已删除 {removed} 条冒烟数据")


# ============================================================================
# 主入口
# ============================================================================

def main() -> int:
    print("════════════════════════════════════════")
    print("🧪 aiduMEI API 烟雾测试")
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

    # 跑 7 个测试
    test_01_health()
    test_02_stats()
    test_03_categories()
    test_04_list_facts()
    test_05_search_facts()
    # 6、7 是 v20.0 新增的写—读闭环：真请求打 /add 和 /search。
    # 加它们的直接原因是那次 13 分钟 / 195 次 500 的 /add 事故——当时的冒烟
    # 一条都没打到炸点。清理放在 finally 里：即便断言失败也要把合成租户的
    # 残留删掉，不然反复跑会在库里越堆越多。
    try:
        test_06_add_write_path()
        test_07_search_read_path()
    finally:
        _smoke_cleanup()

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
