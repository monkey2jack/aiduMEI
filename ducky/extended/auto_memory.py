"""ducky.extended.auto_memory — 后台自动记忆 + 过期清理"""
from __future__ import annotations

import json
import logging
import os
from ducky.shutdown import sleep as _shutdown_sleep
from collections import defaultdict
from datetime import datetime, timezone

from ducky.bank_contract import DEFAULT_BANK_ID
from ducky.utils import DATA_DIR, DEFAULT_USER_ID
from fastapi import HTTPException

logger = logging.getLogger("aiduMEM.extended")
AUTO_MEMORY_STATE = os.path.join(DATA_DIR, "auto_memory_state.json")
_AUTO_MD = {"source": "auto_memory"}

# 由 register_extended_routes 注入
_get_db = None
_get_facts_conn = None
get_memory = None


def bind_runtime(*, get_memory_fn, get_db_fn, get_facts_conn_fn) -> None:
    """注入运行时依赖（避免循环 import）。"""
    global get_memory, _get_db, _get_facts_conn
    get_memory = get_memory_fn
    _get_db = get_db_fn
    _get_facts_conn = get_facts_conn_fn


def _auto_read_last_id():
    if os.path.exists(AUTO_MEMORY_STATE):
        with open(AUTO_MEMORY_STATE) as f:
            return json.load(f).get("last_msg_id", 0)
    return 0

def _auto_write_last_id(msg_id):
    os.makedirs(os.path.dirname(AUTO_MEMORY_STATE), exist_ok=True)
    with open(AUTO_MEMORY_STATE, "w") as f:
        json.dump({"last_msg_id":msg_id,"last_run":datetime.now().isoformat()}, f)

def _auto_fetch_new_messages(last_id, limit=200):
    # 宿主 Agent 会话库路径由环境变量提供，未配置时不做自动记忆
    host_state_db = os.environ.get("AIDUMEM_HOST_STATE_DB", "")
    if not host_state_db or not os.path.exists(host_state_db):
        logger.debug("未配置 AIDUMEM_HOST_STATE_DB，跳过后台自动记忆抓取")
        return []
    conn = _get_db(host_state_db)
    try:
        rows = conn.execute("""SELECT id,role,content,session_id,created_at
            FROM messages WHERE id>? AND role IN ('user','assistant')
            ORDER BY id ASC LIMIT ?""", (last_id, limit)).fetchall()
    except Exception:
        rows = conn.execute("""SELECT id,role,content,session_id,NULL as created_at
            FROM messages WHERE id>? AND role IN ('user','assistant')
            ORDER BY id ASC LIMIT ?""", (last_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def _auto_group_by_session(messages):
    groups = defaultdict(list)
    for m in messages: groups[m.get("session_id","")].append(m)
    return groups

def _auto_format_conversation(msgs):
    lines = []
    for m in msgs:
        role = "user" if m["role"]=="user" else "AI"
        lines.append(f"{role}: {m['content'][:200]}")
    return "\n".join(lines)

def _auto_gate_session(msgs, sid):
    """f0.8 ②：与 /add 同一道拦截（write_policy.enforce_messages）——每条消息先清洗、中和、
    打码（与 /add 同序），再交给开了拦截的原则：P1 机器噪声 / 思考过程进墓碑 7 天可恢复，
    P8 不留正文。返回剩下要总结的消息；拦截失败时 enforce_messages 自己 fail-open。"""
    from ducky.security.injection_guard import neutralize_messages_struct, sanitize_messages_struct
    from ducky.write_policy import enforce_messages, redact_messages_struct
    try:
        struct = [{"role": "user" if m["role"] == "user" else "assistant", "content": m.get("content") or ""}
                  for m in msgs]
        struct, _kinds = redact_messages_struct(neutralize_messages_struct(sanitize_messages_struct(struct)))
    except (ValueError, TypeError, KeyError, AttributeError) as e:   # fail-open: 整段照旧走后面的闸门
        logger.warning("auto_memory: 会话 %s 逐条拦截跳过: %s", sid, type(e).__name__)
        return msgs, []
    kept, gated = enforce_messages(struct, _AUTO_MD, user_id=DEFAULT_USER_ID, bank_id=DEFAULT_BANK_ID)
    if gated:
        logger.info("auto_memory: 会话 %s 拦下 %d 条: %s", sid, len(gated),
                    ",".join(f"{g['principle']}:{g['reason']}" for g in gated))
    return list(kept or []), gated


def _run_auto_memory():
    if get_memory is None: return {"status":"error","message":"mem0 not initialized"}
    last_id = _auto_read_last_id()
    messages = _auto_fetch_new_messages(last_id)
    if not messages: return {"status":"ok","new_messages":0}
    sessions = _auto_group_by_session(messages)
    extracted = 0
    gated_total = 0
    for sid, msgs in sessions.items():
        msgs, _gated = _auto_gate_session(msgs, sid)
        gated_total += len(_gated)
        text = _auto_format_conversation(msgs)
        if len(text) > 50:
            # 跨消息的忘记规则（规则的词分在两条里）：整段再过一次同一道闸
            from ducky.write_policy import enforce_messages
            text, _g2 = enforce_messages(text, _AUTO_MD, user_id=DEFAULT_USER_ID, bank_id=DEFAULT_BANK_ID)
            gated_total += len(_g2)
        if len(text) > 50:
            # f0.7.5（外审 qwen F-04）：后台环此前直调内核，绕过 /add 的注入检测、
            # 控制字符清洗、边界中和与原则 7 打码。现在走同一组闸门；被拒的会话跳过。
            try:
                from ducky.scope_auth import sanitize_memory_or_raise
                from ducky.security.injection_guard import neutralize_boundary_markers
                from ducky.write_policy import hard_block_reason, redact_forbidden
                text = neutralize_boundary_markers(sanitize_memory_or_raise(text))
                text, _kinds = redact_forbidden(text)
                if hard_block_reason(text, summary=True):
                    logger.warning("auto_memory: 会话 %s 被原则 7 整段拒收，跳过", sid)
                    continue
            except (HTTPException, ValueError, TypeError) as e:
                logger.warning("auto_memory: 会话 %s 未通过写入闸门，跳过: %s", sid, type(e).__name__)
                continue
            try:
                from ducky.gear import should_try_llm
                _infer = should_try_llm()
                get_memory().add([{"role":"user","content":f"总结以下对话的关键信息:\n{text}"}], user_id=DEFAULT_USER_ID, infer=_infer)
                extracted += 1
            except Exception as e:
                logger.error(f"auto_memory extract 失败: {e}")
    _auto_write_last_id(messages[-1]["id"])
    return {"status":"ok","new_messages":len(messages),"sessions":len(sessions),"extracted":extracted,
            "gated":gated_total}

def auto_memory_background_loop():
    while True:
        try: _run_auto_memory()
        except Exception as e: logger.error(f"auto_memory 后台: {e}")
        if not _shutdown_sleep(600):
            return  # 停机请求（P2-20）：收尾退出

def _auto_expire_loop():
    """后台线程：每小时清理过期事实"""
    while True:
        try:
            db = _get_facts_conn()
            now = datetime.now(timezone.utc).isoformat()
            expired = db.execute(
                "SELECT COUNT(*) FROM facts WHERE expires_at IS NOT NULL AND expires_at < ? AND archived=0",
                (now,)).fetchone()[0]
            if expired > 0:
                db.execute("UPDATE facts SET archived=1, archived_at=? WHERE expires_at IS NOT NULL AND expires_at < ? AND archived=0", (now, now))
                db.commit()
                logger.info(f"⏳ 自动遗忘: {expired} 条过期事实已归档")
            db.close()
        except Exception as e:
            logger.error(f"自动遗忘异常: {e}")
        if not _shutdown_sleep(3600):
            return  # 停机请求（P2-20）：收尾退出

def _wrapper_auto_memory():
    try: _run_auto_memory()
    except Exception as e: logger.error(f"auto_memory 失败: {e}")
