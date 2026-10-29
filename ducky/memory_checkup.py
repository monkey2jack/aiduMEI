"""ducky.memory_checkup — 记忆体检(f0.7 P4 · 心图日常视图)。

心图不常态渲染整张真实图谱(几百上千个点对人没用、还慢),日常只回答三个问题:
  1. 重复:同一段话存了几份(哈希相同,或去掉标点空白大小写后相同);
  2. 过时:已过有效期却还在库里的记忆;事实里已标记失效(valid_to 已过)却未归档的;
  3. 冲突:同一个主题键当前同时有几个不同的值(只看「主题型」事实,度量/日期类键天生多值,不算)。

纪律:只读(只开 ?mode=ro),不改任何数据;处置仍走 /delete、/update,删除照样进墓碑可恢复。
租户隔离与 /facts 同款:只看 (user_id, bank_id) 自己的行。
"""
from __future__ import annotations

import os
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List

# 键本身就是度量/日期/版本号的 pattern 事实:同键多值是常态(「30秒」可以出现在一百句话里),不是冲突
_MULTI_VALUE_CATEGORIES = ("pattern_metric", "pattern_datetime", "pattern_version", "pattern_link",
                           "pattern_errsig", "pattern_instruction")
_NORM = re.compile(r"[\W_]+", re.UNICODE)
# 宿主/钩子吐出的机器话(不是人说的话)。这类重复不该「留一份」,而是整组都可清理;写线过滤词另配
_MACHINE_NOISE = re.compile(r"^\s*(\[SILENT\]|\[Your previous response had no visible output|\[Image: original|"
                            r"Stop hook feedback:|\[Request interrupted by user)", re.IGNORECASE)


def _ro(path: str) -> "sqlite3.Connection | None":
    if not path or not os.path.isfile(path):
        return None
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def _norm(text: str) -> str:
    return _NORM.sub("", (text or "")).lower()


def _snip(text: str, n: int = 80) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def _has_table(c: sqlite3.Connection, name: str) -> bool:
    return c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _mem_scope(user_id: str, bank_id: str) -> tuple:
    """memories 表的租户片段:走 scope_sql 唯一入口(canonical = user_id + bank_id 精确匹配)。"""
    from ducky.bank_contract import make_scope
    from ducky.scope_sql import scope_clause
    return scope_clause(make_scope(user_id, bank_id))


def _fact_scope(f, user_id: str, bank_id: str) -> tuple:
    """facts 表的租户片段:与 /facts 族同款(scope_sql facts flavor,老库无 bank_id 列时按 v19 语义)。"""
    from ducky.bank_contract import make_scope
    from ducky.scope_sql import scope_clause
    return scope_clause(make_scope(user_id, bank_id), flavor="facts", conn=f)


def _duplicates(c, user_id: str, bank_id: str, limit: int) -> Dict[str, Any]:
    frag, args = _mem_scope(user_id, bank_id)
    rows = c.execute("SELECT id, content, memory_type, created_at FROM memories WHERE 1=1 " + frag +
                     " ORDER BY created_at", args).fetchall()
    groups: Dict[str, List[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        key = _norm(r["content"])
        if key:
            groups[key].append(r)
    dup = [g for g in groups.values() if len(g) > 1]
    dup.sort(key=lambda g: (-len(g), g[0]["created_at"] or ""))
    return {
        "groups": len(dup),
        "extra_copies": sum(len(g) - 1 for g in dup),
        "items": [{
            "sample": _snip(g[0]["content"]),
            "count": len(g),
            "memory_type": g[0]["memory_type"],
            "machine_noise": bool(_MACHINE_NOISE.match(g[0]["content"] or "")),   # True = 整组可清,不必留
            "keep_id": g[0]["id"],                       # 最早的一份;其余是可清理的副本
            "extra_ids": [r["id"] for r in g[1:]][:20],
        } for g in dup[:limit]],
    }


def _stale(c, f, user_id: str, bank_id: str, limit: int) -> Dict[str, Any]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    frag, args = _mem_scope(user_id, bank_id)
    expired = c.execute(
        "SELECT id, content, expires_at FROM memories WHERE expires_at IS NOT NULL AND expires_at<>'' "
        "AND expires_at<? " + frag + " ORDER BY expires_at", [today, *args]).fetchall()
    ended: list = []
    if f is not None and _has_table(f, "facts"):
        cols = {r[1] for r in f.execute("PRAGMA table_info(facts)")}
        has_vt, has_sup = "valid_to" in cols, "superseded_by" in cols
        if has_vt or has_sup:
            ffrag, fargs = _fact_scope(f, user_id, bank_id)
            if has_vt and has_sup:
                cond, cargs = ("((valid_to IS NOT NULL AND valid_to<>'' AND valid_to<?) "
                               "OR (superseded_by IS NOT NULL AND superseded_by<>''))"), [today]
            elif has_vt:
                cond, cargs = "(valid_to IS NOT NULL AND valid_to<>'' AND valid_to<?)", [today]
            else:
                cond, cargs = "(superseded_by IS NOT NULL AND superseded_by<>'')", []
            sel = ("SELECT id, category, fact_key, fact_value, valid_to FROM facts" if has_vt
                   else "SELECT id, category, fact_key, fact_value, NULL AS valid_to FROM facts")
            ended = f.execute(sel + " WHERE COALESCE(archived,0)=0 AND " + cond + " " + ffrag + " ORDER BY id",
                              [*cargs, *fargs]).fetchall()
    items = [{"kind": "memory_expired", "id": r["id"], "sample": _snip(r["content"]), "since": r["expires_at"]}
             for r in expired]
    items += [{"kind": "fact_ended", "id": r["id"], "sample": _snip(f"{r['fact_key']}: {r['fact_value']}"),
               "since": r["valid_to"] or "superseded"} for r in ended]
    return {"expired_memories": len(expired), "ended_facts": len(ended), "items": items[:limit]}


def _conflicts(f, user_id: str, bank_id: str, limit: int) -> Dict[str, Any]:
    if f is None or not _has_table(f, "facts"):
        return {"keys": 0, "items": []}
    cols = {r[1] for r in f.execute("PRAGMA table_info(facts)")}
    ffrag, fargs = _fact_scope(f, user_id, bank_id)
    sql = "SELECT id, category, fact_key, fact_value, created_at FROM facts WHERE COALESCE(archived,0)=0"
    if "superseded_by" in cols:
        sql += " AND (superseded_by IS NULL OR superseded_by='')"
    if "valid_to" in cols:
        sql += " AND (valid_to IS NULL OR valid_to='')"
    sql += " AND category NOT IN (" + ",".join("?" * len(_MULTI_VALUE_CATEGORIES)) + ") " + ffrag
    rows = f.execute(sql + " ORDER BY created_at", [*_MULTI_VALUE_CATEGORIES, *fargs]).fetchall()
    by_key: Dict[tuple, Dict[str, sqlite3.Row]] = defaultdict(dict)
    for r in rows:
        v = _norm(r["fact_value"])
        if v and v not in by_key[(r["category"], r["fact_key"])]:
            by_key[(r["category"], r["fact_key"])][v] = r
    hits = [(k, list(vs.values())) for k, vs in by_key.items() if len(vs) > 1]
    hits.sort(key=lambda kv: -len(kv[1]))
    return {
        "keys": len(hits),
        "items": [{
            "category": cat, "key": key,
            "values": [{"id": r["id"], "value": _snip(r["fact_value"], 60), "created_at": r["created_at"]}
                       for r in vals[:6]],
        } for (cat, key), vals in hits[:limit]],
    }


def checkup(user_id: str, bank_id: str = "default", limit: int = 20) -> Dict[str, Any]:
    """只读体检。memory.db 不存在(mem0 内核)时如实返回 available=False,不伪造零。"""
    from ducky.memory_kernel.impl_native import default_db_path
    from ducky.utils import FACTS_DB
    limit = max(1, min(int(limit or 20), 100))
    c = _ro(default_db_path())
    f = _ro(FACTS_DB)
    try:
        if c is None or not _has_table(c, "memories"):
            return {"available": False, "reason": "native_memory_db_missing",
                    "conflicts": _conflicts(f, user_id, bank_id, limit)}
        frag, args = _mem_scope(user_id, bank_id)
        total = c.execute("SELECT COUNT(*) FROM memories WHERE 1=1 " + frag, args).fetchone()[0]
        dups = _duplicates(c, user_id, bank_id, limit)
        stale = _stale(c, f, user_id, bank_id, limit)
        conf = _conflicts(f, user_id, bank_id, limit)
        problems = dups["extra_copies"] + stale["expired_memories"] + stale["ended_facts"] + conf["keys"]
        return {
            "available": True, "total_memories": total,
            "headline": ("✅ 没发现重复、过时或冲突" if not problems else
                         f"重复 {dups['groups']} 组（多 {dups['extra_copies']} 份）· "
                         f"过时 {stale['expired_memories'] + stale['ended_facts']} 条 · 冲突 {conf['keys']} 处"),
            "duplicates": dups, "stale": stale, "conflicts": conf,
        }
    finally:
        for x in (c, f):
            if x is not None:
                x.close()
