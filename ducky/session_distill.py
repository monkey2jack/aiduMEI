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


def collect_session_memories(session_id: str, *, user_id: str = "",
                             bank_id: str = "") -> list[dict[str, Any]]:
    """取这个会话写进来的记忆原文（按 sidecar 的 origin_session_id）。

    只认 `origin_session_id` —— 那是写线透传进来的、唯一能把「这一程」圈出来
    的键。读不到就返回空，不去猜时间窗（按时间圈会把并发的别的会话卷进来）。
    """
    if not session_id:
        return []
    uid = user_id or DEFAULT_USER_ID
    bid = bank_id or DEFAULT_BANK_ID
    rows: list[dict[str, Any]] = []
    conn = get_facts_conn()
    try:
        # 租户/库作用域走单一真源 scope_clause()，不手拼 —— 本仓有一条棘轮
        # 盯着这件事：手拼的 where 片段迟早会漏掉一个轴，而漏掉的表现是
        # 「查到了别人的数据」，不会报错。
        from ducky.bank_contract import make_scope
        from ducky.scope_sql import scope_clause
        _scope, _params = scope_clause(make_scope(uid, bid), flavor="canonical")
        cur = conn.execute(
            "SELECT memory_ref, origin_turn, created_at FROM memory_epistemic "
            f"WHERE origin_session_id = ?{_scope} "
            "ORDER BY origin_turn ASC, created_at ASC LIMIT ?",
            (session_id, *_params, MAX_SOURCE))
        refs = [(r[0], r[1], r[2]) for r in cur.fetchall()]
    except sqlite3.Error as exc:
        logger.warning("会话记忆取用失败 session=%s: %s", session_id[:32], exc)
        return []
    finally:
        conn.close()
    if not refs:
        return []

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
    return rows


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
        "distill_emotion_hits": emo,       # 词表在 salience/config.LANE_KEYWORDS
        "distill_initial_salience": round(initial, 4),
    }
    return {"status": "ok", "session_id": session_id, "summary": summary,
            "mode": mode, "source_count": len(rows), "emotion_hits": emo,
            "initial_salience": round(initial, 4), "metadata": meta,
            "user_id": uid, "bank_id": bid}
