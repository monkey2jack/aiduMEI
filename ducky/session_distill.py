"""ducky.session_distill — 会话精华萃取（v21.2.0）

一个会话结束时，把「这一程最值得记住的事」提炼成一两句话，单独存一条。

**为什么不是「再归纳一次」**
用户原话：那些随口说的一句话、一起解决的一个难题、某个决定的瞬间，现在都
淹没在归纳条目里了。普通写入把每轮拆成若干条语义事实，颗粒是「事实」；
精华的颗粒是「这一程」。两者不可互相替代——所以精华单独成条、单独一个泳道，
而不是给某条已有记忆加个标记。

**情感权重从哪来（不发明新维度）**
本仓 `salience/config.py` 早就有 `emotion` 关键词表。这里直接数该会话内容里
命中了几个情绪词，作为 `emotion_hits` 写进 metadata，并据此给初始显著性一个
有界的加成。每个数字都能回溯到那张既有词表，不是拍脑袋的分数。

**为什么精华不进 emotion 泳道**
emotion 是 150% 快衰减 —— 那是给「今天有点烦」这类波动用的，设计没错。
但精华是「这一程最值得记住的」，让它比普通记忆忘得更快是荒谬的。所以另立
`distill` 泳道（0.3 慢衰减），理由写在 config 里。

**LLM 不可用时不许静默不产出**
挡位关着、超时、返回空——都退到确定性降级：取该会话显著性最高的若干条拼接。
降级产出会在 metadata 里标明 `distill_mode=fallback`，不冒充 LLM 提炼。
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from typing import Any

from ducky.bank_contract import DEFAULT_BANK_ID
from ducky.utils import DEFAULT_USER_ID, get_facts_conn

logger = logging.getLogger("aiduMEM.distill")

# 一个会话至少要有这么多条带 session 的写入才值得萃取。
# 1~2 条的会话（打个招呼就走）提炼出来的只会是噪音。
from ducky.env_config import int_env as _int_env  # noqa: E402

MIN_MEMORIES = _int_env("AIDUMEI_DISTILL_MIN_MEMORIES", 3, minimum=1)
MAX_SOURCE = _int_env("AIDUMEI_DISTILL_MAX_SOURCE", 24, minimum=1)


def _emotion_hits(text: str) -> int:
    """数情绪词命中数。词表来自既有的 salience 配置，不另起炉灶。"""
    try:
        from ducky.salience.config import LANE_KEYWORDS
    except ImportError:
        return 0
    return sum(1 for kw in LANE_KEYWORDS.get("emotion", []) if kw in text)


def _collect_raw_session_turns(session_id: str, user_id: str,
                               bank_id: str) -> list[dict[str, Any]]:
    """Read only committed verbatim turns for this exact session and scope.

    /add stores these before dispatching an async job, including local/lite
    engine paths that may never create a mem0 sidecar.  A bounded recent
    window retains the end of a long session where the final decision often
    occurs.
    """
    conn = get_facts_conn()
    try:
        from ducky.bank_contract import make_scope
        from ducky.scope_sql import scope_clause
        _scope, _params = scope_clause(make_scope(user_id, bank_id), flavor="canonical")
        rows = conn.execute(
            "SELECT id, content, recorded_at, created_at FROM verbatim_turns "
            f"WHERE session_id=?{_scope} "
            "ORDER BY id DESC LIMIT ?",
            (session_id, *_params, MAX_SOURCE),
        ).fetchall()
    except sqlite3.Error as exc:
        logger.debug("会话原文候补不可用 session=%s: %s", session_id[:32], exc)
        return []
    finally:
        conn.close()
    rows.reverse()
    return [
        {"ref": f"verbatim:{row[0]}", "turn": index,
         "created_at": row[2] or row[3], "text": str(row[1] or "").strip()}
        for index, row in enumerate(rows, 1) if str(row[1] or "").strip()
    ]


def _collect_sidecar_refs(session_id: str, user_id: str,
                          bank_id: str) -> list[tuple[Any, Any, Any]]:
    """Read semantic refs for one session, excluding prior distill outputs."""
    conn = get_facts_conn()
    try:
        from ducky.bank_contract import make_scope
        from ducky.scope_sql import scope_clause
        _scope, _params = scope_clause(make_scope(user_id, bank_id), flavor="canonical")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_epistemic)")}
        sql = (
            "SELECT memory_ref, origin_turn, created_at FROM memory_epistemic "
            f"WHERE origin_session_id = ?{_scope}"
        )
        if "origin_agent" in cols:
            sql += " AND COALESCE(origin_agent, '')!='session-distill'"
        sql += " ORDER BY origin_turn ASC, created_at ASC LIMIT ?"
        cur = conn.execute(sql, (session_id, *_params, MAX_SOURCE))
        return [(r[0], r[1], r[2]) for r in cur.fetchall()]
    except sqlite3.Error as exc:
        logger.warning("会话记忆取用失败 session=%s: %s", session_id[:32], exc)
        return []
    finally:
        conn.close()


def collect_session_memories(session_id: str, *, user_id: str = "",
                             bank_id: str = "") -> list[dict[str, Any]]:
    """取这个会话已提交的原文；旧写线才回退到语义 sidecar。

    /add 在异步语义抽取前已提交 verbatim。sidecar 的条数与轮次并非一一
    对应，无法凭其达到 MIN_MEMORIES 就认定最新轮次也已完成。原文达到
    萃取门槛时优先使用完整的已提交窗口；没有原文的旧写线仍可使用 sidecar。
    两路都用精确的 session/user/bank 作用域，不猜时间窗。
    """
    if not session_id:
        return []
    uid = user_id or DEFAULT_USER_ID
    bid = bank_id or DEFAULT_BANK_ID
    raw_rows = _collect_raw_session_turns(session_id, uid, bid)
    if len(raw_rows) >= MIN_MEMORIES:
        return raw_rows
    rows: list[dict[str, Any]] = []
    refs = _collect_sidecar_refs(session_id, uid, bid)
    if not refs:
        return raw_rows

    # 原文取用分两步，顺序是有讲究的：
    #   ① salience 的 content_preview —— 本地 SQLite、一次批量 SQL 拿完。
    #   ② 缺的再逐条问 mem0（权威但慢，且要走向量库）。
    # 生产实测 salience 里只有约 57% 的行带 preview，所以第二步不是可选的；
    # 但把它放第二步，是为了让常见情况只花一次本地查询。
    preview: dict[str, str] = {}
    try:
        from ducky.salience.core import get_batch_salience_records
        for ref, rec in (get_batch_salience_records([r[0] for r in refs]) or {}).items():
            txt = str((rec or {}).get("content_preview") or "").strip()
            if txt:
                preview[ref] = txt
    except (ImportError, sqlite3.Error) as exc:
        # 只收窄到「这一步自己可能出的错」：模块缺失 / 库读失败。
        # 富化拿不到就少几条原文，不该中断萃取；但别用宽捕获盖住真 bug。
        logger.debug("显著性预览批量取用降级: %s", type(exc).__name__)

    missing = 0
    _mem = None
    for ref, turn, created in refs:
        text = preview.get(ref, "")
        if not text:
            if _mem is None:
                try:
                    from ducky.mem0_runtime import get_memory
                    _mem = get_memory()
                except (ImportError, RuntimeError, OSError, ValueError):
                    _mem = False
            if _mem:
                try:
                    got = _mem.get(ref) or {}
                    text = str(got.get("memory") or got.get("text") or "")
                except (KeyError, AttributeError, TypeError, ValueError, OSError):
                    # 单条取不到不中断整批；下面的 missing 计数会把它说出来。
                    text = ""
        if not text:
            missing += 1
            continue
        rows.append({"ref": ref, "turn": turn, "created_at": created, "text": text})
    if refs and not rows:
        logger.warning("会话 %s 有 %d 条 sidecar 登记，但一条原文都取不到 —— "
                       "sidecar 与主库可能已失配", session_id[:32], len(refs))
    elif missing:
        logger.info("会话 %s 萃取：%d/%d 条原文取不到，按现有的提炼",
                    session_id[:32], missing, len(refs))
    return rows if len(rows) >= len(raw_rows) else raw_rows


def _fallback_summary(rows: list[dict[str, Any]]) -> str:
    """LLM 不可用时的确定性降级。

    不做任何"理解"，只挑最长的两条原文拼接 —— 长度是**可复现**的代理指标，
    而随便挑一条或挑最新一条都会让降级产出随机漂移。降级就该老实承认自己是
    降级（metadata 里标 fallback），不冒充提炼。
    """
    best = sorted(rows, key=lambda r: len(r["text"]), reverse=True)[:2]
    parts = [r["text"].strip().replace("\n", " ")[:120] for r in best if r["text"].strip()]
    return "；".join(parts)


def distill_session(session_id: str, *, user_id: str = "",
                    bank_id: str = "") -> dict[str, Any]:
    """萃取一个会话的精华并落库。返回结构化结果（含未产出的原因）。"""
    uid = user_id or DEFAULT_USER_ID
    bid = bank_id or DEFAULT_BANK_ID
    rows = collect_session_memories(session_id, user_id=uid, bank_id=bid)
    if len(rows) < MIN_MEMORIES:
        # 不是故障：短会话本来就没什么可提炼的。但要把原因说出来，
        # 否则「这次怎么没精华」又变成一个查不出来的问题。
        return {"status": "skipped", "reason": "too_short",
                "session_id": session_id, "source_count": len(rows),
                "min_required": MIN_MEMORIES}

    joined = "\n".join(f"- {r['text']}" for r in rows)
    emo = _emotion_hits(joined)

    summary, mode = "", "fallback"
    try:
        from ducky.llm_client import call_llm
        out = call_llm(
            "下面是一次对话里记下的内容。用一到两句话说出这一程最值得记住的是什么。\n"
            "要求：具体，点出人和事；不要罗列，不要总结套话；不要超过 80 字。\n\n"
            + joined,
            system="你在为一个长期记忆系统提炼会话精华。只输出那一两句话本身。",
            max_tokens=200, temperature=0.3, timeout=30)
        if out and out.strip():
            summary, mode = out.strip()[:400], "llm"
    except (ImportError, RuntimeError, OSError, ValueError, TypeError) as exc:
        # 降级是本函数契约的一部分（call_llm 本身失败返 None，这里兜的是
        # 导入不到 / 网络层抛出这类）。仍然收窄，不拿宽捕获当万能垫子。
        logger.info("会话精华 LLM 提炼不可用，走确定性降级: %s", type(exc).__name__)
    if not summary:
        summary = _fallback_summary(rows)
    if not summary:
        return {"status": "skipped", "reason": "empty_after_fallback",
                "session_id": session_id, "source_count": len(rows)}

    # Same committed sources must keep the same write generation even if the
    # LLM phrases its summary differently on a repeated session-end event.
    # Including refs as well as text lets the bounded latest-turn window move
    # forward after MAX_SOURCE rows without reusing an old idempotency key.
    source_fingerprint = hashlib.sha256(json.dumps(
        [(r["ref"], r["text"]) for r in rows], ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()

    # 情感成分给初始显著性一个**有界**加成：0 命中 0.60，命中越多越高，
    # 上限 0.85。不设 1.0 —— 精华已经走慢衰减泳道，再给满分等于永不遗忘，
    # 那是 preference 泳道的语义，不是这里的。
    initial = min(0.60 + 0.05 * emo, 0.85)

    meta = {
        "kind": "session_distill",
        "lane": "distill",
        "_origin_session_id": session_id,
        "_origin_agent": "session-distill",
        "_origin_turn": max((r["turn"] or 0) for r in rows),
        "distill_mode": mode,              # llm | fallback，不冒充
        "distill_source_count": len(rows),
        "distill_source_fingerprint": source_fingerprint,
        "distill_emotion_hits": emo,       # 词表在 salience/config.LANE_KEYWORDS
        "distill_initial_salience": round(initial, 4),
    }
    return {"status": "ok", "session_id": session_id, "summary": summary,
            "mode": mode, "source_count": len(rows), "emotion_hits": emo,
            "source_fingerprint": source_fingerprint,
            "initial_salience": round(initial, 4), "metadata": meta,
            "user_id": uid, "bank_id": bid}
