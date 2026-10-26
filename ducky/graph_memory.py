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
import sqlite3
from typing import Any, Dict, List, Tuple

logger = logging.getLogger("aiduMEM.graph_memory")

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


def _conn() -> sqlite3.Connection:
    """沿用 facts 连接与隔离谓词(单库设计;WP8 迁移时随真源同移)。"""
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
