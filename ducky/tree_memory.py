"""
ducky.tree_memory — 树状记忆架构 (v17.0 · 借鉴 Mímir 联邦记忆系统)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
提供层级化/树状节点记忆表达与检索 (Tree Memory Architecture)。
让分类与事实支持 parent_id、node_path 的下钻与向上追溯。

借鉴来源: Mímir v9.1 联邦记忆系统 + MemOS TreeMemory
  - 树形结构表达 aidu 矩阵的父子关系（父节点 -> 子节点 等层级）
  - 节点路径索引 (node_path) 支持前缀查询
  - 与 facts 表关联统计：每个节点挂载的事实数

v17.0 修复:
  - fact_count 统计改为精确匹配 category，去除 tags LIKE 模糊匹配避免误匹配
  - 根节点预设改为通用占位（脱敏），不含私有业务信息

v20.4.0（三方审计 P1-6 · Codex P1-06）：补租户轴。
  此前 node_path 全局 UNIQUE、无 user/bank 维度 —— 不同租户的同名路径
  互相覆盖、统计串味，图谱层还会泄露别家的名称/路径/计数。现在逻辑
  唯一键是 (user_id, bank_id, node_path)；存量库整表重建，老行按
  default/default 认领（只认领不迁移，default 租户行为与升级前一致）；
  fact_count 统计同样按域收窄。
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Optional

from ducky.bank_contract import DEFAULT_BANK_ID, table_columns
from ducky.utils import DEFAULT_USER_ID, get_facts_conn

logger = logging.getLogger("aiduMEM.TreeMemory")

_TREE_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS memory_nodes (
    node_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id   INTEGER REFERENCES memory_nodes(node_id),
    name        TEXT NOT NULL,
    node_path   TEXT NOT NULL,
    user_id     TEXT NOT NULL DEFAULT 'default',
    bank_id     TEXT NOT NULL DEFAULT 'default',
    depth       INTEGER DEFAULT 0,
    description TEXT DEFAULT '',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_scope_path
    ON memory_nodes(user_id, bank_id, node_path);
CREATE INDEX IF NOT EXISTS idx_nodes_parent ON memory_nodes(parent_id);
CREATE INDEX IF NOT EXISTS idx_nodes_path   ON memory_nodes(node_path);
"""

# 默认根节点（通用模板，api_server 启动时可调用 init_tree_memory_schema 传入自定义根）
_DEFAULT_ROOT_NODES: list[tuple[str, str, str]] = [
    ("projects", "/projects", "项目与产品总根"),
    ("user_profile", "/user_profile", "用户个人偏好与约定"),
    ("system", "/system", "系统与架构约束"),
]

_migrate_lock = threading.Lock()
_migrated = False


def _norm(user_id: str, bank_id: str) -> tuple[str, str]:
    return (str(user_id or "").strip() or DEFAULT_USER_ID,
            str(bank_id or "").strip() or DEFAULT_BANK_ID)


def _migrate_legacy_table(conn) -> None:
    """存量表（node_path 列级 UNIQUE、无租户列）→ 新表形状。

    SQLite 改不掉列级约束，只能整表重建：建新表 → 搬行（老行盖
    default/default）→ 换名。memory_nodes 是小表（节点数量级），
    一次性重建成本可忽略；幂等 —— 已迁移的表直接短路。
    """
    global _migrated
    if _migrated:
        return
    with _migrate_lock:
        if _migrated:
            return
        cols = table_columns(conn, "memory_nodes")
        if not cols or "user_id" in cols:
            _migrated = True
            return
        logger.info("🐙 [TreeMemory] 存量表补租户轴：整表重建，老行归 default/default")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS memory_nodes_v2 (
                node_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_id   INTEGER REFERENCES memory_nodes_v2(node_id),
                name        TEXT NOT NULL,
                node_path   TEXT NOT NULL,
                user_id     TEXT NOT NULL DEFAULT 'default',
                bank_id     TEXT NOT NULL DEFAULT 'default',
                depth       INTEGER DEFAULT 0,
                description TEXT DEFAULT '',
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO memory_nodes_v2
                (node_id, parent_id, name, node_path, depth, description, created_at)
                SELECT node_id, parent_id, name, node_path, depth, description, created_at
                FROM memory_nodes;
            DROP TABLE memory_nodes;
            ALTER TABLE memory_nodes_v2 RENAME TO memory_nodes;
        """)
        conn.commit()
        _migrated = True


def init_tree_memory_schema(
    custom_roots: Optional[list[tuple[str, str, str]]] = None,
    *,
    user_id: str = DEFAULT_USER_ID,
    bank_id: str = DEFAULT_BANK_ID,
) -> None:
    """
    初始化 memory_nodes 表结构与根节点（根节点种在指定域内）。
    custom_roots: [(name, node_path, description), ...] 自定义根节点列表
    """
    uid, bid = _norm(user_id, bank_id)
    conn = get_facts_conn()
    try:
        _migrate_legacy_table(conn)
        conn.executescript(_TREE_SCHEMA_DDL)
        roots = custom_roots if custom_roots is not None else _DEFAULT_ROOT_NODES
        for name, path, desc in roots:
            conn.execute(
                "INSERT OR IGNORE INTO memory_nodes "
                "(name, node_path, user_id, bank_id, depth, description) "
                "VALUES (?, ?, ?, ?, 0, ?)",
                (name, path, uid, bid, desc),
            )
        conn.commit()
    except Exception as e:
        logger.error("🐙 [TreeMemory] DDL 初始化失败: %s", e)
    finally:
        conn.close()


def add_tree_node(name: str, parent_path: str = "/projects", description: str = "",
                  *, user_id: str = DEFAULT_USER_ID,
                  bank_id: str = DEFAULT_BANK_ID) -> dict[str, Any]:
    """新增树状节点，自动计算 node_path 与 depth（全程锁在调用方域内）"""
    uid, bid = _norm(user_id, bank_id)
    init_tree_memory_schema(user_id=uid, bank_id=bid)
    conn = get_facts_conn()
    try:
        parent_path = "/" + parent_path.strip("/")
        parent_row = conn.execute(
            "SELECT node_id, depth FROM memory_nodes "
            "WHERE node_path = ? AND user_id = ? AND bank_id = ?",
            (parent_path, uid, bid)
        ).fetchone()

        parent_id = parent_row[0] if parent_row else None
        depth = (parent_row[1] + 1) if parent_row else 0
        node_path = f"{parent_path}/{name.strip('/')}"

        conn.execute(
            """
            INSERT INTO memory_nodes (parent_id, name, node_path, user_id, bank_id, depth, description)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, bank_id, node_path) DO UPDATE SET description = excluded.description
            """,
            (parent_id, name, node_path, uid, bid, depth, description),
        )
        conn.commit()
        node_row = conn.execute(
            "SELECT node_id FROM memory_nodes "
            "WHERE node_path = ? AND user_id = ? AND bank_id = ?",
            (node_path, uid, bid)
        ).fetchone()
        return {"node_id": node_row[0], "node_path": node_path, "depth": depth, "name": name}
    except Exception as e:
        logger.error("🐙 [TreeMemory] add_tree_node 失败: %s", e)
        return {"error": str(e)}
    finally:
        conn.close()


def get_subtree(root_path: str = "/projects", *,
                user_id: str = DEFAULT_USER_ID,
                bank_id: str = DEFAULT_BANK_ID) -> list[dict[str, Any]]:
    """
    根据根节点路径，获取该子树下的所有节点与挂载的记忆数（仅本域）。
    fact_count 使用 category 精确匹配（v17 修复：去除 tags LIKE 模糊匹配），
    且统计同样按 (user_id, bank_id) 收窄 —— 图谱层不泄露别家的计数。
    """
    uid, bid = _norm(user_id, bank_id)
    init_tree_memory_schema(user_id=uid, bank_id=bid)
    conn = get_facts_conn()
    try:
        root_path = "/" + root_path.strip("/")
        rows = conn.execute(
            """
            SELECT node_id, parent_id, name, node_path, depth, description, created_at
            FROM memory_nodes
            WHERE (node_path = ? OR node_path LIKE ?) AND user_id = ? AND bank_id = ?
            ORDER BY depth ASC, name ASC
            """,
            (root_path, f"{root_path}/%", uid, bid),
        ).fetchall()

        _fact_cols = table_columns(conn, "facts")
        _facts_scoped = "user_id" in _fact_cols and "bank_id" in _fact_cols
        nodes = []
        for r in rows:
            node_name = r[2]
            # fact_count 单独容错：facts 表缺席/形状旧时计 0，不吞掉整棵子树
            # （v20.4.0 矩阵测试实测：原实现一处 no such table 让 get_subtree
            # 整体返回 []，节点明明在却看不见 —— 假红形态）。
            try:
                if _facts_scoped:
                    fact_count = conn.execute(
                        "SELECT COUNT(*) FROM facts WHERE category = ? "
                        "AND (archived = 0 OR archived IS NULL) AND user_id = ? AND bank_id = ?",
                        (node_name, uid, bid),
                    ).fetchone()[0]
                else:
                    fact_count = conn.execute(
                        "SELECT COUNT(*) FROM facts WHERE category = ? AND (archived = 0 OR archived IS NULL)",
                        (node_name,),
                    ).fetchone()[0]
            except Exception as ce:
                logger.debug("🐙 [TreeMemory] fact_count 统计跳过（计 0）: %s", ce)
                fact_count = 0

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


def get_ancestors(node_path: str, *,
                  user_id: str = DEFAULT_USER_ID,
                  bank_id: str = DEFAULT_BANK_ID) -> list[dict[str, Any]]:
    """
    向上追溯：返回某节点的所有祖先节点（从根到父，仅本域）。
    用于"点击某个记忆，追溯它属于哪个项目/分支"。
    """
    uid, bid = _norm(user_id, bank_id)
    init_tree_memory_schema(user_id=uid, bank_id=bid)
    conn = get_facts_conn()
    ancestors = []
    try:
        parts = node_path.strip("/").split("/")
        for i in range(1, len(parts)):
            ancestor_path = "/" + "/".join(parts[:i])
            row = conn.execute(
                "SELECT node_id, parent_id, name, node_path, depth FROM memory_nodes "
                "WHERE node_path = ? AND user_id = ? AND bank_id = ?",
                (ancestor_path, uid, bid),
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
