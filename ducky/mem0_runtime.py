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

from ducky.utils import BASE_DIR, LOG_DIR, DEFAULT_USER_ID

USAGE_FILE = os.path.join(LOG_DIR, "llm_usage.json")
MEM0_CONFIG = os.path.join(BASE_DIR, "mem0_config_local.json")


def _clear_qdrant_lock():
    """启动前清理 Qdrant 残留锁文件，防止服务崩溃后锁死"""
    try:
        qdrant_path = os.path.join(BASE_DIR, "data", "qdrant")
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

    emb_key = cfg.get("embedder", {}).get("config", {}).get("api_key", "")
    if emb_key == "__SF_KEY__" or not emb_key:
        kp = os.path.join(base, ".sf_key")
        if os.path.exists(kp):
            with open(kp) as f:
                cfg["embedder"]["config"]["api_key"] = f.read().strip()

    llm_key = cfg.get("llm", {}).get("config", {}).get("api_key", "")
    if llm_key == "__SF_KEY__" or llm_key == "__LLM_KEY__" or not llm_key:
        kp = os.path.join(base, ".llm_key")
        if os.path.exists(kp):
            with open(kp) as f:
                cfg["llm"]["config"]["api_key"] = f.read().strip()

    rerank_cfg = cfg.get("rerank")
    if isinstance(rerank_cfg, dict):
        rk = rerank_cfg.get("config", {}).get("api_key", "")
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


def get_memory():
    """延迟初始化 mem0 单例，绑定到 sys 命名空间防止跨模块双重导入"""
    global m
    with _mem_init_lock:
        if hasattr(sys, "_aidumem_singleton") and sys._aidumem_singleton is not None:
            m = sys._aidumem_singleton
            return m
        if m is not None:
            sys._aidumem_singleton = m
            return m
        try:
            if Memory is None:
                raise RuntimeError("mem0 SDK 未加载")
            cfg = json.loads(open(MEM0_CONFIG).read())
            cfg = _resolve_api_keys(cfg)
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
        except Exception as e:
            logger.error(f"mem0 初始化失败: {e}")
            raise HTTPException(500, f"mem0 不可用: {e}")

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
