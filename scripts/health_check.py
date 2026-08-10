#!/usr/bin/env python3
"""aiduMEM 全功能健康检查 — Layer 1/2/3/4 状态 + API + LLM + Embedding"""
import json, base64, os, sys, time
import requests

# 仓库根 = 本文件上一级（scripts/ 的父目录），可用 AIDUMEM_HOME 覆盖
SCRIPT_DIR = os.environ.get("AIDUMEM_HOME") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.environ.get("AIDUMEM_CONFIG_FILE") or os.path.join(
    SCRIPT_DIR, "mem0_config_local.json")
API_BASE = os.environ.get("AIDUMEM_API_BASE", "http://127.0.0.1:8767").rstrip("/")

# ═══════════ 读取 mem0 配置 ═══════════
try:
    with open(CONFIG_FILE) as f:
        cfg = json.load(f)
except Exception as e:
    print(f"❌ 读取 mem0 配置失败: {e}")
    sys.exit(1)

# ── 读取 Embedding 配置 ──
emb_cfg = cfg.get("embedder", {}).get("config", {})
emb_model = emb_cfg.get("model", "your-embedding-model")
emb_base_url = emb_cfg.get("openai_base_url", "https://your-rerank-endpoint/v1")
emb_api_key = emb_cfg.get("api_key", "")
if emb_api_key == "__SF_KEY__" or not emb_api_key:
    kp = os.path.join(SCRIPT_DIR, ".sf_key")
    if os.path.exists(kp):
        with open(kp) as f:
            emb_api_key = f.read().strip()

# ── 读取 LLM 配置 ──
llm_cfg = cfg.get("llm", {}).get("config", {})
llm_model = llm_cfg.get("model", "unknown")
llm_base_url = llm_cfg.get("openai_base_url", "")
llm_api_key = llm_cfg.get("api_key", "")
llm_reasoning_effort = llm_cfg.get("reasoning_effort")  # 与正式链路对齐，如 "none"
if llm_api_key == "__SF_KEY__" or not llm_api_key:
    for kf in [".llm_key", ".sensenova_key"]:
        kp = os.path.join(SCRIPT_DIR, kf)
        if os.path.exists(kp):
            with open(kp) as f:
                llm_api_key = f.read().strip()
                break

# LLM 探活候选：公网网关优先（便于用量统计），本地隧道兜底；超时放宽到 30s
#   AIDUMEM_LLM_PUBLIC_BASE  公网 OpenAI-compatible 网关，如 https://your-gateway/v1
#   AIDUMEM_LLM_TUNNEL_BASE  本地隧道/直连地址
LOCAL_LLM_TUNNEL = os.environ.get("AIDUMEM_LLM_TUNNEL_BASE", "http://127.0.0.1:22012/v1")
PUBLIC_LLM_BASE = os.environ.get("AIDUMEM_LLM_PUBLIC_BASE", "")
LLM_PROBE_TIMEOUT = 30
llm_base_candidates = []
for u in [PUBLIC_LLM_BASE, llm_base_url, LOCAL_LLM_TUNNEL]:
    u = (u or "").rstrip("/")
    if u and u not in llm_base_candidates:
        llm_base_candidates.append(u)

# 探活 payload 与正式 aiduMEM LLM 配置对齐（避免默认开思考导致 6s+ 假慢）
llm_probe_payload = {
    "model": llm_model,
    "messages": [{"role": "user", "content": "ping"}],
    "max_tokens": 5,
}
if llm_reasoning_effort:
    llm_probe_payload["reasoning_effort"] = llm_reasoning_effort

checks = {}
start = time.time()

# ═══════════ 1. Embedding API ═══════════
t0 = time.time()
try:
    r = requests.post(
        f"{emb_base_url.rstrip('/')}/embeddings",
        headers={"Authorization": f"Bearer {emb_api_key}", "Content-Type": "application/json"},
        json={"model": emb_model, "input": "test"},
        timeout=15
    )
    checks["embedding_api"] = {"ok": r.status_code == 200, "code": r.status_code, "ms": int((time.time()-t0)*1000)}
except Exception as e:
    checks["embedding_api"] = {"ok": False, "error": str(e)[:100], "ms": int((time.time()-t0)*1000)}

# ═══════════ 2. LLM API ═══════════
t0 = time.time()
llm_ok = False
llm_last_err = ""
llm_code = None
llm_used = ""
for base in llm_base_candidates:
    llm_url = f"{base}/chat/completions"
    try:
        r = requests.post(
            llm_url,
            headers={"Authorization": f"Bearer {llm_api_key}", "Content-Type": "application/json"},
            json=llm_probe_payload,
            timeout=LLM_PROBE_TIMEOUT
        )
        if r.status_code == 200:
            llm_ok = True
            llm_code = r.status_code
            llm_used = base
            break
        llm_code = r.status_code
        llm_last_err = f"HTTP {r.status_code} via {base}"
    except Exception as e:
        llm_last_err = f"{e} via {base}"
        llm_code = None
if llm_ok:
    detail = f"{llm_code} via {llm_used}" if llm_used else llm_code
    if llm_reasoning_effort:
        detail = f"{detail} effort={llm_reasoning_effort}"
    checks["llm_api"] = {"ok": True, "code": detail, "ms": int((time.time()-t0)*1000)}
else:
    checks["llm_api"] = {"ok": False, "error": str(llm_last_err)[:140], "ms": int((time.time()-t0)*1000)}

# ═══════════ 3. aiduMEM API Health ═══════════
t0 = time.time()
try:
    r = requests.get(f"{API_BASE}/health", timeout=5)
    if r.status_code == 200:
        data = r.json()
        checks["aidumem_api"] = {
            "ok": True,
            "service": data.get("service", "unknown"),
            "version": data.get("version", "unknown"),
            "modules": data.get("modules", {}),
            "ms": int((time.time()-t0)*1000)
        }
    else:
        checks["aidumem_api"] = {"ok": False, "code": r.status_code, "ms": int((time.time()-t0)*1000)}
except Exception as e:
    checks["aidumem_api"] = {"ok": False, "error": str(e)[:100], "ms": int((time.time()-t0)*1000)}

# ═══════════ 4. aiduMEM Search ═══════════
t0 = time.time()
try:
    r = requests.post(f"{API_BASE}/search",
        json={"query": "健康检查", "user_id": "health_check", "limit": 1}, timeout=10)
    checks["aidumem_search"] = {"ok": r.status_code == 200, "code": r.status_code, "ms": int((time.time()-t0)*1000)}
except Exception as e:
    checks["aidumem_search"] = {"ok": False, "error": str(e)[:100], "ms": int((time.time()-t0)*1000)}

# ═══════════ 5. aiduMEM Stats ═══════════
t0 = time.time()
try:
    r = requests.get(f"{API_BASE}/stats?user_id={os.environ.get('AIDUMEM_DEFAULT_USER_ID', 'default')}", timeout=10)
    if r.status_code == 200:
        data = r.json()
        checks["aidumem_stats"] = {"ok": True, "total_memories": data.get("total", 0), "ms": int((time.time()-t0)*1000)}
    else:
        checks["aidumem_stats"] = {"ok": False, "code": r.status_code, "ms": int((time.time()-t0)*1000)}
except Exception as e:
    checks["aidumem_stats"] = {"ok": False, "error": str(e)[:100], "ms": int((time.time()-t0)*1000)}

# ═══════════ 汇总 ═══════════
total_ms = int((time.time() - start) * 1000)
all_ok = all(v.get("ok") for v in checks.values())

print(f"🧠 aiduMEM 健康检查 ({'🟢' if all_ok else '🔴'})")
print(f"llm_model_name={llm_model}")
print(f"emb_model_name={emb_model}")
for name, result in checks.items():
    icon = "✅" if result["ok"] else "❌"
    detail = result.get("error", "") or result.get("code", "")
    extra = ""
    if "service" in result:
        extra = f" [{result['service']} {result.get('version','')}]"
    if "total_memories" in result:
        extra = f" [记忆: {result['total_memories']}条]"
    if "modules" in result:
        mods = result["modules"]
        mod_status = " ".join(f"{k}={v}" for k,v in mods.items())
        extra += f" [模块: {mod_status}]"
    print(f"  {icon} {name}: {detail}{extra} ({result['ms']}ms)")

print(f"\n总计: {total_ms}ms | {'🟢 全部正常' if all_ok else '⚠️ 有异常'}")

sys.exit(0 if all_ok else 1)
