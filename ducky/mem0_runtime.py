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
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException

from ducky.memory_salience import on_memory_accessed, on_memory_added

logger = logging.getLogger("aiduMEM.runtime")

from ducky.utils import BASE_DIR, LOG_DIR

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


def _track_rerank_usage(input_tokens: int = 0, total_tokens: int = 0):
    """追踪 rerank API 用量"""
    today = _ensure_today()
    with _usage_lock:
        d = _llm_usage[today].setdefault("rerank", {"calls": 0, "input_tokens": 0, "total_tokens": 0})
        d["calls"] += 1
        d["input_tokens"] += input_tokens
        d["total_tokens"] += total_tokens
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
    if r.status_code == 200:
        results = r.json().get("results", [])
        meta = r.json().get("meta", {})
        tokens = meta.get("tokens", {})
        if tokens:
            _track_rerank_usage(input_tokens=tokens.get("input_tokens", 0),
                                total_tokens=tokens.get("input_tokens", 0))
        return [{"index": x["index"], "relevance_score": x.get("relevance_score", 0)} for x in results]
    return []


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
    if r.status_code == 200:
        results = r.json().get("results", [])
        usage = r.json().get("usage", {})
        if usage.get("total_tokens"):
            _track_rerank_usage(input_tokens=usage["total_tokens"],
                                total_tokens=usage["total_tokens"])
        return [{"index": x["index"], "relevance_score": x.get("relevance_score", 0)} for x in results]
    return []


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
    if r.status_code == 200:
        results = r.json().get("results", [])
        return [{"index": x["index"], "relevance_score": x.get("relevance_score", 0)} for x in results]
    return []


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
    if r.status_code == 200:
        data = r.json()
        results = data.get("results", [])
        usage = data.get("usage", {})
        if usage and usage.get("total_tokens"):
            _track_rerank_usage(input_tokens=usage["total_tokens"],
                                total_tokens=usage["total_tokens"])
        return [{"index": x["index"], "relevance_score": x.get("relevance_score", 0)} for x in results]
    return []


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


def rerank(query: str, documents: list[str], top_n: int = 10) -> list[dict]:
    """
    调用配置好的 reranker 做重排序。返回 [{index, relevance_score}, ...] 按分数降序。
    失败返回空列表，不阻断检索主链路。
    支持: OpenAI-compatible / Jina / Cohere
    """
    if not documents:
        return []
    cfg = _load_rerank_config()
    api_key = cfg.get("api_key", "")
    base_url = cfg.get("base_url", "")
    if not api_key or not base_url:
        return []
    provider = cfg.get("provider", DEFAULT_RERANK_PROVIDER)
    handler = RERANK_PROVIDERS.get(provider.lower(), _rerank_openai_compatible)
    try:
        return handler(cfg, query, documents, top_n)
    except Exception as e:
        logger.warning(f"rerank ({provider}) 调用失败: {e}")
        return []


_load_usage()


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
    """给 Memory 实例的 OpenAI client 打用量追踪补丁（首次加载时调用一次）"""
    try:
        from openai import OpenAI
        client = getattr(mem_instance, "client", None)
        if client is None or not isinstance(client, OpenAI):
            return
        _orig_create = client.chat.completions.create

        def _tracked_create(self, *args, **kwargs):
            resp = _orig_create(self, *args, **kwargs)
            if hasattr(resp, "usage") and resp.usage:
                _track_llm_usage(
                    resp.usage.prompt_tokens or 0,
                    resp.usage.completion_tokens or 0,
                    resp.usage.total_tokens or 0,
                )
            return resp

        client.chat.completions.create = _tracked_create.__get__(client, OpenAI)

        _orig_embed = client.embeddings.create

        def _tracked_embed(self, *args, **kwargs):
            resp = _orig_embed(self, *args, **kwargs)
            if hasattr(resp, "usage") and resp.usage:
                _track_embed_usage(resp.usage.total_tokens or 0)
            return resp

        client.embeddings.create = _tracked_embed.__get__(client, OpenAI)
        logger.info("✅ 用量追踪已打补丁")
    except Exception as e:
        logger.warning(f"用量追踪打补丁跳过: {e}")


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


def _normalize_user_id(user_id: str) -> str:
    """规范化 user ID：历史版本使用过自定义名字，现统一映射到 default，保证与存量库兼容。

    历史私有 user_id 不写进仓库：通过环境变量 AIDUMEM_LEGACY_USER_IDS
    （逗号分隔）由部署方按各自存量库配置，映射后老数据才能被新查询召回。
    不再硬编码 admin/user 映射，避免未来真实用户被静默并进 default。
    """
    if not user_id:
        return "default"
    legacy = set()
    extra = os.getenv("AIDUMEM_LEGACY_USER_IDS", "")
    if extra:
        legacy = {x.strip().lower() for x in extra.split(",") if x.strip()}
    return "default" if user_id.lower() in legacy else user_id


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
            # 启动时清理 Qdrant 锁（与生产对齐：先读配置再清理）
            _clear_qdrant_lock()
            m = Memory.from_config(cfg)
            _patch_usage_tracking(m)
            try:
                from ducky.add_speed import patch_llm_for_speed
                patch_llm_for_speed(m)
            except Exception as pe:
                logger.warning(f"speed patch on init skip: {pe}")
            sys._aidumem_singleton = m
            logger.info("✅ mem0 初始化成功 (用量追踪已激活)")
            return m
        except Exception as e:
            logger.error(f"mem0 初始化失败: {e}")
            raise HTTPException(500, f"mem0 不可用: {e}")

def reset_memory_singleton() -> None:
    """/reload 用：清空模块级 + sys 级单例。"""
    global m
    m = None
    if hasattr(sys, "_aidumem_singleton"):
        sys._aidumem_singleton = None


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
def register_salience_for_add(add_result):
    """mem0.add() 返回后注册显著性（非关键路径，失败只打 debug）"""
    try:
        results = add_result if isinstance(add_result, list) else add_result.get("results", [])
        for r in results:
            mid = r.get("id") or r.get("memory_id", "")
            content = r.get("memory") or r.get("data") or ""
            if mid:
                on_memory_added(mid, content=content)
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
