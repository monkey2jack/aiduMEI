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
    """追踪硅基流动 rerank API 用量（免费模型，token 记着玩）"""
    today = _ensure_today()
    with _usage_lock:
        d = _llm_usage[today].setdefault("rerank", {"calls": 0, "input_tokens": 0, "total_tokens": 0})
        d["calls"] += 1
        d["input_tokens"] += input_tokens
        d["total_tokens"] += total_tokens
        _save_usage()


# ═══════════════════════════════════════════════
# §1b 硅基流动 Rerank（懒加载配置 + requests 直发）
# ═══════════════════════════════════════════════
_RERANK_CONFIG_CACHE: Optional[dict] = None


def _load_rerank_config() -> dict:
    """从 mem0_config 或环境读 reranker 配置，返回 {api_key, base_url, model}"""
    global _RERANK_CONFIG_CACHE
    if _RERANK_CONFIG_CACHE is not None:
        return _RERANK_CONFIG_CACHE
    cfg = {}
    try:
        if os.path.exists(MEM0_CONFIG):
            with open(MEM0_CONFIG) as f:
                j = json.load(f)
            rerank = j.get("reranker", {}).get("config", {})
            cfg["model"] = rerank.get("model", "BAAI/bge-reranker-v2-m3")
            cfg["base_url"] = rerank.get("openai_base_url", "https://api.siliconflow.cn/v1")
            api_key = rerank.get("api_key", "")
            if api_key == "__SF_KEY__" or not api_key:
                kp = os.path.join(BASE_DIR, ".sf_key")
                if os.path.exists(kp):
                    with open(kp) as fk:
                        api_key = fk.read().strip()
            cfg["api_key"] = api_key
        else:
            # 兜底：跟 embedding 一样
            cfg = {
                "model": "BAAI/bge-reranker-v2-m3",
                "base_url": "https://api.siliconflow.cn/v1",
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
    调用硅基流动 BAAI/bge-reranker-v2-m3 做重排序。
    返回 [{index, relevance_score}, ...] 按分数降序。
    失败返回空列表，不阻断检索主链路。
    """
    if not documents:
        return []
    cfg = _load_rerank_config()
    api_key = cfg.get("api_key", "")
    if not api_key:
        return []
    model = cfg.get("model", "BAAI/bge-reranker-v2-m3")
    base_url = cfg.get("base_url", "https://api.siliconflow.cn/v1")
    try:
        import requests as req
        r = req.post(
            f"{base_url}/rerank",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "query": query,
                "documents": documents,
                "top_n": min(top_n, len(documents)),
            },
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            results = data.get("results", [])
            # 硅基流动 token 在 meta.tokens 里
            meta = data.get("meta", {})
            tokens = meta.get("tokens", {})
            if tokens:
                _track_rerank_usage(
                    input_tokens=tokens.get("input_tokens", 0),
                    total_tokens=tokens.get("input_tokens", 0),
                )
            return results
        else:
            logger.debug(f"rerank API {r.status_code}: {r.text[:200]}")
            return []
    except Exception as e:
        logger.warning(f"rerank 调用失败: {e}")
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
    if llm_key == "__SF_KEY__" or not llm_key:
        kp = os.path.join(base, ".llm_key")
        if os.path.exists(kp):
            with open(kp) as f:
                cfg["llm"]["config"]["api_key"] = f.read().strip()
    return cfg


def get_memory():
    """延迟初始化 mem0 单例，绑定到 sys 命名空间防止跨模块双重导入"""
    global m
    if hasattr(sys, "_aidumem_singleton") and sys._aidumem_singleton is not None:
        m = sys._aidumem_singleton
        return m
    if m is not None:
        sys._aidumem_singleton = m
        return m

    with _mem_init_lock:
        if hasattr(sys, "_aidumem_singleton") and sys._aidumem_singleton is not None:
            m = sys._aidumem_singleton
            return m
        try:
            if Memory is None:
                raise RuntimeError("mem0 SDK 未加载")
            cfg = json.loads(open(MEM0_CONFIG).read())
            cfg = _resolve_api_keys(cfg)
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
        from ducky.layer1_selfcheck import layer1_add_wrapper
        _layer1 = layer1_add_wrapper
    return _layer1


def lazy_import_funnel():
    global _funnel
    if _funnel is None:
        from ducky.recall_funnel import funnel_search
        _funnel = funnel_search
    return _funnel


def lazy_import_hybrid():
    global _hybrid
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
    for r in (results if isinstance(results, list) else results.get("results", [])):
        if isinstance(r, dict):
            mid = r.get("id") or r.get("memory_id", "")
            if mid:
                on_memory_accessed(mid)
