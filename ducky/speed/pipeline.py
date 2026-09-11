"""aiduMEM speed · 高速写入主流程"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from ducky.bank_contract import DEFAULT_BANK_ID
from ducky.speed.cache import cache_get, cache_key, cache_set
from ducky.speed.config import load_speed_cfg, messages_to_text
from ducky.speed.fastpath import try_fastpath_text
from ducky.security.injection_guard import validate_and_sanitize_memory_content
from ducky.failure_ledger import feature_failed

logger = logging.getLogger("aiduMEM.speed")


# ── 子步骤（v20.5.1 · T-13 圈复杂度整改）─────────────────────────────
#
# run_add_pipeline 曾是 CC 53（radon F 级）的巨函数：注入闸门、抽取缓存、
# 去重更新、容量合并、快路径/LLM 双出口、FTS 索引、结果摘要全堆在一个
# 函数体里。这里只做**换骨架**（与 scoring.py v20.4.1a 同款打法）：每道
# 子步骤一个可独立测试的函数，编排函数只负责流程组合；判据、取字段顺序、
# 遥测键、返回值结构逐行未动。


def _gate_injection(text, messages_json, user_id: str):
    """[P0 Gate] 终审注入防护：写入前全量执行清洗与越权拦截。

    拦截直接抛 ValueError；清洗命中时 text 换净化版，且末条消息的
    content 同步回写（保结构），返回 (text, messages_json)。
    """
    is_safe, sanitized_text, threat = validate_and_sanitize_memory_content(text)
    if not is_safe:
        logger.warning(f"🛡️ [SpeedPipeline] 拦截注入攻击 user_id={user_id}: {threat}")
        raise ValueError(f"安全风控拦截：检测到非法注入模式 ({threat})")
    if sanitized_text != text:
        text = sanitized_text
        if isinstance(messages_json, list) and len(messages_json) > 0 and isinstance(messages_json[-1], dict) and "content" in messages_json[-1]:
            messages_json[-1]["content"] = sanitized_text
    return text, messages_json


def _read_extract_cache(user_id: str, text: str, metadata: dict, bank_id: str,
                        timing: dict, details: dict, t0: float):
    """0) 抽取缓存（仅 infer 路径）。返回 (ck, 命中结果|None)。

    ck 无论命中与否都要算 —— 编排层末尾的 cache_set 还用同一把钥匙。
    """
    ck = cache_key(user_id, text, "infer", bank_id=bank_id)
    cached = cache_get(ck) if text else None
    if cached is not None and not metadata.get("no_cache"):
        timing["cache_hit"] = 1
        details["cache_hit"] = True
        details["ms"] = int((time.time() - t0) * 1000)
        details["timing_ms"] = timing
        # 缓存存的是上次完整返回
        if isinstance(cached, dict):
            out = dict(cached)
            out["details"] = {**(out.get("details") or {}), **details}
            out["details"]["cache_hit"] = True
            return ck, out
    return ck, None


def _dedup_update_existing(memory, user_id: str, text: str, metadata: dict,
                           bank_id: str, timing: dict, details: dict):
    """1) 去重：命中则就地 update。返回 (existing_id, action)。

    update 失败回落 (None, "new")，流程继续走新增 —— 与抽函数前一致。
    """
    from ducky.layer1_selfcheck import dedup_check

    t1 = time.time()
    existing_id = dedup_check(memory, user_id, text, bank_id=bank_id) if text else None
    timing["dedup"] = int((time.time() - t1) * 1000)
    action = "new"
    if existing_id:
        try:
            memory.update(existing_id, text, metadata=metadata)
            action = "updated"
            details["existing_id"] = existing_id
            logger.info(f"Layer1 去重更新: {existing_id[:16]}")
        except Exception:
            existing_id = None
    return existing_id, action


def _merge_for_capacity(memory, user_id: str, bank_id: str, speed: dict,
                        force_sync: bool, timing: dict, details: dict) -> bool:
    """2) 容量检查（合并默认异步，不堵热路径）。返回是否当场发生了合并成组。"""
    from ducky.layer1_selfcheck import auto_merge_similar, check_capacity

    t2 = time.time()
    cap = check_capacity(memory, user_id, bank_id=bank_id)
    timing["capacity"] = int((time.time() - t2) * 1000)
    details["capacity"] = cap
    if not cap.get("needs_merge"):
        return False
    if speed.get("capacity_merge_async", True) and not force_sync:
        details["merge_scheduled"] = True
        try:
            threading.Thread(
                # bank_id 用默认参数绑死：这个 lambda 在别的线程里跑，
                # 靠闭包读外层变量会把「删哪个域」交给时序去决定。
                target=lambda b=bank_id: auto_merge_similar(memory, user_id, bank_id=b),
                daemon=True,
                name="aiduMEM-cap-merge",
            ).start()
        except Exception as e:
            logger.debug(f"async merge schedule skip: {e}")
        return False
    t2b = time.time()
    merge_result = auto_merge_similar(memory, user_id, bank_id=bank_id)
    timing["merge"] = int((time.time() - t2b) * 1000)
    details["merge"] = merge_result
    return merge_result.get("merged_groups", 0) > 0


def _add_fastpath_or_llm(memory, messages_json, text: str, user_id: str,
                         metadata: dict, speed: dict, bank_id: str,
                         timing: dict, details: dict, action: str):
    """3) 快路径 or LLM add。返回 (add_result, action)。

    快路径命中把 action 改判 "fastpath"；LLM/本地出口**不改** action ——
    前面合并出口置下的 "merged" 必须原样穿透。
    """
    from ducky.mem0_runtime import register_salience_for_add

    t3 = time.time()
    fast_fact = None
    if speed.get("fastpath_enabled", True) and not metadata.get("no_fastpath"):
        fast_fact = try_fastpath_text(text)

    if fast_fact:
        # infer=False：跳过 LLM，直接写规范化事实
        add_result = memory.add(
            [{"role": "user", "content": fast_fact}],
            user_id=user_id,
            metadata={**metadata, "fastpath": True, "source_text": text[:200]},
            infer=False,
        )
        details["fastpath_fact"] = fast_fact
        timing["llm_add"] = int((time.time() - t3) * 1000)
        timing["path"] = "fastpath"
        register_salience_for_add(add_result, user_id=user_id, bank_id=bank_id)
        return add_result, "fastpath"

    # 长文提示：通过 metadata 标记，不改 mem0 SDK
    if len(text) >= int(speed.get("long_text_chars", 2500)):
        metadata = {
            **metadata,
            "long_text": True,
            "extract_hint": "优先拆成多条自洽事实，避免冗长叙述",
        }
    from ducky.gear import should_try_llm
    _use_llm = should_try_llm()
    add_result = memory.add(
        messages_json, user_id=user_id, metadata=metadata,
        infer=_use_llm,
    )
    timing["llm_add"] = int((time.time() - t3) * 1000)
    timing["path"] = "llm" if _use_llm else "local"
    register_salience_for_add(add_result, user_id=user_id, bank_id=bank_id)
    return add_result, action


def _index_fts_after_add(action: str, existing_id, add_result, text: str,
                         metadata: dict, user_id: str, bank_id: str,
                         timing: dict) -> None:
    """4) FTS：updated 重索引既有行；否则索引 add_result 全部结果。失败只降级。"""
    t4 = time.time()
    try:
        from ducky.text_fts import _index_memory

        # 甲14：metadata 里没有 category ≠ 分类是空的。这里的 "updated" 分支是
        # 重索引既有行，写死空串等于每次快路径更新都抹掉一次分类。
        category = (metadata or {}).get("category")
        if action == "updated" and existing_id:
            _index_memory(existing_id, text, user_id=user_id, category=category, bank_id=bank_id)
        elif add_result is not None:
            results = (
                add_result
                if isinstance(add_result, list)
                else (add_result.get("results") if isinstance(add_result, dict) else [])
            )
            if isinstance(results, list):
                for r in results:
                    if not isinstance(r, dict):
                        continue
                    mid = r.get("id") or r.get("memory_id")
                    content = r.get("memory") or r.get("data") or text
                    if mid and content:
                        _index_memory(mid, content, user_id=user_id, category=category, bank_id=bank_id)
    except Exception as e:
        feature_failed("index_memory", e)
        logger.debug(f"FTS 索引跳过: {e}")
    timing["fts"] = int((time.time() - t4) * 1000)


def _summarize_add_result(add_result):
    """结果摘要：stored 计数 + 前 8 条 memories（正文截 200 字符）。"""
    stored = 0
    memories = []
    if isinstance(add_result, dict):
        rs = add_result.get("results") or []
        if isinstance(rs, list):
            stored = len(rs)
            for r in rs[:8]:
                if isinstance(r, dict):
                    memories.append(
                        {
                            "id": r.get("id") or r.get("memory_id"),
                            "memory": (r.get("memory") or r.get("data") or "")[:200],
                            "event": r.get("event"),
                        }
                    )
    elif isinstance(add_result, list):
        stored = len(add_result)
    return stored, memories


def run_add_pipeline(
    memory,
    messages_json,
    user_id: str,
    metadata: dict,
    *,
    force_sync: bool = False,
    bank_id: str = DEFAULT_BANK_ID,
) -> dict:
    """
    高速写入主流程（供 layer1 / 异步 worker 调用）。
    """
    t0 = time.time()
    timing = {}
    details: dict[str, Any] = {}
    action = "new"
    add_result = None
    existing_id = None
    speed = load_speed_cfg()
    text = messages_to_text(messages_json)
    # 🔴v20：与 layer1_add_wrapper 同理 —— metadata 是 bank_id 进入向量 payload
    # 的唯一通道，在入口盖一次戳，下面 update/fastpath/llm 三条写入路径都继承。
    from ducky.bank_contract import stamp_bank_metadata
    metadata = stamp_bank_metadata(metadata, bank_id)

    text, messages_json = _gate_injection(text, messages_json, user_id)

    # 0) 抽取缓存（仅 infer 路径）
    ck, cached_out = _read_extract_cache(user_id, text, metadata, bank_id, timing, details, t0)
    if cached_out is not None:
        return cached_out

    # 1) 去重
    existing_id, action = _dedup_update_existing(memory, user_id, text, metadata, bank_id, timing, details)

    if not existing_id:
        # 2) 容量检查（合并默认异步，不堵热路径）
        if _merge_for_capacity(memory, user_id, bank_id, speed, force_sync, timing, details):
            action = "merged"

        # 3) 快路径 or LLM add
        add_result, action = _add_fastpath_or_llm(
            memory, messages_json, text, user_id, metadata, speed, bank_id,
            timing, details, action)

    # 4) FTS
    _index_fts_after_add(action, existing_id, add_result, text, metadata, user_id, bank_id, timing)

    total_ms = int((time.time() - t0) * 1000)
    timing["total"] = total_ms
    details["ms"] = total_ms
    details["timing_ms"] = timing

    # 结果摘要
    stored, memories = _summarize_add_result(add_result)

    out = {
        "status": "ok",
        "action": action,
        "details": details,
        "stored": stored,
        "memories": memories,
    }

    # 写缓存（仅成功的 llm/fastpath 新写）
    if action in ("new", "fastpath", "merged") and text and not metadata.get("no_cache"):
        cache_set(ck, out)

    logger.info(
        f"add_speed action={action} total={total_ms}ms "
        f"dedup={timing.get('dedup')} llm={timing.get('llm_add')} "
        f"path={timing.get('path')} stored={stored}"
    )
    return out
