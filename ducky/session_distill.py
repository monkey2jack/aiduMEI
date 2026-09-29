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

    # f0.3 (C1 / S-1): the summary names its sources, so deleting any one of
    # them can cascade to this derived record (right to delete).  Bounded to
    # the /add metadata value budget; truncation is flagged, never silent.
    source_refs, refs_truncated = _bounded_refs([r["ref"] for r in rows])
    meta = {
        "kind": "session_distill",
        "lane": "distill",
        "_origin_session_id": session_id,
        "_origin_agent": "session-distill",
        "_origin_turn": max((r["turn"] or 0) for r in rows),
        "distill_mode": mode,              # llm | fallback，不冒充
        "distill_source_count": len(rows),
        "distill_source_fingerprint": source_fingerprint,
        "distill_source_refs": source_refs,
        "distill_emotion_hits": emo,       # 词表在 salience/config.LANE_KEYWORDS
        "distill_initial_salience": round(initial, 4),
    }
    if refs_truncated:
        meta["distill_source_refs_truncated"] = True
    return {"status": "ok", "session_id": session_id, "summary": summary,
            "mode": mode, "source_count": len(rows), "emotion_hits": emo,
            "source_fingerprint": source_fingerprint,
            "source_refs": source_refs,
            "initial_salience": round(initial, 4), "metadata": meta,
            "user_id": uid, "bank_id": bid}


# -- f0.3 (C1): derived-summary provenance -----------------------------------
#
# A session summary is a *derived* record: its text is built from the
# session's committed turns (fallback mode copies up to 120 characters of the
# two longest ones verbatim).  Deleting a source used to leave the summary
# behind because nothing linked the two.  Two links now exist:
#   1. metadata["distill_source_refs"] travels with the summary into the
#      vector payload (cloud/auto gear);
#   2. the `distill_sources` ledger (facts.db) is written by /add in every
#      engine mode, including local/lite where no vector payload exists.
# On deletion, wal_engine asks :func:`find_derived_summaries` which summaries
# included the deleted item and deletes them through the normal cascade.

_REFS_VALUE_BUDGET = 3800        # < api_models._METADATA_MAX_VALUE_CHARS (4096)
_REF_MAX_CHARS = 256
_LEGACY_MIN_PREFIX = 10          # fallback-summary containment probe length

_DISTILL_SOURCES_DDL = (
    "CREATE TABLE IF NOT EXISTS distill_sources ("
    " user_id TEXT NOT NULL,"
    " bank_id TEXT NOT NULL DEFAULT 'default',"
    " session_id TEXT NOT NULL DEFAULT '',"
    " summary_hash TEXT NOT NULL,"
    " source_ref TEXT NOT NULL,"
    " created_at TEXT,"
    " PRIMARY KEY (user_id, bank_id, summary_hash, source_ref))"
)
_DISTILL_SOURCES_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_distill_sources_ref "
    "ON distill_sources(user_id, bank_id, source_ref)"
)


def _bounded_refs(refs: list) -> tuple[list[str], bool]:
    out: list[str] = []
    used = 2
    for ref in refs:
        text = str(ref or "").strip()[:_REF_MAX_CHARS]
        if not text or text in out:
            continue
        cost = len(repr(text)) + 2
        if used + cost > _REFS_VALUE_BUDGET:
            return out, True
        out.append(text)
        used += cost
    return out, False


def _normalize_refs(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [v for v in value.split(",")]
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for ref in value[:512]:
        text = str(ref or "").strip()[:_REF_MAX_CHARS]
        if text and text not in out:
            out.append(text)
    return out


def summary_text_hash(text: str) -> str:
    """Same fingerprint as verbatim_turns.content_hash (sha256 of stripped text)."""
    return hashlib.sha256((text or "").strip().encode("utf-8", errors="ignore")).hexdigest()


def _canonical(user_id: str, bank_id: str) -> tuple:
    """(scope, fragment, params) through the one scope entry (scope_sql)."""
    from ducky.bank_contract import make_scope
    from ducky.scope_sql import scope_clause
    scope = make_scope(user_id, bank_id)
    frag, params = scope_clause(scope, flavor="canonical")
    return scope, frag, list(params)


def ensure_distill_sources_schema(conn) -> None:
    conn.execute(_DISTILL_SOURCES_DDL)
    conn.execute(_DISTILL_SOURCES_INDEX)


def _rollback_own_write(conn, was_clean: bool) -> None:
    """Undo a failed ledger write on the shared thread connection.

    A failed statement leaves the connection inside a transaction (holding the
    write lock if anything was written), and the next unrelated commit on this
    thread would persist the half-done write.  Roll back only a transaction
    this function opened: the caller's pending writes on the same connection
    are not ours to discard.
    """
    if not was_clean or not conn.in_transaction:
        return
    try:
        conn.rollback()
    except sqlite3.Error as exc:
        logger.debug("distill_sources rollback skipped: %s", exc)


def record_summary_sources(user_id: str, bank_id: str, metadata: dict | None,
                           summary_text: str) -> int:
    """/add hook: remember which sources a session summary was built from.

    No-op (0) for anything that is not a session summary.  Never raises: a
    failed ledger write is logged loudly and the vector payload still carries
    the refs.
    """
    from ducky.origin_context import is_session_summary
    md = metadata if isinstance(metadata, dict) else {}
    if not is_session_summary(md):
        return 0
    refs = _normalize_refs(md.get("distill_source_refs"))
    text = (summary_text or "").strip()
    if not refs or not text:
        return 0
    conn, was_clean = None, False
    try:
        from ducky.bank_contract import make_scope
        from datetime import datetime, timezone
        scope = make_scope(user_id, bank_id)
        digest = summary_text_hash(text)
        now = datetime.now(timezone.utc).isoformat()
        sid = str(md.get("_origin_session_id") or "")[:_REF_MAX_CHARS]
        conn = get_facts_conn()
        was_clean = not conn.in_transaction
        ensure_distill_sources_schema(conn)
        conn.executemany(
            "INSERT OR IGNORE INTO distill_sources "
            "(user_id, bank_id, session_id, summary_hash, source_ref, created_at) "
            "VALUES (?,?,?,?,?,?)",
            [(scope.user_id, scope.bank_id, sid, digest, ref, now) for ref in refs])
        conn.commit()
        return len(refs)
    except (sqlite3.Error, ValueError, TypeError) as exc:
        if conn is not None:
            _rollback_own_write(conn, was_clean)
        logger.warning("session summary source ledger write failed "
                       "(vector payload still carries the refs): %s", exc)
        return 0


def delete_scope_sources(user_id: str, bank_id: str) -> int:
    """delete_all leg for the ledger (exact (user_id, bank_id))."""
    conn = get_facts_conn()
    was_clean = not conn.in_transaction
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='distill_sources'").fetchone():
            return 0
        _, frag, params = _canonical(user_id, bank_id)
        cur = conn.execute("DELETE FROM distill_sources WHERE 1=1" + frag, params)
        conn.commit()
        return int(cur.rowcount or 0)
    except sqlite3.Error:
        _rollback_own_write(conn, was_clean)
        raise
    finally:
        conn.close()


def forget_summary_sources(user_id: str, bank_id: str, summary_hashes) -> int:
    """Drop ledger rows of summaries that were just deleted."""
    hashes = [h for h in (summary_hashes or []) if h]
    if not hashes:
        return 0
    conn = get_facts_conn()
    was_clean = not conn.in_transaction
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='distill_sources'").fetchone():
            return 0
        _, frag, params = _canonical(user_id, bank_id)
        cur = conn.executemany(
            "DELETE FROM distill_sources WHERE summary_hash=?" + frag,
            [(h, *params) for h in hashes])
        conn.commit()
        return int(cur.rowcount or 0)
    except sqlite3.Error:
        _rollback_own_write(conn, was_clean)
        raise
    finally:
        conn.close()


def source_keys_before_delete(memory_id: str, user_id: str, bank_id: str,
                              content: str = "") -> dict:
    """Keys a summary may use to name the item about to be deleted.

    Must run *before* the physical delete: afterwards the verbatim rows that
    share the item's content (deleted by content hash) cannot be resolved.
    Returns {"refs": set, "sessions": set, "content": str}.
    """
    _, frag, sparams = _canonical(user_id, bank_id)
    mid = str(memory_id or "").strip()
    refs = {mid} if mid else set()
    sessions: set = set()
    text = (content or "").strip()
    conn = get_facts_conn()
    try:
        vid = mid.split(":", 1)[1].strip() if mid.lower().startswith("verbatim:") else ""
        if vid.isdigit():
            refs.add(f"verbatim:{int(vid)}")
            row = conn.execute(
                "SELECT content, session_id FROM verbatim_turns WHERE id=?" + frag,
                (int(vid), *sparams)).fetchone()
            if row:
                text = text or str(row[0] or "").strip()
                sessions.add(str(row[1] or ""))
        if text:
            for row in conn.execute(
                    "SELECT id, session_id FROM verbatim_turns WHERE content_hash=?" + frag,
                    (summary_text_hash(text), *sparams)).fetchall():
                refs.add(f"verbatim:{row[0]}")
                sessions.add(str(row[1] or ""))
        try:
            for row in conn.execute(
                    "SELECT origin_session_id FROM memory_epistemic WHERE memory_ref=?" + frag,
                    (mid, *sparams)).fetchall():
                sessions.add(str(row[0] or ""))
        except sqlite3.Error as exc:
            logger.debug("sidecar session lookup skipped: %s", exc)
    except sqlite3.Error as exc:
        logger.debug("derived-summary key lookup degraded: %s", exc)
    finally:
        conn.close()
    sessions.discard("")
    return {"refs": refs, "sessions": sessions, "content": text}


def _ledger_summaries(scope, refs: set) -> dict[str, str]:
    """summary_hash -> session_id for ledger rows naming any of ``refs``."""
    if not refs:
        return {}
    conn = get_facts_conn()
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                            "AND name='distill_sources'").fetchone():
            return {}
        out: dict[str, str] = {}
        _, frag, params = _canonical(scope.user_id, scope.bank_id)
        for ref in sorted(refs):          # a handful of refs; indexed lookups
            for row in conn.execute(
                    "SELECT summary_hash, session_id FROM distill_sources "
                    "WHERE source_ref=?" + frag, (ref, *params)).fetchall():
                out[str(row[0])] = str(row[1] or "")
        return out
    finally:
        conn.close()


def _item_metadata(item: dict) -> dict:
    md = item.get("metadata") if isinstance(item, dict) else None
    return md if isinstance(md, dict) else {}


def _legacy_fallback_contains(summary: str, content: str) -> bool:
    probe = (content or "").strip().replace("\n", " ")[:120]
    return len(probe) >= _LEGACY_MIN_PREFIX and probe in (summary or "")


def _classify_vector_summaries(items: list, keys: dict, ledger: dict) -> dict:
    """Split the scope's summary points into derived / unverified."""
    from ducky.origin_context import is_session_summary
    refs = keys.get("refs") or set()
    derived: dict[str, str] = {}
    unverified: list[str] = []
    for item in items or []:
        md = _item_metadata(item)
        mid = str(item.get("id") or item.get("memory_id") or "").strip()
        if not mid or not is_session_summary(md):
            continue
        text = str(item.get("memory") or item.get("data") or "")
        digest = summary_text_hash(text)
        item_refs = set(_normalize_refs(md.get("distill_source_refs")))
        if (item_refs & refs) or digest in ledger:
            derived[mid] = digest
        elif item_refs:
            continue            # names its sources, and the deleted item is not one
        elif (md.get("distill_mode") == "fallback"
              and _legacy_fallback_contains(text, keys.get("content", ""))):
            derived[mid] = digest   # legacy fallback summary quoting the content
        elif str(md.get("_origin_session_id") or "") in (keys.get("sessions") or set()):
            unverified.append(mid)  # legacy LLM summary of the same session
    return {"derived": derived, "unverified": unverified}


def summary_verbatim_ids(scope, hashes) -> list[int]:
    """verbatim_turns ids holding a summary's text (its raw-layer replica)."""
    hashes = sorted({h for h in (hashes or []) if h})
    if not hashes:
        return []
    conn = get_facts_conn()
    try:
        ids: set = set()
        _, frag, params = _canonical(scope.user_id, scope.bank_id)
        for digest in hashes:
            ids.update(int(r[0]) for r in conn.execute(
                "SELECT id FROM verbatim_turns WHERE content_hash=?" + frag,
                (digest, *params)).fetchall())
        return sorted(ids)
    except sqlite3.Error as exc:
        logger.debug("summary verbatim lookup skipped: %s", exc)
        return []
    finally:
        conn.close()


def _pending_summary_ids(scope, keys: dict, hashes) -> list[int]:
    """Deferred (lite gear) summary writes that would resurrect deleted content."""
    from ducky.origin_context import is_session_summary
    refs = keys.get("refs") or set()
    wanted = set(hashes or [])
    out: list[int] = []
    conn = get_facts_conn()
    try:
        _, frag, params = _canonical(scope.user_id, scope.bank_id)
        rows = conn.execute(
            "SELECT pending_id, payload FROM pending_embeddings "
            "WHERE side='cloud' AND replayed_at IS NULL" + frag, params).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    for pid, payload in rows:
        try:
            data = json.loads(payload or "{}")
        except (TypeError, ValueError):
            continue
        md = data.get("metadata") if isinstance(data, dict) else None
        if not isinstance(md, dict) or not is_session_summary(md):
            continue
        msgs = data.get("messages")
        text = msgs if isinstance(msgs, str) else ""
        if (set(_normalize_refs(md.get("distill_source_refs"))) & refs) or (
                text and summary_text_hash(text) in wanted):
            out.append(int(pid))
    return out


def find_derived_summaries(mem: Any, scope, keys: dict,
                           items: list | None = None) -> dict:
    """Which session summaries in ``scope`` include the deleted item?

    ``items`` is the scope's vector enumeration (mem0 get_all shape); when it
    is None and ``mem`` is available the scope is enumerated here.
    Returns {"vector_ids": [...], "verbatim_ids": [...], "pending_ids": [...],
             "summary_hashes": [...], "unverified": [...],
             "vector_enumeration": bool}.  ``verbatim_ids`` is the pre-delete
    view of the summaries' raw replicas (callers re-query after deleting the
    vector points, which already take the replica with them).
    """
    ledger = _ledger_summaries(scope, keys.get("refs") or set())
    enumerated = items is not None
    if items is None and mem is not None:
        from ducky.wal_engine import _scoped_vector_items
        items, enumerated = _scoped_vector_items(mem, scope)
    split = _classify_vector_summaries(items or [], keys, ledger)
    hashes = set(ledger) | set(split["derived"].values())
    return {
        "vector_ids": sorted(split["derived"]),
        "verbatim_ids": summary_verbatim_ids(scope, hashes),
        "pending_ids": _pending_summary_ids(scope, keys, hashes),
        "summary_hashes": sorted(hashes),
        "unverified": sorted(split["unverified"]),
        "vector_enumeration": bool(enumerated),
    }
