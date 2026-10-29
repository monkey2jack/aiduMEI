"""ducky.graph_memory — 记忆图谱(f0.6++ WP5 · 三类真实关系边)。

边不从「猜测」来,只从**已经发生的事**派生:
  1. update_chain : facts 行的 valid_from/valid_to 前后相接 → 谁取代了谁
  2. same_session : 两行 verbatim/facts 共享 origin_session_id → 同源
  3. co_occurrence: 两行 facts 在同一段原文中出现 → 实体共现

设计纪律:
  - **不预建全图**:表在写入时增量落边(与 facts 同事务出不了就异步补写),
    查询按 node 展开 depth≤3;
  - **不引新依赖**:纯 SQLite(可被 FTS 索引),不用 networkx/neo4j;
  - **租户隔离与 facts 同款谓词**——图谱不比主库更宽。
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
from typing import Any, Dict, List, Tuple

logger = logging.getLogger("aiduMEM.graph_memory")

# ── 确定性实体抽取器(co_occurrence 边的依据;自 hot/legacy_helpers 迁入,逻辑逐字不变) ──
_RE_QUOTED      = re.compile(r'["\\\']([^"\\\']+)["\\\']')
_RE_AKA         = re.compile(r'(\w+(?:\s+\w+)*)\s+(?:aka|also known as)\s+(\w+(?:\s+\w+)*)', re.IGNORECASE)
_RE_USER        = re.compile(r'\b(User|用户)\b')
_RE_AI          = re.compile(r'\b(AI|Assistant|助手)\b')
# 品牌名跨了三代（duMem → aiduMEM → aiduMEI），而这条正则是拿去匹配**存量记忆文本**的。
# IGNORECASE 帮不上忙：aiduMEM 与 aiduMEI 差在最后一个字母。只留新名 = 老记忆里的项目实体
# 全部认不出来；只留旧名 = 改名之后写进来的新记忆全部认不出来。匹配器必须同时覆盖两代。
_RE_PROJECT     = re.compile(r'\b(aiduMEI|aiduMEM|duMem|mem0|Hermes|Qdrant|FastAPI|SQLite)\b', re.IGNORECASE)
_RE_TECH        = re.compile(r'\b(Hermes|DeepSeek|Qdrant|NVIDIA|FTS5|WAL|VACUUM|nginx|systemd|FastAPI)\b')


def _env_pattern(env_key: str) -> "re.Pattern | None":
    """从环境变量读取 `|` 分隔的自定义词表，编译成实体识别正则。

    部署方专属的机房代号、纪念日、人名等不硬编码在源码里，例如：
        export AIDUMEM_SERVER_KEYWORDS="tokyo|osaka"
        export AIDUMEM_DATE_KEYWORDS="1月1日|周年"
    未设置时返回 None，该类实体不参与提取。
    """
    raw = (os.environ.get(env_key) or "").strip().strip("|")
    if not raw:
        return None
    words = [re.escape(w.strip()) for w in raw.split("|") if w.strip()]
    if not words:
        return None
    return re.compile("(" + "|".join(words) + ")")


# 可选的部署方自定义实体（服务器代号 / 纪念日期）
#
# 血训（v15）：这两个正则原本在 import 时就固化，一旦模块比 setenv 先加载
# （或 systemd 单元漏了 Environment=），自定义实体就永久不参与抽取，
# 且全程静默。改为惰性 + 环境变量变化自动重建。
_ENV_PATTERN_CACHE: dict = {}


def _env_pattern_cached(env_key: str) -> "re.Pattern | None":
    raw = (os.environ.get(env_key) or "").strip().strip("|")
    cached = _ENV_PATTERN_CACHE.get(env_key)
    if cached is None or cached[0] != raw:
        _ENV_PATTERN_CACHE[env_key] = (raw, _env_pattern(env_key))
    return _ENV_PATTERN_CACHE[env_key][1]

def _extract_entities(text: str) -> list[str]:
    """原生的实体提取 — 保持首次出现顺序去重"""
    seen, candidates = set(), []
    def _add(name):
        n = name.strip()
        if n and n.lower() not in seen: seen.add(n.lower()); candidates.append(n)
    for m in _RE_QUOTED.finditer(text):       _add(m.group(1))
    for m in _RE_AKA.finditer(text):          _add(m.group(1)); _add(m.group(2))
    _re_server = _env_pattern_cached("AIDUMEM_SERVER_KEYWORDS")
    _re_date = _env_pattern_cached("AIDUMEM_DATE_KEYWORDS")
    for pat in [_RE_USER, _RE_AI, _RE_PROJECT, _re_server, _RE_TECH, _re_date]:
        if pat is None: continue
        for m in pat.finditer(text):          _add(m.group(0))
    return candidates

_DEPTHS = (1, 2, 3)
_EDGE_TYPES = ("update_chain", "same_session", "co_occurrence")

_DDL = """
CREATE TABLE IF NOT EXISTS graph_edges (
  src_type TEXT NOT NULL,
  src_id TEXT NOT NULL,
  dst_type TEXT NOT NULL,
  dst_id TEXT NOT NULL,
  edge_type TEXT NOT NULL,
  user_id TEXT NOT NULL,
  bank_id TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (src_type, src_id, dst_type, dst_id, edge_type)
);
CREATE INDEX IF NOT EXISTS idx_ge_src ON graph_edges(user_id, bank_id, src_type, src_id);
CREATE INDEX IF NOT EXISTS idx_ge_dst ON graph_edges(user_id, bank_id, dst_type, dst_id);
"""


_NATIVE_CONN: Dict[str, sqlite3.Connection] = {}


def _conn() -> sqlite3.Connection:
    """边在哪个库:native 内核写边进 memory.db(单库真源),mem0 内核沿用 facts 连接。

    f0.7 P4 实测:生产 native 后 memory.db 有 1387 条边,而这里一直读 facts.db 的空表,
    控制台心图因此只会显示「图未派生」——读写两头必须指向同一个库。
    """
    from ducky.mem0_runtime import native_kernel_active
    if native_kernel_active():
        from ducky.memory_kernel.impl_native import default_db_path
        path = default_db_path()
        c = _NATIVE_CONN.get(path)
        if c is None:
            c = sqlite3.connect(path, timeout=10, check_same_thread=False)
            c.execute("PRAGMA busy_timeout=10000")
            _NATIVE_CONN[path] = c
        return c
    from ducky.core_memory import _get_conn as _cm_conn
    return _cm_conn()


def ensure_graph_schema() -> None:
    conn = _conn()
    conn.executescript(_DDL)
    conn.commit()


def add_edge(src_type: str, src_id: str, dst_type: str, dst_id: str,
             edge_type: str, user_id: str, bank_id: str = "default") -> bool:
    """写一条边;重复写入幂等跳过。类型不在三允许列表即拒(fail-fast)。"""
    if edge_type not in _EDGE_TYPES:
        raise ValueError(f"edge_type {edge_type!r} 不在允许集 {_EDGE_TYPES}")
    ensure_graph_schema()
    try:
        conn = _conn()
        conn.execute(
            "INSERT OR IGNORE INTO graph_edges "
            "(src_type, src_id, dst_type, dst_id, edge_type, user_id, bank_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (src_type, str(src_id), dst_type, str(dst_id), edge_type, user_id, bank_id))
        conn.commit()
        return True
    except sqlite3.Error as exc:
        logger.warning("graph edge write failed: %s", exc)
        return False


def neighbors(user_id: str, node_type: str, node_id: str,
              bank_id: str = "default", depth: int = 1,
              limit: int = 50) -> Dict[str, Any]:
    """从 node 出发展开 depth 跳邻域(逐步执行;SQLite 递归 CTE 在此未必快)。

    返回 {"nodes": [{type,id}], "edges": [{src,dst,edge_type}], "depth": d,
          "truncated": bool}
    """
    if depth not in _DEPTHS:
        raise ValueError(f"depth 只支持 {_DEPTHS},收到 {depth}")
    ensure_graph_schema()
    conn = _conn()
    nodes: List[Tuple[str, str]] = [(node_type, node_id)]
    seen = {(node_type, node_id)}
    edges: List[Dict[str, str]] = []
    truncated = False
    for _ in range(depth):
        frontier = nodes[-limit:] if len(nodes) > limit else nodes
        nxt: List[Tuple[str, str]] = []
        for (nt, nid) in frontier:
            rows = conn.execute(
                """SELECT src_type, src_id, dst_type, dst_id, edge_type FROM graph_edges
                   WHERE user_id=? AND bank_id=?
                     AND ((src_type=? AND src_id=?) OR (dst_type=? AND dst_id=?))
                   LIMIT ?""",
                (user_id, bank_id, nt, nid, nt, nid, limit)).fetchall()
            for r in rows:
                st, si, dt, di, et = r
                edges.append({"src_type": st, "src_id": si,
                              "dst_type": dt, "dst_id": di, "edge_type": et})
                for pt, pi in ((st, si), (dt, di)):
                    if (pt, pi) not in seen:
                        seen.add((pt, pi))
                        nxt.append((pt, pi))
        if not nxt:
            break
        nodes.extend(nxt)
        if len(nodes) > limit * max(_DEPTHS):
            truncated = True
            break
    return {"nodes": [{"type": t, "id": i} for t, i in nodes],
            "edges": edges, "depth": depth, "truncated": truncated}


def derive_update_chain_edges(user_id: str, bank_id: str = "default") -> int:
    """从 facts 的 valid_from/valid_to 派生 update_chain 边(谁取代了谁)。

    以 (fact_key, user_id, bank_id) 为组;组内 valid_from 与前一条的
    valid_to 相同或紧接的视为更新链。
    """
    ensure_graph_schema()
    conn = _conn()
    # f0.6++ WP5 修订:UNIQUE(agent_id,user_id,bank_id,category,fact_key) 使
    # 同 key 多行在同租户下根本插不进去(更新=UPDATE 原行)。update_chain 的
    # 真实形态在 **agent_id 维度**:同 key 不同 agent 的先后写入才构成
    # 「谁取代了谁」。分组键改为 (user_id, bank_id, category, fact_key),
    # 组内按 agent_id 排序定先后。
    groups = conn.execute(
        """SELECT fact_key, category, user_id, bank_id,
                  GROUP_CONCAT(id) ids, COUNT(DISTINCT agent_id) agents
           FROM facts WHERE user_id=? AND bank_id=?
           GROUP BY fact_key, category, user_id, bank_id
           HAVING agents > 1 LIMIT 500""",
        (user_id, bank_id)).fetchall()
    total = 0
    for row in groups:
        # SELECT 列序:fact_key(0), category(1), user_id(2), bank_id(3),
        # GROUP_CONCAT(id)=ids(4) —— 原先按旧列序取 row[3] 取到的是 bank_id,
        # ids 恒空 → 零边。改为按名取,排位不再咬人。
        ids_raw = row["ids"] if not isinstance(row, tuple) else row[4]
        ids = [x for x in (ids_raw or "").split(",") if x]
        for prev_id, next_id in zip(ids, ids[1:]):
            conn.execute(
                "INSERT OR IGNORE INTO graph_edges "
                "(src_type, src_id, dst_type, dst_id, edge_type, user_id, bank_id) "
                "VALUES ('fact', ?, 'fact', ?, 'update_chain', ?, ?)",
                (prev_id, next_id, user_id, bank_id))
            total += 1
    conn.commit()
    return total


def derive_session_edges(user_id: str, bank_id: str = "default",
                         limit_sessions: int = 200) -> int:
    """从 verbatim_turns 按 session 派生 same_session 边(会话内互为同源)。"""
    ensure_graph_schema()
    conn = _conn()
    sessions = conn.execute(
        """SELECT session_id, COUNT(*) n FROM verbatim_turns
           WHERE user_id=? AND bank_id=? AND session_id<>''
           GROUP BY session_id ORDER BY MAX(id) DESC LIMIT ?""",
        (user_id, bank_id, limit_sessions)).fetchall()
    total = 0
    for (sid, n) in sessions:
        if n < 2:
            continue
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM verbatim_turns WHERE user_id=? AND bank_id=? AND session_id=? LIMIT 30",
            (user_id, bank_id, sid)).fetchall()]
        for prev_id, next_id in zip(ids, ids[1:]):
            conn.execute(
                "INSERT OR IGNORE INTO graph_edges "
                "(src_type, src_id, dst_type, dst_id, edge_type, user_id, bank_id) "
                "VALUES ('verbatim', ?, 'verbatim', ?, 'same_session', ?, ?)",
                (str(prev_id), str(next_id), user_id, bank_id))
            total += 1
    conn.commit()
    return total


def graph_stats(user_id: str, bank_id: str = "default") -> Dict[str, int]:
    ensure_graph_schema()
    conn = _conn()
    by_type: Dict[str, int] = {}
    total = 0
    for et, n in conn.execute(
            """SELECT edge_type, COUNT(*) FROM graph_edges
               WHERE user_id=? AND bank_id=? GROUP BY edge_type""",
            (user_id, bank_id)).fetchall():
        by_type[et] = n
        total += n
    return {"total_edges": total, "by_type": by_type}


def nebula(user_id: str, bank_id: str = "default", limit: int = 1500) -> Dict[str, Any]:
    """记忆星云(f0.7 P4):整张真实图谱一次给前端画,按需加载、只读。

    节点 = 有边的记忆(按度数取前 limit 个,万点也不卡),附类型与一句摘要;
    边 = 两端都在节点集里的真实关系边(三类,不含猜测)。
    """
    limit = max(50, min(int(limit or 1500), 3000))
    ensure_graph_schema()
    conn = _conn()
    from ducky.bank_contract import make_scope
    from ducky.scope_sql import scope_clause
    frag, args = scope_clause(make_scope(user_id, bank_id))
    rows = conn.execute(
        "SELECT src_type, src_id, dst_type, dst_id, edge_type FROM graph_edges WHERE 1=1 " + frag,
        args).fetchall()
    degree: Dict[Tuple[str, str], int] = {}
    for st, si, dt, di, _et in rows:
        for k in ((st, si), (dt, di)):
            degree[k] = degree.get(k, 0) + 1
    keep = sorted(degree, key=lambda k: -degree[k])[:limit]
    keep_set = set(keep)
    info: Dict[str, Tuple[str, str]] = {}
    has_mem = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memories'").fetchone() is not None
    if has_mem and keep:
        for mid, content, mtype in conn.execute(
                "SELECT id, content, memory_type FROM memories WHERE 1=1 " + frag, args):
            info[str(mid)] = (mtype or "", " ".join((content or "").split())[:60])
    nodes = []
    for nt, ni in keep:
        mtype, text = info.get(ni) or info.get("verbatim:" + ni) or ("", "")
        nodes.append({"id": nt + ":" + ni, "kind": nt, "memory_type": mtype or nt.upper(),
                      "degree": degree[(nt, ni)], "text": text})
    edges = [{"s": st + ":" + si, "t": dt + ":" + di, "type": et}
             for st, si, dt, di, et in rows if (st, si) in keep_set and (dt, di) in keep_set]
    return {"nodes": nodes, "edges": edges, "total_nodes": len(degree),
            "total_edges": len(rows), "truncated": len(degree) > len(keep)}
