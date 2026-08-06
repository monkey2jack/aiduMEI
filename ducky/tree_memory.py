"""
ducky.tree_memory — 树状记忆架构 (v17.0 · 借鉴 Mímir 联邦记忆系统)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
提供层级化/树状节点记忆表达与检索 (Tree Memory Architecture)。
让分类与事实支持 parent_id、node_path 的下钻与向上追溯。

借鉴来源: Mímir v9.1 联邦记忆系统 + MemOS TreeMemory
  - 树形结构表达 aidu 矩阵的父子关系（aiduBOX -> 小猴 等层级）
  - 节点路径索引 (node_path) 支持前缀查询
  - 与 facts 表关联统计：每个节点挂载的事实数

v17.0 修复:
  - fact_count 统计改为精确匹配 category，去除 tags LIKE 模糊匹配避免误匹配
  - 根节点预设改为通用占位（脱敏），不含私有业务信息
"""
from __future__ import annotations

import logging
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

# 默认根节点（通用模板，api_server 启动时可调用 init_tree_memory_schema 传入自定义根）
_DEFAULT_ROOT_NODES: list[tuple[str, str, str]] = [
    ("projects", "/projects", "项目与产品总根"),
    ("user_profile", "/user_profile", "用户个人偏好与约定"),
    ("system", "/system", "系统与架构约束"),
]


def init_tree_memory_schema(
    custom_roots: Optional[list[tuple[str, str, str]]] = None,
) -> None:
    """
    初始化 memory_nodes 表结构与根节点。
    custom_roots: [(name, node_path, description), ...] 自定义根节点列表
    """
    conn = get_facts_conn()
    try:
        conn.executescript(_TREE_SCHEMA_DDL)
        roots = custom_roots if custom_roots is not None else _DEFAULT_ROOT_NODES
        for name, path, desc in roots:
            conn.execute(
                "INSERT OR IGNORE INTO memory_nodes (name, node_path, depth, description) VALUES (?, ?, 0, ?)",
                (name, path, desc),
            )
        conn.commit()
    except Exception as e:
        logger.error("🐙 [TreeMemory] DDL 初始化失败: %s", e)
    finally:
        conn.close()


def add_tree_node(name: str, parent_path: str = "/projects", description: str = "") -> dict[str, Any]:
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
        node_row = conn.execute(
            "SELECT node_id FROM memory_nodes WHERE node_path = ?", (node_path,)
        ).fetchone()
        return {"node_id": node_row[0], "node_path": node_path, "depth": depth, "name": name}
    except Exception as e:
        logger.error("🐙 [TreeMemory] add_tree_node 失败: %s", e)
        return {"error": str(e)}
    finally:
        conn.close()


def get_subtree(root_path: str = "/projects") -> list[dict[str, Any]]:
    """
    根据根节点路径，获取该子树下的所有节点与挂载的记忆数。
    fact_count 使用 category 精确匹配（v17 修复：去除 tags LIKE 模糊匹配）。
    """
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
            node_name = r[2]
            # 精确匹配 category，避免 tags LIKE 的误匹配与全表扫描
            fact_count = conn.execute(
                "SELECT COUNT(*) FROM facts WHERE category = ? AND (archived = 0 OR archived IS NULL)",
                (node_name,),
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


def get_ancestors(node_path: str) -> list[dict[str, Any]]:
    """
    向上追溯：返回某节点的所有祖先节点（从根到父）。
    用于"点击某个记忆，追溯它属于哪个项目/分支"。
    """
    init_tree_memory_schema()
    conn = get_facts_conn()
    ancestors = []
    try:
        parts = node_path.strip("/").split("/")
        for i in range(1, len(parts)):
            ancestor_path = "/" + "/".join(parts[:i])
            row = conn.execute(
                "SELECT node_id, parent_id, name, node_path, depth FROM memory_nodes WHERE node_path = ?",
                (ancestor_path,),
            ).fetchone()
            if row:
                ancestors.append({
                    "node_id": row[0],
                    "parent_id": row[1],
                    "name": row[2],
                    "node_path": row[3],
                    "depth": row[4],
                })
    except Exception as e:
        logger.error("🐙 [TreeMemory] get_ancestors 失败: %s", e)
    finally:
        conn.close()
    return ancestors
