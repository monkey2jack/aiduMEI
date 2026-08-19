#!/usr/bin/env python3
"""
aiduMEM 性能基线
跑 50 问句 × L0/L1/L2 × search/inject-context
输出：token 节省率、search/inject 延迟 P50/P95
零外部依赖

问句集为通用中性样本；如需贴合自己的语料，
把 AIDUMEM_PERF_QUERIES 指向一个每行一句的文本文件即可。
"""
import json
import statistics
import sys
import time
import urllib.request
import urllib.parse
from collections import defaultdict

import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ducky.utils import api_auth_headers  # noqa: E402

BASE = os.environ.get("AIDUMEM_API_BASE", "http://127.0.0.1:8767").rstrip("/")
TIMEOUT = int(os.environ.get("AIDUMEM_PERF_TIMEOUT", "10"))
BASE_DIR = os.environ.get("AIDUMEM_HOME") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE_DIR, "logs")

# 50 问句集：10 类 category × 5 问，纯中性样本（不含任何真实身份信息）
DEFAULT_QUERIES = [
    # 个人档案类
    "用户生日", "用户职业", "用户所在公司", "用户职位", "用户英文名",
    # 助手自身类
    "助手名称", "助手上线时间", "助手人格设定", "助手擅长什么", "助手是谁",
    # 约定/口令类
    "常用口令", "问候语", "专属称呼", "纪念日期", "约定事项",
    # 时间线类
    "第一次见面", "重要转折点", "去年这时候", "最近一次变更", "项目起点",
    # 运维类
    "服务配置", "API key 在哪", "接口地址", "冷却时间设置", "核心模块清单",
    # 关系类
    "家人信息", "同事", "重要联系人", "生日提醒", "家庭情况",
    # 工具类
    "工具箱", "常用脚本", "生图流程", "生视频流程", "自动化任务",
    # 内容/作品类
    "作品名称", "写作进度", "完稿时间", "故事主线", "章节结构",
    # 规范类
    "行为准则", "操作前检查", "权限级别", "执行协议", "硬性规则",
    # 项目配置类
    "项目名称", "服务面板", "部署域名", "服务器信息", "系统架构",
]


def _load_queries():
    """支持 AIDUMEM_PERF_QUERIES=/path/to/queries.txt 覆盖（每行一句）"""
    path = os.environ.get("AIDUMEM_PERF_QUERIES")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            custom = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
        if custom:
            return custom
    return DEFAULT_QUERIES


QUERIES = _load_queries()


def _request(method, path, params=None, data=None, headers=None):
    """唯一出口 —— 凭据在这里统一挂上。

    性能基线尤其经不起裸奔：401 的响应又小又快，跑出来的 P50/P95 会漂亮得反常，
    「基线」于是变成了一串测量门禁拒绝速度的数字。
    """
    qs = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
    url = f"{BASE}{path}?{qs}" if qs else f"{BASE}{path}"
    merged = {**(headers or {}), **api_auth_headers()}
    req = urllib.request.Request(url, data=data, method=method, headers=merged)
    return json.loads(urllib.request.urlopen(req, timeout=TIMEOUT).read())


def get(path, **params):
    return _request('GET', path, params=params)


def post_json(path, payload):
    return _request('POST', path, data=json.dumps(payload).encode(),
                    headers={'Content-Type': 'application/json'})


def percentile(data, p):
    if not data:
        return 0
    s = sorted(data)
    idx = int(len(s) * p / 100)
    return s[min(idx, len(s) - 1)]


print("="*70)
print(f"📊 aiduMEM 性能基线（{len(QUERIES)} 问句）")
print("="*70)
print()

# 1. /facts/search 延迟（按 level 分组）
print(f"🔍 Test 1: /facts/search 延迟（3 levels × {len(QUERIES)} queries）")
search_latency = defaultdict(list)
search_results = defaultdict(int)
for q in QUERIES:
    for level in ("L0", "L1", "L2"):
        t0 = time.time()
        d = get("/facts/search", query=q, level=level, top_k=5)
        elapsed_ms = (time.time() - t0) * 1000
        search_latency[level].append(elapsed_ms)
        search_results[level] += d.get('count', 0)

print()
print(f"  Level    P50(ms)   P95(ms)   总命中   平均命中/query")
for level in ("L0", "L1", "L2"):
    lat = search_latency[level]
    p50 = percentile(lat, 50)
    p95 = percentile(lat, 95)
    total_hits = search_results[level]
    avg = total_hits / len(QUERIES) if QUERIES else 0
    print(f"  {level:5}    {p50:7.1f}   {p95:7.1f}   {total_hits:5}    {avg:.2f}")

# 2. /facts/inject-context token 节省
print()
print()
print(f"💰 Test 2: /facts/inject-context token 节省（3 levels × {len(QUERIES)} queries）")
inject_tokens = defaultdict(list)
for q in QUERIES:
    for level in ("L0", "L1", "L2"):
        d = post_json("/facts/inject-context", {
            "query": q, "k": 5, "level": level, "max_tokens": 1500
        })
        inject_tokens[level].append(d.get('total_tokens', 0))

total_l0 = sum(inject_tokens['L0'])
total_l1 = sum(inject_tokens['L1'])
total_l2 = sum(inject_tokens['L2'])
print()
print(f"  Level    总 tokens    相对 L2 节省")
print(f"  L0       {total_l0:8}    {(1 - total_l0/max(total_l2,1))*100:5.1f}%")
print(f"  L1       {total_l1:8}    {(1 - total_l1/max(total_l2,1))*100:5.1f}%")
print(f"  L2       {total_l2:8}    —  baseline")

# 3. 目录递归 trajectory 分析
print()
print()
print("📈 Test 3: 目录递归 trajectory 分析（前 5 query）")
print()
print(f"  {'Query':25} {'intent_ms':10} {'position_ms':12} {'scanned':8} {'hits':6}")
for q in QUERIES[:5]:
    d = get("/facts/search", query=q, level="L0", top_k=3)
    traj = {s['step']: s for s in d['trajectory']}
    intent_ms = traj.get('intent_analysis', {}).get('elapsed_ms', 0)
    pos_ms = traj.get('position', {}).get('elapsed_ms', 0)
    scanned = traj.get('position', {}).get('scanned_facts', 0)
    hits = d.get('count', 0)
    print(f"  {q:25} {intent_ms:10} {pos_ms:12} {scanned:8} {hits:6}")

# 总结
print()
print("="*70)
print("📊 性能基线总结")
print("="*70)
print(f"  • /facts/search 平均延迟: P50 ~{percentile(search_latency['L0'], 50):.1f}ms / P95 ~{percentile(search_latency['L0'], 95):.1f}ms")
print(f"  • L0 模式平均节省: {(1 - total_l0/max(total_l2,1))*100:.1f}% token")
print(f"  • L1 模式平均节省: {(1 - total_l1/max(total_l2,1))*100:.1f}% token")
print(f"  • 全部问句总命中: L0={search_results['L0']} L1={search_results['L1']} L2={search_results['L2']}")
print()
print("💡 建议：每次升级后跑本脚本，对比基线（不下降 10% 即 OK）")
print()

# 输出基线数据为 JSON（供将来对比）
baseline = {
    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    "queries_count": len(QUERIES),
    "search_latency_ms": {
        level: {
            "p50": percentile(search_latency[level], 50),
            "p95": percentile(search_latency[level], 95),
            "avg": statistics.mean(search_latency[level]) if search_latency[level] else 0,
        }
        for level in ("L0", "L1", "L2")
    },
    "inject_tokens": {
        level: {
            "total": sum(inject_tokens[level]),
            "avg_per_query": sum(inject_tokens[level]) / len(QUERIES) if QUERIES else 0,
        }
        for level in ("L0", "L1", "L2")
    },
    "search_hits": dict(search_results),
    "token_saving_vs_L2": {
        "L0": f"{(1 - total_l0/max(total_l2,1))*100:.1f}%",
        "L1": f"{(1 - total_l1/max(total_l2,1))*100:.1f}%",
    },
}

os.makedirs(LOG_DIR, exist_ok=True)
_out = os.path.join(LOG_DIR, "perf_baseline.json")
with open(_out, "w", encoding="utf-8") as f:
    json.dump(baseline, f, ensure_ascii=False, indent=2)
print(f"📁 基线数据已存: {_out}")
