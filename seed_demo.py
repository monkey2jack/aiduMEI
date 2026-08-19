#!/usr/bin/env python3
"""Seed script — fill local test aiduMEM with realistic synthetic demo data.
All data is fictional. No real memories, names, or production info.

Usage:
  python seed_demo.py
  AIDUMEM_API_BASE=http://127.0.0.1:8767 python seed_demo.py
"""
import os
import sys
import requests

# 仓库根补进 sys.path：本脚本常被从任意目录直接调用
_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# v19.4.2：demo 也要能在「开了鉴权门禁」的部署上跑通。
# 否则新用户照 README 敲第一条命令就是满屏 401——这是我们对外的第一印象。
# 未设 token 时返回空 dict，本机零配置体验完全不变。
from ducky.utils import api_auth_headers

API = os.environ.get("AIDUMEM_API_BASE", "http://127.0.0.1:8767").rstrip("/")

SEEDS = [
    # user default — 张伟（虚构产品经理）
    ("用户张伟是一个资深产品经理，在北京工作了八年，习惯每天早上喝一杯美式咖啡", "default"),
    ("张伟的公司叫明远科技，主营数据中台，团队规模 120 人左右", "default"),
    ("张伟最近在看微服务架构改造方案，评估 K8s 和 Nomad 的适用性", "default"),
    ("周末张伟喜欢去后海骑自行车，基本上是朝着东边日落的方向骑", "default"),
    ("张伟的笔记本是 M3 Pro 芯片，16GB 内存，跑 Docker 经常内存告急", "default"),
    ("明远科技决定把记忆中台作为下一个季度的产品方向，代号「记忆引擎」", "default"),
    ("张伟小时候在北京十一学校读过书，直到现在还对个平房的走廊有印象", "default"),
    ("张伟对产品设计的要求很高：克制、高级、美观，不能太花哨", "default"),
    # user aidu — aidu AI 助手（通用设定，不含真实部署信息）
    ("aidu is an AI assistant with persistent memory, deployed on a local server", "aidu"),
    ("aidu's memory backend is aiduMEI v18.3.0-zeus built on mem0ai SDK", "aidu"),
    ("aidu prefers concise responses and always double-checks credentials before actions", "aidu"),
    ("aidu's recall pipeline has five stages: candidate pool, ignition, dedup, time decay, final selection", "aidu"),
    ("aidu uses your-embedding-model for embeddings and the configured LLM for fact extraction", "aidu"),
    ("aidu's memory ledger currently holds over 800 structured facts", "aidu"),
    ("aidu's recall accuracy improved 15% after the ignition threshold was tuned last week", "aidu"),
    ("aidu was given a warm hexagon-themed UI called aiduMEI with 6 panels", "aidu"),
    # user linchen — 林晨（虚构产品负责人，替代原真实用户）
    ("林晨是明远科技的产品负责人，平时用一台 ThinkPad X1 Carbon 办公", "linchen"),
    ("林晨的设计审美偏好深色主题，克制、高级，不能花花绿绿", "linchen"),
    ("林晨最近在推进把数据统计面板接入记忆引擎的后端", "linchen"),
    ("林晨要求所有新项目都要开源友好，README 要中英文双语", "linchen"),
    ("林晨的代码提交习惯遵循 Conventional Commits 规范", "linchen"),
    # user alice (foreign user)
    ("Alice is a data scientist at Nexus AI, working on RAG pipeline evaluation", "alice"),
    ("Alice prefers Python and writes a lot of databricks notebooks", "alice"),
    ("She is experimenting with embedding models vs other embedding models for Chinese text", "alice"),
    ("Alice runs a home lab with a NVIDIA RTX 4090 for fine-tuning small LLMs", "alice"),
    # user bob
    ("Bob is a backend engineer specializing in Go and distributed systems", "bob"),
    ("Bob maintains the federation layer for the aiduMEI multi-agent memory network", "bob"),
    ("Bob recommends Nacos for service discovery and etcd for config store", "bob"),
    # knowledge tree / categories
    ("Memcached 一致性哈希在分片扩容时存在的坑已解决", "tech_solution"),
    ("Kubernetes Pod 频繁重启：用了 initContainer 初始化 secrets 导致的时序问题", "tech_solution"),
    ("前端白屏通常是 CDN 挂了或者 ECharts CDN 被墙，需要先切本地引用", "tech_solution"),
    # experience
    ("第一次带 8 岁女儿去后海滑冰，摔了三次才学会站起来，她说冰面像空心玻璃", "experience"),
    ("昨晚梦见北京四合院下大雨，屋檐水流声特别清晰，醒来后心情很平静", "experience"),
    ("读完《纳瓦尔宝典》之后重新整理了 Notion 的知识库标签体系", "experience"),
    ("在潘家园旧书摊淘到了一本 1998 年的《数据结构》习题集", "experience"),
    ("今天撸猫的时候发现门罗的《逃离》里的隐喻比我想象的还要多", "experience"),
    # raw drawer
    ("docker-compose up -d nginx && docker logs -f aidu_nginx ——nginx 端口被占用问题已解决", "raw_tech"),
    ("【根因】k8s rolling update 时 readiness probe 5s 超时 → 通过 probeThreshold=3 延长存活判定", "raw_tech"),
    ("curl -X POST http://localhost:13306/v1/chat/completions -d '{messages:[{role:user,content:hello}]}'", "raw_tech"),
    ("sqlite vacuum 之后磁盘从 12GB 清出 4.7GB，要经常顺手做", "raw_tech"),
]

results = []
for text, user in SEEDS:
    try:
        resp = requests.post(f"{API}/add", json={
            "messages": text,
            "user_id": user,
            "async_mode": True,
        }, headers=api_auth_headers(), timeout=10)
        ok = resp.status_code == 200
        results.append({"user": user, "ok": ok})
        if resp.status_code in (401, 403):
            print(f"[ERR:{resp.status_code}] 鉴权门禁已开启但没拿到 token —— "
                  f"请设 AIDUMEM_API_TOKEN 或把它写进仓库根 .env")
        else:
            print(f"[{'OK' if ok else 'ERR:' + str(resp.status_code)}] {user}: {text[:40]}")
    except Exception as e:
        results.append({"user": user, "ok": False})
        print(f"[ERR] {user}: {e}")

ok_count = sum(1 for r in results if r["ok"])
print(f"\n=== seeded {ok_count}/{len(results)} ===")
if ok_count == 0:
    print("No seeds succeeded — is aiduMEI running? Default: http://127.0.0.1:8767")
    print("If the auth gate is on, set AIDUMEM_API_TOKEN (or put it in the repo-root .env).")
    sys.exit(1)

print("\n--- flushing coalesce ---")
try:
    requests.post(f"{API}/add/coalesce/flush", headers=api_auth_headers(), timeout=60)
    print("flush ok")
except Exception as e:
    print(f"flush timed out (seeds still queued, LLM extraction runs in background): {e}")

try:
    r = requests.get(f"{API}/stats?user_id=default", headers=api_auth_headers(), timeout=10).json()
    print(f"stats: {r.get('total_memories', '?')} memories")
except Exception:
    print("stats: unavailable (server busy processing)")
try:
    r = requests.get(f"{API}/facts?limit=1", headers=api_auth_headers(), timeout=10).json()
    print(f"facts: {r.get('count', '?')}")
except Exception:
    print("facts: unavailable")
