"""
ducky.code_graph — 代码结构图谱（Zeus-Alpha）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
吸收 code-review-graph 的 AST 爆炸半径理念。
用 Python 标准库 ast 解析 import/def/class 依赖关系。
不引入 Tree-sitter 重依赖，保持 aiduMEM 轻量。

功能：
  - parse_python_file: 解析单个 .py 文件的 imports/functions/classes
  - build_dependency_graph: 从目录构建依赖图
  - compute_blast_radius: 计算改动文件的"爆炸半径"
  - REST 端点: GET /code/graph, POST /code/impact
"""

from __future__ import annotations

import ast
import logging
import os
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger("aiduMEM.code_graph")


# ── 数据结构 ──

class FileNode:
    """一个源文件的结构信息"""
    __slots__ = ("path", "imports", "functions", "classes", "imported_by")

    def __init__(self, path: str):
        self.path = path
        self.imports: list[str] = []        # 该文件 import 了哪些模块
        self.functions: list[str] = []      # 该文件定义了哪些函数
        self.classes: list[str] = []        # 该文件定义了哪些类
        self.imported_by: list[str] = []    # 被哪些文件 import

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "imports": self.imports,
            "functions": self.functions,
            "classes": self.classes,
            "imported_by": self.imported_by,
        }


# ── 解析 ──

def parse_python_file(filepath: str) -> FileNode | None:
    """用标准库 ast 解析 Python 文件的结构"""
    node = FileNode(filepath)
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            source = f.read()
        tree = ast.parse(source, filename=filepath)
    except (SyntaxError, UnicodeDecodeError, OSError) as e:
        logger.debug(f"跳过无法解析的文件 {filepath}: {e}")
        return None

    for item in ast.walk(tree):
        if isinstance(item, ast.Import):
            for alias in item.names:
                node.imports.append(alias.name)
        elif isinstance(item, ast.ImportFrom):
            if item.module:
                node.imports.append(item.module)
        elif isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
            node.functions.append(item.name)
        elif isinstance(item, ast.ClassDef):
            node.classes.append(item.name)

    return node


def build_dependency_graph(root_dir: str, max_files: int = 500) -> dict[str, FileNode]:
    """扫描目录构建依赖图"""
    graph: dict[str, FileNode] = {}
    root = Path(root_dir).resolve()
    count = 0

    for py_file in root.rglob("*.py"):
        if count >= max_files:
            break
        # 跳过常见无意义目录
        parts = py_file.parts
        if any(skip in parts for skip in ("__pycache__", ".git", "node_modules", ".venv", "venv")):
            continue

        rel_path = str(py_file.relative_to(root))
        node = parse_python_file(str(py_file))
        if node:
            node.path = rel_path
            graph[rel_path] = node
            count += 1

    # 反向关联：构建 imported_by
    # 先建模块名 → 文件路径映射
    module_to_file: dict[str, str] = {}
    for rel_path in graph:
        # 把 foo/bar/baz.py → foo.bar.baz
        mod = rel_path.replace("/", ".").replace("\\", ".")
        if mod.endswith(".py"):
            mod = mod[:-3]
        if mod.endswith(".__init__"):
            mod = mod[:-9]
        module_to_file[mod] = rel_path

    for rel_path, node in graph.items():
        for imp in node.imports:
            # 尝试精确匹配和前缀匹配
            target = module_to_file.get(imp)
            if not target:
                # 尝试子模块匹配
                for mod_name, mod_path in module_to_file.items():
                    if mod_name.startswith(imp + ".") or imp.startswith(mod_name + "."):
                        target = mod_path
                        break
            if target and target != rel_path:
                graph[target].imported_by.append(rel_path)

    logger.info(f"📊 代码图谱构建完成: {count} 文件, {root_dir}")
    return graph


def compute_blast_radius(
    graph: dict[str, FileNode],
    changed_files: list[str],
    max_depth: int = 3,
) -> dict[str, Any]:
    """计算改动文件的爆炸半径（BFS 扩散）

    返回：受影响的文件列表 + 影响路径
    """
    affected: dict[str, int] = {}   # file → depth
    queue: list[tuple[str, int]] = [(f, 0) for f in changed_files if f in graph]
    visited: set[str] = set(changed_files)

    while queue:
        current, depth = queue.pop(0)
        if depth > max_depth:
            continue
        affected[current] = depth

        if current in graph:
            # 向上扩散：谁 import 了我
            for parent in graph[current].imported_by:
                if parent not in visited:
                    visited.add(parent)
                    queue.append((parent, depth + 1))

    return {
        "changed_files": changed_files,
        "blast_radius": len(affected),
        "max_depth_reached": max_depth,
        "affected_files": [
            {"path": path, "depth": depth}
            for path, depth in sorted(affected.items(), key=lambda x: x[1])
        ],
    }


# ── REST 端点 ──

class ImpactRequest(BaseModel):
    """爆炸半径查询请求"""
    root_dir: str = ""
    changed_files: list[str] = Field(default_factory=list)
    max_depth: int = 3
    max_files: int = 500


def register_code_graph_routes(app: FastAPI) -> None:
    """注册代码图谱端点"""

    @app.post("/code/impact")
    def code_impact(req: ImpactRequest):
        """计算改动文件的爆炸半径"""
        t0 = time.time()

        if not req.changed_files:
            raise HTTPException(400, "changed_files 不能为空")

        root_dir = req.root_dir or os.getcwd()
        if not os.path.isdir(root_dir):
            raise HTTPException(400, f"目录不存在: {root_dir}")

        graph = build_dependency_graph(root_dir, max_files=req.max_files)
        result = compute_blast_radius(graph, req.changed_files, max_depth=req.max_depth)

        elapsed_ms = round((time.time() - t0) * 1000, 1)
        return {
            "status": "ok",
            "timing_ms": elapsed_ms,
            "total_files_scanned": len(graph),
            **result,
        }

    @app.get("/code/graph")
    def code_graph_stats(root_dir: str = ""):
        """代码图谱统计"""
        t0 = time.time()

        root_dir = root_dir or os.getcwd()
        if not os.path.isdir(root_dir):
            raise HTTPException(400, f"目录不存在: {root_dir}")

        graph = build_dependency_graph(root_dir, max_files=500)

        # 找出最"危险"的文件（被引用最多的）
        hotspots = sorted(
            [(path, len(n.imported_by)) for path, n in graph.items() if n.imported_by],
            key=lambda x: -x[1]
        )[:10]

        elapsed_ms = round((time.time() - t0) * 1000, 1)
        return {
            "status": "ok",
            "timing_ms": elapsed_ms,
            "total_files": len(graph),
            "total_functions": sum(len(n.functions) for n in graph.values()),
            "total_classes": sum(len(n.classes) for n in graph.values()),
            "total_imports": sum(len(n.imports) for n in graph.values()),
            "hotspots": [{"path": p, "imported_by_count": c} for p, c in hotspots],
        }
