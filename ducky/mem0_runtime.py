"""
ducky.mem0_runtime — mem0 单例 + 用量追踪 + lazy 模块 + salience 辅助
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
C 档 (2026-07-19) 从 api_server 抽出，语义不变。
对外仍由 api_server 再导出 get_memory，兼容 legacy_routes 的
`from api_server import get_memory`。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException

from ducky.memory_salience import on_memory_accessed, on_memory_added

logger = logging.getLogger("aiduMEM.runtime")

from ducky.utils import BASE_DIR, LOG_DIR, DEFAULT_USER_ID, mem0_config_path

USAGE_FILE = os.path.join(LOG_DIR, "llm_usage.json")
MEM0_CONFIG = mem0_config_path()   # v20.2.4 F-22：支持 AIDUMEM_CONFIG_FILE


def _clear_qdrant_lock():
    """启动前清理 Qdrant 残留锁文件，防止服务崩溃后锁死"""
    try:
        # v20.3.2（外审 Gemini P2-3）：部署方设了 AIDUMEM_DATA_DIR 时，锁文件在那边，
        # 硬编码 BASE_DIR/data 会去清一个不存在的路径 —— 崩溃重启后真锁清不掉。
        from ducky.utils import DATA_DIR as _DATA_DIR
        qdrant_path = os.path.join(_DATA_DIR, "qdrant")
        lock_file = os.path.join(qdrant_path, ".lock")
        if os.path.exists(lock_file):
            os.remove(lock_file)
            logger.info("🔓 Qdrant 残留锁文件已清理")
    except Exception as e:
        logger.warning(f"Qdrant 锁清理跳过: {e}")

# ═══════════════════════════════════════════════
# §1  LLM & Embedding 用量追踪
# ═══════════════════════════════════════════════
_usage_lock = threading.Lock()
_llm_usage: dict = {}


def _load_usage():
    global _llm_usage
    if os.path.exists(USAGE_FILE):
        try:
            with open(USAGE_FILE) as f:
                _llm_usage = json.load(f)
        except Exception:
            _llm_usage = {}


def _save_usage():
    with open(USAGE_FILE, "w") as f:
        json.dump(_llm_usage, f, indent=2)


def _ensure_today() -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today not in _llm_usage:
        _llm_usage[today] = {}
    return today


def _track_llm_usage(input_tokens: int, output_tokens: int, total_tokens: int):
    today = _ensure_today()
    with _usage_lock:
        d = _llm_usage[today].setdefault(
            "llm", {"calls": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        )
        d["calls"] += 1
        d["input_tokens"] += input_tokens
        d["output_tokens"] += output_tokens
        d["total_tokens"] += total_tokens
        _save_usage()


def _track_embed_usage(total_tokens: int):
    today = _ensure_today()
    with _usage_lock:
        d = _llm_usage[today].setdefault("embedding", {"calls": 0, "total_tokens": 0})
        d["calls"] += 1
        d["total_tokens"] += total_tokens
        _save_usage()


def _track_rerank_tokens(input_tokens: int = 0, total_tokens: int = 0):
    """记录 rerank 的 token 消耗（provider 响应带 usage 时调用；不计 calls）。

    v20 拆分：此前 provider 各自 +1 calls，但只有响应里带 usage 字段才会
    调用——Cohere 从来不带，导致 /usage 里 Cohere 一次都没"发生过"。
    现在 calls/failures/latency 统一由 rerank() 本体记账（每次真实外呼
    恰好一次），这里只加 token。
    """
    today = _ensure_today()
    with _usage_lock:
        d = _llm_usage[today].setdefault(
            "rerank",
            {"calls": 0, "input_tokens": 0, "total_tokens": 0,
             "failures": 0, "latency_ms_sum": 0.0, "providers": {}},
        )
        d["input_tokens"] = d.get("input_tokens", 0) + input_tokens
        d["total_tokens"] = d.get("total_tokens", 0) + total_tokens
        _save_usage()


def _track_rerank_usage(provider: str = "", latency_ms: float = 0.0, failed: bool = False):
    """每次真实 rerank 外呼记一笔：calls / failures / 耗时 / 按 provider 细分。"""
    today = _ensure_today()
    with _usage_lock:
        d = _llm_usage[today].setdefault(
            "rerank",
            {"calls": 0, "input_tokens": 0, "total_tokens": 0,
             "failures": 0, "latency_ms_sum": 0.0, "providers": {}},
        )
        d["calls"] = d.get("calls", 0) + 1
        d["failures"] = d.get("failures", 0) + (1 if failed else 0)
        d["latency_ms_sum"] = round(d.get("latency_ms_sum", 0.0) + latency_ms, 1)
        prov = d.setdefault("providers", {}).setdefault(provider or "unknown", {"calls": 0, "failures": 0})
        prov["calls"] += 1
        if failed:
            prov["failures"] += 1
        _save_usage()


def track_vision_usage(input_tokens: int = 0, output_tokens: int = 0, total_tokens: int = 0):
    """追踪多模态 Vision API 用量（v18.3）"""
    today = _ensure_today()
    with _usage_lock:
        d = _llm_usage[today].setdefault("vision", {"calls": 0, "total_tokens": 0})
        d["calls"] += 1
        d["total_tokens"] += total_tokens or (input_tokens + output_tokens)
        _save_usage()


# ═══════════════════════════════════════════════
# §1b Reranker（懒加载配置 + requests 直发）
# ═══════════════════════════════════════════════
_RERANK_CONFIG_CACHE: Optional[dict] = None

# ---------------------------------------------------------------------------
# Reranker provider registry
# Each entry knows how to: build the HTTP request, parse the response.
# Return shape: a list of {index, relevance_score} sorted descending.
# ---------------------------------------------------------------------------

def _rerank_http_error(r) -> None:
    """非 200 一律抛错（只带状态码，不带响应体——响应体可能回显凭据错误详情）。

    v20 之前非 200 返回 []，401（key 失效）和「真没相关文档」在调用方
    完全无法区分，key 过期后重排序静默消失、检索质量悄悄退化。
    """
    if r.status_code != 200:
        raise RuntimeError(f"rerank HTTP {r.status_code}")


def _rerank_openai_rerank(cfg: dict, query: str, documents: list, top_n: int) -> list[dict]:
    """OpenAI-compatible rerank endpoint"""
    import requests as req
    r = req.post(
        f"{cfg['base_url']}/rerank",
        headers={
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
        },
        json={
            "model": cfg["model"],
            "query": query,
            "documents": documents,
            "top_n": min(top_n, len(documents)),
        },
        timeout=10,
    )
    _rerank_http_error(r)
    results = r.json().get("results", [])
    meta = r.json().get("meta", {})
    tokens = meta.get("tokens", {})
    if tokens:
        _track_rerank_tokens(input_tokens=tokens.get("input_tokens", 0),
                             total_tokens=tokens.get("input_tokens", 0))
    return [{"index": x["index"], "relevance_score": x.get("relevance_score", 0)} for x in results]


def _rerank_jina(cfg: dict, query: str, documents: list, top_n: int) -> list[dict]:
    """Jina AI rerank endpoint"""
    import requests as req
    r = req.post(
        "https://api.jina.ai/v1/rerank",
        headers={
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json={
            "model": cfg.get("model") or "jina-reranker-v3",
            "query": query,
            "documents": documents,
            "top_n": min(top_n, len(documents)),
        },
        timeout=10,
    )
    _rerank_http_error(r)
    results = r.json().get("results", [])
    usage = r.json().get("usage", {})
    if usage.get("total_tokens"):
        _track_rerank_tokens(input_tokens=usage["total_tokens"],
                             total_tokens=usage["total_tokens"])
    return [{"index": x["index"], "relevance_score": x.get("relevance_score", 0)} for x in results]


def _rerank_cohere(cfg: dict, query: str, documents: list, top_n: int) -> list[dict]:
    """Cohere / rerank endpoint"""
    import requests as req
    r = req.post(
        "https://api.cohere.com/v1/rerank",
        headers={
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
        },
        json={
            "model": cfg.get("model") or "rerank-v3.5",
            "query": query,
            "documents": documents,
            "top_n": min(top_n, len(documents)),
            "return_documents": False,
        },
        timeout=10,
    )
    _rerank_http_error(r)
    data = r.json()
    results = data.get("results", [])
    # Cohere 按 search_units 计费而非 token；此前这条路径从不记账，
    # /usage 里 Cohere 一次都"没发生过"。计入 token 栏并如实按单位记。
    units = (data.get("meta") or {}).get("billed_units", {}).get("search_units", 0)
    if units:
        _track_rerank_tokens(input_tokens=0, total_tokens=int(units))
    return [{"index": x["index"], "relevance_score": x.get("relevance_score", 0)} for x in results]


def _rerank_openai_compatible(cfg: dict, query: str, documents: list, top_n: int) -> list[dict]:
    """Generic OpenAI-compatible rerank endpoint (e.g. Azure, vLLM, LiteLLM rerank)."""
    import requests as req
    base = cfg['base_url'].rstrip('/')
    url = f"{base}/rerank" if not base.endswith('/rerank') else base
    r = req.post(
        url,
        headers={"Authorization": f"Bearer {cfg['api_key']}",
                 "Content-Type": "application/json"},
        json={
            "model": cfg["model"],
            "query": query,
            "documents": documents,
            "top_n": min(top_n, len(documents)),
        },
        timeout=10,
    )
    _rerank_http_error(r)
    data = r.json()
    results = data.get("results", [])
    usage = data.get("usage", {})
    if usage and usage.get("total_tokens"):
        _track_rerank_tokens(input_tokens=usage["total_tokens"],
                             total_tokens=usage["total_tokens"])
    return [{"index": x["index"], "relevance_score": x.get("relevance_score", 0)} for x in results]


RERANK_PROVIDERS = {
    "siliconflow": _rerank_openai_rerank,
    "jina": _rerank_jina,
    "cohere": _rerank_cohere,
    "openai_compatible": _rerank_openai_compatible,
    # aliases — make config forgiving
    "sf": _rerank_openai_rerank,
    "openai": _rerank_openai_compatible,
    "azure": _rerank_openai_compatible,
    "vllm": _rerank_openai_compatible,
    "litellm": _rerank_openai_compatible,
}

# default provider back-compat: old configs without provider field
DEFAULT_RERANK_PROVIDER = "openai_compatible"


def _load_rerank_config() -> dict:
    """从 mem0_config 或环境读 reranker 配置，返回 {provider, api_key, base_url, model}"""
    global _RERANK_CONFIG_CACHE
    if _RERANK_CONFIG_CACHE is not None:
        return _RERANK_CONFIG_CACHE
    cfg = {}
    try:
        if os.path.exists(MEM0_CONFIG):
            with open(MEM0_CONFIG) as f:
                j = json.load(f)
            rerank = j.get("rerank") or j.get("reranker") or {}
            rc = rerank.get("config", {})
            cfg["provider"] = rerank.get("provider", DEFAULT_RERANK_PROVIDER)
            cfg["model"] = rc.get("model", "")
            cfg["base_url"] = rc.get("openai_base_url", "")
            api_key = rc.get("api_key", "")
            if api_key == "__SF_KEY__" or not api_key:
                kp = os.path.join(BASE_DIR, ".sf_key")
                if os.path.exists(kp):
                    with open(kp) as fk:
                        api_key = fk.read().strip()
            cfg["api_key"] = api_key
        else:
            # 兜底：跟 embedding 一样
            cfg = {
                "provider": DEFAULT_RERANK_PROVIDER,
                "model": "",
                "base_url": "",
                "api_key": "",
            }
            kp = os.path.join(BASE_DIR, ".sf_key")
            if os.path.exists(kp):
                with open(kp) as fk:
                    cfg["api_key"] = fk.read().strip()
    except Exception as e:
        logger.warning(f"rerank config load skip: {e}")
    _RERANK_CONFIG_CACHE = cfg
    return cfg


# v20 P0-4：rerank 逐请求遥测。线程本地——FastAPI 同步端点每个请求跑在
# 独立线程里，/search 链路（search → hybrid → scoring → rerank）全程同线程，
# 请求开头 reset、结尾读取即可拿到本次请求的真实 rerank 结局；
# 线程复用带来的跨请求残留由 reset_rerank_telemetry() 消除。
_rerank_tls = threading.local()


def reset_rerank_telemetry() -> None:
    _rerank_tls.last = None


def last_rerank_telemetry() -> Optional[dict]:
    return getattr(_rerank_tls, "last", None)


def rerank_config_status() -> dict:
    """/health 用：只报「配没配、配的谁」，绝不吐 key/base_url 内容。"""
    cfg = _load_rerank_config()
    provider = str(cfg.get("provider", DEFAULT_RERANK_PROVIDER))
    # 与 rerank() 的判定保持同一条规则：jina/cohere 端点写死官方 URL，
    # 不需要 base_url。两处不一致会让 /health 报「未配置」而实际在跑。
    needs_base_url = provider.lower() not in ("jina", "cohere")
    configured = bool(cfg.get("api_key")) and (bool(cfg.get("base_url")) or not needs_base_url)
    out = {"configured": configured}
    if configured:
        out["provider"] = cfg.get("provider", DEFAULT_RERANK_PROVIDER)
        out["model"] = cfg.get("model", "")
    return out


def rerank(query: str, documents: list[str], top_n: int = 10) -> list[dict]:
    """
    调用配置好的 reranker 做重排序。返回 [{index, relevance_score}, ...] 按分数降序。
    失败返回空列表，不阻断检索主链路——但结局必须可观测：
    「未配置 / 调用失败 / 真空结果」三态记入线程本地遥测与 /usage 账本，
    绝不折叠成同一个静默的 []。
    支持: OpenAI-compatible / Jina / Cohere
    """
    telem: dict = {"status": "not_configured", "provider": None, "latency_ms": 0.0}
    _rerank_tls.last = telem
    if not documents:
        telem["status"] = "skipped_empty_input"
        return []
    # v20.2.4（外审 F-03）：本地档拦下云 rerank。三态遥测里如实记一态，
    # 绝不折叠进 not_configured —— 「没配」和「档位不让」是两件事。
    from ducky.engine_mode import cloud_egress_allowed
    if not cloud_egress_allowed("rerank"):
        telem["status"] = "blocked_by_engine_mode"
        return []
    cfg = _load_rerank_config()
    api_key = cfg.get("api_key", "")
    base_url = cfg.get("base_url", "")
    provider = cfg.get("provider", DEFAULT_RERANK_PROVIDER)
    # Jina/Cohere 端点是写死的官方 URL，不需要 base_url；只有
    # OpenAI-compatible 系需要。未配置判定按 provider 区分。
    needs_base_url = provider.lower() not in ("jina", "cohere")
    if not api_key or (needs_base_url and not base_url):
        telem["status"] = "not_configured"
        return []
    handler = RERANK_PROVIDERS.get(provider.lower(), _rerank_openai_compatible)
    telem["provider"] = provider
    telem["model"] = cfg.get("model", "")
    t0 = time.time()
    try:
        out = handler(cfg, query, documents, top_n)
        telem["latency_ms"] = round((time.time() - t0) * 1000, 1)
        telem["status"] = "ok" if out else "empty"
        telem["returned"] = len(out)
        _track_rerank_usage(provider=provider, latency_ms=telem["latency_ms"], failed=False)
        return out
    except Exception as e:
        telem["latency_ms"] = round((time.time() - t0) * 1000, 1)
        telem["status"] = "error"
        telem["error"] = str(e)[:200]
        _track_rerank_usage(provider=provider, latency_ms=telem["latency_ms"], failed=True)
        logger.warning(f"rerank ({provider}) 调用失败: {e}")
        return []


_load_usage()


def mem0_patch_status():
    """给 /health 用：mem0 补丁层实际台账（哪条打上了、哪条没打上、救回多少次）。"""
    try:
        from ducky.mem0_patches import patch_status
        return patch_status()
    except Exception as e:
        return {"ok": False, "problems": ["patch_layer_import"],
                "patches": {}, "counters": {}, "error": str(e)}


def get_llm_usage() -> dict:
    """/usage 端点用：返回当前用量快照。"""
    return _llm_usage


# ═══════════════════════════════════════════════
# §2  mem0 SDK 加载（延迟初始化）
# ═══════════════════════════════════════════════
try:
    from mem0 import Memory
    logger.info("✅ mem0 SDK loaded")
except Exception as e:
    logger.error(f"mem0 SDK 加载失败: {e}")
    Memory = None

m = None  # 模块级单例（延迟填充）
_mem_init_lock = threading.Lock()
_lazy_lock = threading.Lock()


def _patch_usage_tracking(mem_instance):
    """兼容入口：真正的补丁逻辑已收敛到 ducky.mem0_patches（单一真相源）。

    历史坑（v20.0pre 修）：本函数原先把用量追踪打在 `mem_instance.client` 上，
    而 mem0 的 Memory 类根本没有 `client` 属性 —— OpenAI 客户端挂在
    `Memory.llm.client` / `Memory.embedding_model.client` 上。于是补丁常年空转，
    而 get_memory() 仍无条件打印「用量追踪已激活」。更深一层：即使挂载点写对，
    原先 `_orig_create(self, *args)` 的绑定写法会多传一个位置参数，抛
    TypeError 而根本到不了网络 —— 因为挂载点先错，这一层从未暴露。
    """
    try:
        from ducky.mem0_patches import apply_all
        return apply_all(mem_instance)
    except Exception as e:
        # 补丁层自身塌了也不能让 mem0 起不来，但绝不许静默（铁律 8）
        logger.error(f"mem0 补丁层加载失败，基座将以未打补丁状态运行: {e}")
        return {"ok": False, "problems": ["patch_layer_import"],
                "patches": {}, "counters": {}}


def _resolve_api_keys(cfg: dict) -> dict:
    """替换 __SF_KEY__ 占位符为真实 key — 所有密钥从文件读取，禁止硬编码"""
    import copy
    cfg = copy.deepcopy(cfg)
    base = BASE_DIR

    emb_sec = cfg.get("embedder")
    if isinstance(emb_sec, dict) and isinstance(emb_sec.get("config"), dict):
        emb_key = emb_sec["config"].get("api_key", "")
        if emb_key == "__SF_KEY__" or not emb_key:
            kp = os.path.join(base, ".sf_key")
            if os.path.exists(kp):
                with open(kp) as f:
                    emb_sec["config"]["api_key"] = f.read().strip()

    llm_sec = cfg.get("llm")
    if isinstance(llm_sec, dict) and isinstance(llm_sec.get("config"), dict):
        llm_key = llm_sec["config"].get("api_key", "")
        if llm_key == "__SF_KEY__" or llm_key == "__LLM_KEY__" or not llm_key:
            kp = os.path.join(base, ".llm_key")
            if os.path.exists(kp):
                with open(kp) as f:
                    llm_sec["config"]["api_key"] = f.read().strip()

    rerank_cfg = cfg.get("rerank")
    if isinstance(rerank_cfg, dict) and isinstance(rerank_cfg.get("config"), dict):
        rk = rerank_cfg["config"].get("api_key", "")
        if rk == "__SF_KEY__" or not rk:
            kp = os.path.join(base, ".sf_key")
            if os.path.exists(kp):
                with open(kp) as f:
                    rerank_cfg["config"]["api_key"] = f.read().strip()
    return cfg


_legacy_map_announced = False


def _normalize_user_id(user_id: str) -> str:
    """规范化 user ID：历史版本使用过自定义名字，现统一并入**当前默认身份**，保证与存量库兼容。

    历史私有 user_id 不写进仓库：通过环境变量 AIDUMEM_LEGACY_USER_IDS
    （逗号分隔）由部署方按各自存量库配置，映射后老数据才能被新查询召回。
    不再硬编码 admin/user 映射，避免未来真实用户被静默并进默认分区。

    v19.4.2 之一：首次调用时自报一次映射状态。
    脱敏把映射规则整个交给了环境变量，而「没配」和「配好了」在行为上
    长得一模一样——都是安静地什么都不做。区别只在某天有人问
    「我那批老记忆怎么搜不到了」。所以让它开口说一句。

    v19.4.2 之二：映射目标由字面量 "default" 改为 DEFAULT_USER_ID。
    原先无论部署方把默认身份配成什么，历史 id 一律被并进字面量 default——
    于是「把老数据映射到我当前的身份」这件事根本做不到：配了
    AIDUMEM_LEGACY_USER_IDS 反而让老 id 的查询被劫持到一个第三方分区，
    比不配更糟。改后：默认身份未配置时 DEFAULT_USER_ID 就是 "default"，
    行为与旧版逐字节一致；配置了才落到部署方自己的分区。
    """
    global _legacy_map_announced
    if not user_id:
        return DEFAULT_USER_ID
    legacy = set()
    extra = os.getenv("AIDUMEM_LEGACY_USER_IDS", "")
    if extra:
        legacy = {x.strip().lower() for x in extra.split(",") if x.strip()}
    if not _legacy_map_announced:
        _legacy_map_announced = True
        if legacy:
            logger.info(
                "历史 user_id 映射已启用：%d 个 id 将并入 %s", len(legacy), DEFAULT_USER_ID
            )
        else:
            logger.info(
                "历史 user_id 映射未启用（AIDUMEM_LEGACY_USER_IDS 未设）。"
                "若存量库里有旧分区的数据，它们不会被当前身份召回。"
            )
    return DEFAULT_USER_ID if user_id.lower() in legacy else user_id


def _assert_vector_store_inside_sandbox(cfg: dict) -> None:
    """测试沙箱内禁止向量腿逃逸（v20.1 整改轮 R-17 · 外审 w P1-③）。

    conftest 只重定向了 `AIDUMEM_DATA_DIR`（SQLite 面），而 mem0 配置里的
    嵌入式 Qdrant `path` 是**绝对路径**，env 重定向盖不住它 —— 在沙箱里跑
    带真 mem0 的用例，向量会直接写穿到配置指向的真实库。生产向量库里的
    alice/bob/test_user 测试点（外审 w 实测）就是这么进去的。

    判据：数据目录带测试沙箱前缀（conftest 的 DIR_PREFIX）时，本地向量库
    路径必须在沙箱目录之内，否则**拒绝初始化并点名那条路径**。
    生产/常规部署（无沙箱前缀）完全不受影响；远端 Qdrant（host/url 配置、
    无本地 path）不属于本守卫射程 —— 那是部署方显式指定的外部服务。
    """
    data_dir = os.environ.get("AIDUMEM_DATA_DIR", "")
    if "aidumei_test_data_" not in os.path.basename(data_dir.rstrip("/")):
        return
    vs_cfg = ((cfg.get("vector_store") or {}).get("config") or {})
    path = vs_cfg.get("path")
    if not path:
        return
    real_path = os.path.realpath(str(path))
    real_dir = os.path.realpath(data_dir)
    if not (real_path == real_dir or real_path.startswith(real_dir + os.sep)):
        raise RuntimeError(
            "测试沙箱内拒绝连接沙箱外的本地向量库："
            f"vector_store.config.path={path!r} 不在 AIDUMEM_DATA_DIR 沙箱内。"
            "带真 mem0 的用例请把向量库路径指进沙箱（否则测试点会写穿真实库）"
        )


class Mem0NotConfiguredError(HTTPException):
    """mem0 从未启用；删除链可以安全跳过这一层。

    这是初始化边界的类型契约，不是对任意初始化异常的事后解释。它同时继承
    ``HTTPException``，让普通 API 调用保留可操作的 503；删除链则能精确捕获
    这个类型，而不必从 HTTP 状态码或异常字符串事后猜测。配置损坏、配置不
    完整、凭据失效、服务不可达与 SDK 错误都不能构造此异常。
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(
            status_code=503,
            detail=(
                "记忆写入依赖的向量后端尚未配置："
                "请复制 mem0_config_local.json.example 为 mem0_config_local.json，"
                "填入 embedding / LLM 凭据；用 GET /health 查看 degraded 与 "
                "degraded_details；不配凭据可先使用 POST /add/raw。"
                f"（原因：{reason}）"
            ),
        )


_CONFIG_EXAMPLE = "mem0_config_local.json.example"
PLACEHOLDER_HINTS = ("your_", "replace_", "change_me", "<", "xxx", "placeholder")


def is_placeholder_key(value: str) -> bool:
    """凭据值是否仍是样例占位符。"""
    normalized = (value or "").strip().lower()
    return not normalized or any(h in normalized for h in PLACEHOLDER_HINTS)


def _read_resolved_mem0_config() -> dict:
    """读取并解析 mem0 配置；这是初始化与状态探针共用的唯一入口。"""
    path = mem0_config_path()
    if not os.path.isfile(path):
        raise Mem0NotConfiguredError("config_file_missing")
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    # API-key file fallback is part of the production configuration path; do
    # not let a malformed top-level value be mistaken for an absent backend.
    return _resolve_api_keys(cfg)


def _is_placeholder_mem0_config(cfg: dict) -> bool:
    """只把完整的生产形状样例配置认作「从未配置」。

    顶层 ``embedder.api_key`` 等旧/错误形状不会被误认成已配置；它会继续进入
    mem0 初始化并作为配置错误失败。这样状态探针与真实加载路径不会各自发明
    一套配置解释。
    """
    sections = []
    for name in ("embedder", "llm"):
        section = cfg.get(name)
        if not isinstance(section, dict) or not isinstance(section.get("config"), dict):
            return False
        config = section["config"]
        # Missing required fields is a malformed/incomplete deployment, not the
        # complete sample users have never edited.  Let mem0 surface that as a
        # real initialization failure instead of making deletion skip it.
        if "api_key" not in config:
            return False
        sections.append(config["api_key"])
    return bool(sections) and all(is_placeholder_key(key) for key in sections)


def _load_mem0_config() -> dict:
    """读取真实配置，并只在明确的「从未配置」形态抛专用异常。"""
    cfg = _read_resolved_mem0_config()
    if _is_placeholder_mem0_config(cfg):
        raise Mem0NotConfiguredError("placeholder_credentials")
    return cfg


def get_memory():
    """延迟初始化 mem0 单例，绑定到 sys 命名空间防止跨模块双重导入。"""
    global m
    with _mem_init_lock:
        if hasattr(sys, "_aidumem_singleton") and sys._aidumem_singleton is not None:
            m = sys._aidumem_singleton
            return m
        if m is not None:
            sys._aidumem_singleton = m
            return m
        try:
            # 必须先加载配置：无配置时即使 mem0 SDK 未装，删除链也能收到
            # 明确的 Mem0NotConfiguredError；SDK 缺失本身则仍是真故障。
            cfg = _load_mem0_config()
            if Memory is None:
                raise RuntimeError("mem0 SDK 未加载")
            _assert_vector_store_inside_sandbox(cfg)
            # 启动时清理 Qdrant 锁（与生产环境对齐：先读配置再清理）
            _clear_qdrant_lock()
            m = Memory.from_config(cfg)
            _patch_state = _patch_usage_tracking(m)
            try:
                from ducky.add_speed import patch_llm_for_speed
                patch_llm_for_speed(m)
            except Exception as pe:
                logger.warning(f"speed patch on init skip: {pe}")
            sys._aidumem_singleton = m
            # 不再无条件宣称「已激活」—— 按补丁层的实际台账说话（铁律 7 宣称即承诺）
            _p = (_patch_state or {}).get("patches", {})
            _summary = ", ".join(f"{k}={v.get('status')}" for k, v in _p.items()) or "补丁台账为空"
            if (_patch_state or {}).get("ok"):
                logger.info(f"✅ mem0 初始化成功（补丁层: {_summary}）")
            else:
                logger.error(
                    f"⚠️ mem0 初始化完成，但补丁层有问题项 "
                    f"{(_patch_state or {}).get('problems')}（补丁层: {_summary}）"
                )
            return m
        except Mem0NotConfiguredError:
            logger.info("mem0 后端未配置，按零凭据首跑路径返回 503")
            raise
        except Exception as e:
            logger.error(f"mem0 初始化失败: {e}")
            raise HTTPException(503, _mem0_unavailable_detail(e))


def mem0_backend_configured() -> tuple[bool, str]:
    """通过真实配置加载路径报告 mem0 是否明确启用。"""
    try:
        cfg = _read_resolved_mem0_config()
    except Mem0NotConfiguredError as exc:
        return False, exc.reason
    except Exception as exc:
        # 文件存在但读不了/不是合法 JSON，或密钥解析失败：这是部署故障，
        # 不能让删除链把它当成「没启用」。
        logger.warning("mem0 配置读取失败（按真故障处理，不降级）: %s", exc)
        return True, "config_unreadable"
    if _is_placeholder_mem0_config(cfg):
        return False, "placeholder_credentials"
    return True, "configured"


def is_backend_not_configured(exc: BaseException) -> bool:
    """兼容查询：只有专用异常才代表「后端从未配置」。

    删除链不调用此函数；它保留给旧集成方时，必须继续拒绝裸 RuntimeError、
    HTTPException(503) 以及任何其他事后猜测。
    """
    return isinstance(exc, Mem0NotConfiguredError)


def _mem0_unavailable_detail(exc: Exception) -> str:
    """把 mem0 初始化失败翻译成**调用方能照着做**的一句话。

    🔴 参赛前自查 N-1：这里原先是 `f"mem0 不可用: {e}"` + HTTP 500，
    于是第一次拿到这个项目的人，第一个动作（写一条记忆）换来的是：

        500 {"detail":"mem0 不可用: Using SOCKS proxy, but the 'socksio'
             package is not installed. Make sure to install httpx using ..."}

    这句话**是真的，但对他没有用** —— 它说的是 httpx 内部的事，没说
    「你还没配 key」，也没说「不配 key 也有一条路能走」。第一印象就此定型。

    两处改动：
      · 状态码 500 → **503**。这不是服务端故障，是**依赖未就绪**；
        500 会让调用方以为撞上了 bug，503 才是「先去把依赖配好」。
      · 正文给出**缺什么 / 去哪配 / 不配怎么办**三件事，原始异常保留在末尾
        （运维还要靠它定位），但不再是唯一内容。
    """
    raw = str(exc)
    low = raw.lower()
    if "api" in low and "key" in low or "unauthorized" in low or "401" in low:
        cause = "嵌入/LLM 服务的凭据缺失或无效"
    elif "socks" in low or "proxy" in low:
        cause = "出站代理配置导致 HTTP 客户端无法建立连接"
    elif "connect" in low or "timeout" in low or "resolve" in low:
        cause = "嵌入/LLM 服务地址不可达"
    else:
        cause = "mem0 初始化未能完成"
    return (
        f"记忆写入依赖的向量后端尚未就绪：{cause}。"
        f"① 复制 {_CONFIG_EXAMPLE} 为 mem0_config_local.json 并填入你的 "
        f"embedding / LLM 凭据；② 用 GET /health 查看 degraded 与 "
        f"degraded_details 确认还缺什么；"
        f"③ 暂时不想配也可以先用 POST /add/raw —— 那条路零 LLM、零向量，"
        f"仍会写入原文并建全文索引。原始错误：{raw[:160]}"
    )


def reset_memory_singleton() -> None:
    """/reload 用：清空模块级 + sys 级单例。"""
    global m, _RERANK_CONFIG_CACHE
    m = None
    if hasattr(sys, "_aidumem_singleton"):
        sys._aidumem_singleton = None
    # /reload 的语义是「配置改了，重读」。rerank 配置与 mem0 配置同源
    # （mem0_config.json），不清这份缓存的话，换了 rerank key/base_url
    # 之后 /reload 表面成功，重排序却继续用旧凭据直到进程重启。
    _RERANK_CONFIG_CACHE = None


def is_mem_ready() -> bool:
    return m is not None or getattr(sys, "_aidumem_singleton", None) is not None


# ═══════════════════════════════════════════════
# §3  延迟导入缓存（避免循环引用）
# ═══════════════════════════════════════════════
_layer1 = None
_funnel = None
_hybrid = None


def lazy_import_layer1():
    global _layer1
    if _layer1 is None:
        with _lazy_lock:
            if _layer1 is None:
                from ducky.layer1_selfcheck import layer1_add_wrapper
                _layer1 = layer1_add_wrapper
    return _layer1


def lazy_import_funnel():
    global _funnel
    if _funnel is None:
        with _lazy_lock:
            if _funnel is None:
                from ducky.recall_funnel import funnel_search
                _funnel = funnel_search
    return _funnel


def lazy_import_hybrid():
    global _hybrid
    if _hybrid is None:
        with _lazy_lock:
            if _hybrid is None:
                from ducky.hybrid_recall import hybrid_search
                _hybrid = hybrid_search
    return _hybrid


# ═══════════════════════════════════════════════
# §4  salience 辅助
# ═══════════════════════════════════════════════
def register_salience_for_add(add_result, user_id: str = "", bank_id: str = ""):
    """mem0.add() 返回后注册显著性（非关键路径，失败只打 debug）

    v20 P0-2：调用方把写入作用域一并传来，salience 行盖 (user_id, bank_id)
    戳——conflict.py 分域配对靠它。不传 = default 域（v19 行为）。
    """
    try:
        results = add_result if isinstance(add_result, list) else add_result.get("results", [])
        for r in results:
            mid = r.get("id") or r.get("memory_id", "")
            content = r.get("memory") or r.get("data") or ""
            if mid:
                on_memory_added(mid, content=content, user_id=user_id, bank_id=bank_id)
    except Exception as e:
        logger.debug(f"salience register skip: {e}")


def boost_salience_for_results(results):
    """搜索结果 salience 提权"""
    if results is None:
        return
    for r in (results if isinstance(results, list) else results.get("results", [])):
        if isinstance(r, dict):
            mid = r.get("id") or r.get("memory_id", "")
            if mid:
                on_memory_accessed(mid)
