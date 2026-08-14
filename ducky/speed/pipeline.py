"""aiduMEM speed · 高速写入主流程"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from ducky.speed.cache import cache_get, cache_key, cache_set
from ducky.speed.config import load_speed_cfg, messages_to_text
from ducky.speed.fastpath import try_fastpath_text
from ducky.security.injection_guard import validate_and_sanitize_memory_content

logger = logging.getLogger("aiduMEM.speed")


def run_add_pipeline(
    memory,
    messages_json,
    user_id: str,
    metadata: dict,
    *,
    force_sync: bool = False,
) -> dict:
    """
    高速写入主流程（供 layer1 / 异步 worker 调用）。
    """
    from ducky.layer1_selfcheck import (
        auto_merge_similar,
        check_capacity,
        dedup_check,
    )
    from ducky.mem0_runtime import register_salience_for_add

    t0 = time.time()
    timing = {}
    details: dict[str, Any] = {}
    action = "new"
    add_result = None
    existing_id = None
    speed = load_speed_cfg()
    text = messages_to_text(messages_json)
    metadata = dict(metadata or {})

    # [P0 Gate] 终审注入防护：写入前全量执行清洗与越权拦截
    is_safe, sanitized_text, threat = validate_and_sanitize_memory_content(text)
    if not is_safe:
        logger.warning(f"🛡️ [SpeedPipeline] 拦截注入攻击 user_id={user_id}: {threat}")
        raise ValueError(f"安全风控拦截：检测到非法注入模式 ({threat})")
    if sanitized_text != text:
        text = sanitized_text
        if isinstance(messages_json, list) and len(messages_json) > 0 and isinstance(messages_json[-1], dict) and "content" in messages_json[-1]:
            messages_json[-1]["content"] = sanitized_text

    # 0) 抽取缓存（仅 infer 路径）
    ck = cache_key(user_id, text, "infer")
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
            return out

    # 1) 去重
    t1 = time.time()
    existing_id = dedup_check(memory, user_id, text) if text else None
    timing["dedup"] = int((time.time() - t1) * 1000)
    if existing_id:
        try:
            memory.update(existing_id, text, metadata=metadata)
            action = "updated"
            details["existing_id"] = existing_id
            logger.info(f"Layer1 去重更新: {existing_id[:16]}")
        except Exception:
            existing_id = None

    if not existing_id:
        # 2) 容量检查（合并默认异步，不堵热路径）
        t2 = time.time()
        cap = check_capacity(memory, user_id)
        timing["capacity"] = int((time.time() - t2) * 1000)
        details["capacity"] = cap
        if cap.get("needs_merge"):
            if speed.get("capacity_merge_async", True) and not force_sync:
                details["merge_scheduled"] = True
                try:
                    threading.Thread(
                        target=lambda: auto_merge_similar(memory, user_id),
                        daemon=True,
                        name="aiduMEM-cap-merge",
                    ).start()
                except Exception as e:
                    logger.debug(f"async merge schedule skip: {e}")
            else:
                t2b = time.time()
                merge_result = auto_merge_similar(memory, user_id)
                timing["merge"] = int((time.time() - t2b) * 1000)
                details["merge"] = merge_result
                if merge_result.get("merged_groups", 0) > 0:
                    action = "merged"

        # 3) 快路径 or LLM add
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
            action = "fastpath"
            details["fastpath_fact"] = fast_fact
            timing["llm_add"] = int((time.time() - t3) * 1000)
            timing["path"] = "fastpath"
            register_salience_for_add(add_result)
        else:
            # 长文提示：通过 metadata 标记，不改 mem0 SDK
            if len(text) >= int(speed.get("long_text_chars", 2500)):
                metadata = {
                    **metadata,
                    "long_text": True,
                    "extract_hint": "优先拆成多条自洽事实，避免冗长叙述",
                }
            add_result = memory.add(messages_json, user_id=user_id, metadata=metadata)
            timing["llm_add"] = int((time.time() - t3) * 1000)
            timing["path"] = "llm"
            register_salience_for_add(add_result)

    # 4) FTS
    t4 = time.time()
    try:
        from ducky.text_fts import _index_memory

        category = (metadata or {}).get("category", "")
        if action == "updated" and existing_id:
            _index_memory(existing_id, text, user_id=user_id, category=category)
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
                        _index_memory(mid, content, user_id=user_id, category=category)
    except Exception as e:
        logger.debug(f"FTS 索引跳过: {e}")
    timing["fts"] = int((time.time() - t4) * 1000)

    total_ms = int((time.time() - t0) * 1000)
    timing["total"] = total_ms
    details["ms"] = total_ms
    details["timing_ms"] = timing

    # 结果摘要
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
