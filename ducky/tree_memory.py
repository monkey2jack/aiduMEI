"""
ducky.tree_memory — 树状记忆架构 (v16.0 Opus Octopod · opus八爪鱼)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
提供层级化/树状节点记忆表达与检索 (Tree Memory Architecture)。
让分类与事实支持 parent_id、node_path (如 /aidu/aiduBOX/小猴) 的下钻与向上追溯。
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any, Optional

from ducky.utils import get_facts_conn

logger = logging.getLogger("aiduMEM.TreeMemory")

_TREE_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS memory_nodes (
    node_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id   INTEGER REFERENCES memory_nodes(node_id),
    name        TEXT NOT NULL,
    node_path   TEXT NOT NULL UNIQUE,
    depth       INTEGER DEFAULT 0,
    description TEXT DEFAULT '',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_nodes_parent ON memory_nodes(parent_id);
CREATE INDEX IF NOT EXISTS idx_nodes_path   ON memory_nodes(node_path);
"""


def init_tree_memory_schema() -> None:
    """初始化 memory_nodes 表结构与根节点"""
    conn = get_facts_conn()
    try:
        conn.executescript(_TREE_SCHEMA_DDL)
        # 确保基础根节点存在
        default_roots = [
            ("aidu 家族", "/aidu", "aidu 品牌与矩阵总根"),
            ("用户偏好", "/user_profile", "用户个人偏好与约定"),
            ("系统配置", "/system", "系统与架构约束"),
        ]
        for name, path, desc in default_roots:
            conn.execute(
                "INSERT OR IGNORE INTO memory_nodes (name, node_path, depth, description) VALUES (?, ?, 0, ?)",
                (name, path, desc),
            )
        conn.commit()
    except Exception as e:
        logger.error("🐙 [TreeMemory] DDL 初始化失败: %s", e)
    finally:
        conn.close()


def add_tree_node(name: str, parent_path: str = "/aidu", description: str = "") -> dict[str, Any]:
    """新增树状节点，自动计算 node_path 与 depth"""
    init_tree_memory_schema()
    conn = get_facts_conn()
    try:
        parent_path = "/" + parent_path.strip("/")
        parent_row = conn.execute(
            "SELECT node_id, depth FROM memory_nodes WHERE node_path = ?", (parent_path,)
        ).fetchone()

        parent_id = parent_row[0] if parent_row else None
        depth = (parent_row[1] + 1) if parent_row else 0
        node_path = f"{parent_path}/{name.strip('/')}"

        conn.execute(
            """
            INSERT INTO memory_nodes (parent_id, name, node_path, depth, description)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(node_path) DO UPDATE SET description = excluded.description
            """,
            (parent_id, name, node_path, depth, description),
        )
        conn.commit()
        node_row = conn.execute("SELECT node_id FROM memory_nodes WHERE node_path = ?", (node_path,)).fetchone()
        return {"node_id": node_row[0], "node_path": node_path, "depth": depth, "name": name}
    except Exception as e:
        logger.error("🐙 [TreeMemory] add_tree_node 失败: %s", e)
        return {"error": str(e)}
    finally:
        conn.close()


def get_subtree(root_path: str = "/aidu") -> list[dict[str, Any]]:
    """根据根节点路径，获取该子树下的所有节点与挂载的记忆数"""
    init_tree_memory_schema()
    conn = get_facts_conn()
    try:
        root_path = "/" + root_path.strip("/")
        rows = conn.execute(
            """
            SELECT node_id, parent_id, name, node_path, depth, description, created_at
            FROM memory_nodes
            WHERE node_path = ? OR node_path LIKE ?
            ORDER BY depth ASC, name ASC
            """,
            (root_path, f"{root_path}/%"),
        ).fetchall()

        nodes = []
        for r in rows:
            # 统计挂载到该类目的 facts 数量
            node_name = r[2]
            fact_count = conn.execute(
                "SELECT COUNT(*) FROM facts WHERE category = ? OR tags LIKE ?",
                (node_name, f"%{node_name}%"),
            ).fetchone()[0]

            nodes.append({
                "node_id": r[0],
                "parent_id": r[1],
                "name": r[2],
                "node_path": r[3],
                "depth": r[4],
                "description": r[5],
                "fact_count": fact_count,
            })
        return nodes
    except Exception as e:
        logger.error("🐙 [TreeMemory] get_subtree 失败: %s", e)
        return []
    finally:
        conn.close()
