#!/usr/bin/env python3
"""
aiduMEM Memory Persistence — 跨查询 Session 持久化
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
J-space 启发：跨层持久存储 + 分时缓存多组中间表征。

— 核心逻辑 —
1. 一个 Session 是一个持续搜索上下文
2. Session 内所有搜索共享一个"搜索历史"和"工作区"
3. 后续搜索会在之前结果的基础上增量检索
4. Session 过期自动回收（默认 30 分钟）

— 与 J-space 的对应 —
J-space：中间概念跨层持久存在，不被新输入覆盖
Persistence：Session 内的搜索上下文跨请求保留
"""

import time, uuid, threading, logging
from typing import Optional
from collections import OrderedDict

from ducky.utils import quick_sim

logger = logging.getLogger("aiduMEM.persistence")

# ── 配置 ──
SESSION_TTL = 1800          # Session 过期时间 (30分钟)
SESSION_MAX = 100           # 全局最大 Session 数
SESSION_HISTORY_LIMIT = 50  # Session 内搜索历史上限

# ── 全局状态 ──
_sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()
_last_session_cleanup = 0


# ── 公共 API ──

def session_start(user_id: str) -> dict:
    """创建新搜索 Session。返回 {session_id, user_id, created, ttl}"""
    now = time.time()
    sid = f"ses_{uuid.uuid4().hex[:12]}"

    with _sessions_lock:
        # 清理过期
        _evict_stale_locked(now)

        # 容量控制
        if len(_sessions) >= SESSION_MAX:
            oldest_sid = min(_sessions, key=lambda k: _sessions[k]["last_active"])
            logger.debug(f"Session 淘汰: {oldest_sid}")
            del _sessions[oldest_sid]

        _sessions[sid] = {
            "user_id": user_id,
            "created": now,
            "last_active": now,
            "history": [],
            "pinned_ids": [],
            "context_text": "",
        }

    logger.info(f"Session 创建: {sid} (user={user_id})")
    return {
        "session_id": sid,
        "user_id": user_id,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now)),
        "ttl": SESSION_TTL,
    }


def session_search(
    memory,
    session_id: str,
    query: str,
    limit: int = 5,
    use_context: bool = True,
) -> dict:
    """Session 内搜索：融合历史上下文。返回 {results, session_id, hits_from_history, context_used}"""
    now = time.time()
    with _sessions_lock:
        sess = _sessions.get(session_id)
        if not sess:
            return {"status": "error", "detail": f"Session {session_id} 不存在或已过期"}
        sess["last_active"] = now

    sess = _sessions[session_id]
    result_ids = []
    results = []

    # ── Step 1: 历史记忆命中 ──
    if use_context and sess["history"]:
        history_text = " ".join(h["query"] for h in sess["history"][-5:])
        try:
            raw = memory.search(
                f"{query} {history_text[:200]}",
                filters={"user_id": sess["user_id"]},
                limit=limit * 2,
            )
            candidates = raw.get("results", raw) if isinstance(raw, dict) else raw
            results = list(candidates) if isinstance(candidates, list) else []
            result_ids = [r.get("id", "") for r in results[:limit]]
        except Exception as e:
            logger.warning(f"Session search 失败: {e}")
            results = []

    # ── Step 2: 如果上下文搜索无果，fallback 到纯 query ──
    if not results:
        try:
            raw = memory.search(query, filters={"user_id": sess["user_id"]}, limit=limit)
            candidates = raw.get("results", raw) if isinstance(raw, dict) else raw
            results = list(candidates) if isinstance(candidates, list) else []
        except Exception as e:
            logger.warning(f"Session fallback search 失败: {e}")

    # ── Step 3: 记录历史 ──
    with _sessions_lock:
        sess = _sessions.get(session_id)
        if sess:
            sess["history"].append({
                "query": query,
                "result_ids": [r.get("id", "")[:16] for r in results[:limit]],
                "ts": now,
            })
            if len(sess["history"]) > SESSION_HISTORY_LIMIT:
                sess["history"] = sess["history"][-SESSION_HISTORY_LIMIT:]

    # ── Step 4: 检查历史重叠 ──
    history_matches = [
        h for h in (sess.get("history", []) if sess else [])
        if quick_sim(query, h.get("query", "")) > 0.3
    ]

    return {
        "status": "ok",
        "session_id": session_id,
        "results": results[:limit],
        "context_used": use_context and len(history_matches) > 0,
        "history_length": len(sess.get("history", []) if sess else []),
    }


def session_pin(session_id: str, memory_id: str) -> dict:
    """Pin 一条记忆到 Session"""
    with _sessions_lock:
        sess = _sessions.get(session_id)
        if not sess:
            return {"status": "error", "detail": "Session 不存在"}
        if memory_id not in sess["pinned_ids"]:
            sess["pinned_ids"].append(memory_id)
    return {"status": "ok", "session_id": session_id, "pinned": memory_id}


def session_unpin(session_id: str, memory_id: str) -> dict:
    """Unpin 一条记忆"""
    with _sessions_lock:
        sess = _sessions.get(session_id)
        if not sess and memory_id in sess.get("pinned_ids", []):
            sess["pinned_ids"].remove(memory_id)
    return {"status": "ok", "session_id": session_id, "unpinned": memory_id}


def session_report(session_id: str) -> dict:
    """查看 Session 状态"""
    with _sessions_lock:
        sess = _sessions.get(session_id)
        if not sess:
            return {"status": "error", "detail": "Session 不存在"}

        return {
            "status": "ok",
            "session_id": session_id,
            "user_id": sess["user_id"],
            "created": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(sess["created"])),
            "age_seconds": int(time.time() - sess["created"]),
            "history_count": len(sess["history"]),
            "pinned_count": len(sess["pinned_ids"]),
            "pinned": [pid[:16] for pid in sess["pinned_ids"]],
            "recent_queries": [h["query"][:40] for h in sess["history"][-5:]],
        }


def session_end(session_id: str) -> dict:
    """结束 Session。成功时顺带返回 user_id，供上层触发 session_end 反思。"""
    with _sessions_lock:
        removed = _sessions.pop(session_id, None)
    if removed:
        logger.info(f"Session 结束: {session_id}")
        return {
            "status": "ok",
            "session_id": session_id,
            "user_id": removed.get("user_id", ""),
        }
    return {"status": "error", "detail": "Session 不存在"}


def session_list(user_id: str = None) -> list:
    """列出活跃 Session"""
    with _sessions_lock:
        result = []
        for sid, sess in _sessions.items():
            if user_id and sess["user_id"] != user_id:
                continue
            result.append({
                "session_id": sid,
                "user_id": sess["user_id"],
                "age_seconds": int(time.time() - sess["created"]),
                "history_count": len(sess["history"]),
            })
        return result


# ── 内部工具 ──

def _evict_stale_locked(now: float):
    """清理过期 Session（已持有锁）"""
    stale = [
        sid for sid, sess in _sessions.items()
        if now - sess["last_active"] > SESSION_TTL
    ]
    for sid in stale:
        del _sessions[sid]
    if stale:
        logger.info(f"Session 清理: {len(stale)} 个过期")
