#!/usr/bin/env python3
"""
aiduMEM MCP Server — v18.0.0 Zeus
==============================================
通过 stdio/SSE 模式暴露 aiduMEM 工具给宿主 Agent。

架构说明：
  MCP 工具统一通过 HTTP 调用本地 api_server（默认 127.0.0.1:8767），
  不再直接 import ducky 内部模块，实现完全解耦：
    - api_server 可独立重启/升级，MCP 无需重启
    - 减少 Qdrant 锁冲突风险（只有一个进程持有锁）
    - 工具接口与 REST API 保持一致，便于测试

后台自动记忆线程仍走宿主会话库直读（无需 HTTP），保持低延迟。

环境变量（均可选）：
    AIDUMEM_HOST_STATE_DB    宿主 Agent 的会话 SQLite 路径
    AIDUMEM_HOST_LAST_ID     增量游标文件路径
    AIDUMEM_API_BASE         api_server 地址（默认 http://127.0.0.1:8767）
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import argparse
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

# ── 路径 bootstrap（先于 ducky import）──
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ducky.utils import BASE_DIR, DATA_DIR, DEFAULT_USER_ID, LOG_DIR
from ducky.tool_envelope import success, error, format_response

# ── 常量 ──
STATE_DB = os.environ.get("AIDUMEM_HOST_STATE_DB", "")
LAST_ID_FILE = os.environ.get(
    "AIDUMEM_HOST_LAST_ID",
    os.path.join(DATA_DIR, "auto_memory_last_id.txt"),
)
API_BASE = os.environ.get("AIDUMEM_API_BASE", "http://127.0.0.1:8767").rstrip("/")

os.makedirs(LOG_DIR, exist_ok=True)

# ── 日志 ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "mcp_server.log")),
        logging.StreamHandler(sys.stderr),
    ],
)
logger = logging.getLogger("aiduMEM-mcp")


# ═══════════════════════════════════════════════════════
# HTTP 客户端辅助（轻量，无第三方依赖）
# ═══════════════════════════════════════════════════════

def _api_get(path: str, params: dict | None = None, timeout: int = 20) -> dict:
    """GET 请求 api_server。返回解析后的 JSON dict 或 error dict。"""
    url = f"{API_BASE}{path}"
    if params:
        qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
        url = f"{url}?{qs}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        return {"error": f"HTTP {e.code}", "detail": body}
    except Exception as e:
        return {"error": str(e)}


def _api_post(path: str, body: dict | None = None, timeout: int = 30) -> dict:
    """POST 请求 api_server。返回解析后的 JSON dict 或 error dict。"""
    url = f"{API_BASE}{path}"
    data = json.dumps(body or {}).encode()
    try:
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        return {"error": f"HTTP {e.code}", "detail": body_text}
    except Exception as e:
        return {"error": str(e)}


def _ok(data: Any) -> str:
    """把 dict 序列化为 MCP 工具返回字符串。"""
    return json.dumps(data, ensure_ascii=False, indent=2)


def _err(msg: str) -> str:
    return json.dumps({"error": msg}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════
# FastMCP 初始化
# ═══════════════════════════════════════════════════════

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("aidumem", log_level="INFO")


# ═══════════════════════════════════════════════════════
# ① 核心记忆 CRUD
# ═══════════════════════════════════════════════════════

@mcp.tool()
def mem_add(messages: str, user_id: str = DEFAULT_USER_ID) -> str:
    """添加记忆到 aiduMEM（自动提炼 + 向量化存储）。

    Args:
        messages: JSON 字符串，格式 [{"role": "user/assistant", "content": "..."}]
        user_id:  用户标识，默认取 AIDUMEM_DEFAULT_USER_ID 环境变量
    """
    try:
        msg_list = json.loads(messages)
    except json.JSONDecodeError as e:
        return _err(f"messages JSON 解析失败: {e}")
    result = _api_post("/add", {"messages": msg_list, "user_id": user_id})
    return _ok(result)


@mcp.tool()
def mem_add_raw(content: str, source: str = "mcp", user_id: str = DEFAULT_USER_ID) -> str:
    """原味抽屉（Raw Drawer）— 零 LLM 直存原始文本，不经过提炼压缩。

    适合存入代码片段、完整对话记录、原始日志等需要原文检索的内容。

    Args:
        content: 要存储的原始文本内容
        source:  来源标识（如 cursor_hook / claude_code / manual）
        user_id: 用户标识
    """
    result = _api_post("/add/raw", {"content": content, "source": source, "user_id": user_id})
    return _ok(result)


@mcp.tool()
def mem_search(query: str, user_id: str = DEFAULT_USER_ID, top_k: int = 5) -> str:
    """语义搜索记忆（内置相关性闸门 + 显著性 boost）。

    Args:
        query:   搜索关键词或自然语言问题
        user_id: 用户标识
        top_k:   返回结果数量，默认 5
    """
    result = _api_post("/search", {"query": query, "user_id": user_id, "top_k": top_k})
    return _ok(result)


@mcp.tool()
def mem_search_deep(query: str, user_id: str = DEFAULT_USER_ID, top_k: int = 10) -> str:
    """深度搜索 — 同时检索向量库 + FTS5 全文索引 + facts 结构化知识库，三路并行召回。

    比 mem_search 召回更全面，适合知识库查询和精确事实检索。

    Args:
        query:   搜索关键词
        user_id: 用户标识
        top_k:   每路返回结果数，默认 10
    """
    result = _api_post("/search/deep", {"query": query, "user_id": user_id, "top_k": top_k})
    return _ok(result)


@mcp.tool()
def mem_recent(user_id: str = DEFAULT_USER_ID, limit: int = 10) -> str:
    """获取最近添加的记忆列表。

    Args:
        user_id: 用户标识
        limit:   返回条数，默认 10
    """
    result = _api_get("/recent", {"user_id": user_id, "limit": limit})
    return _ok(result)


@mcp.tool()
def mem_update(memory_id: str, data: str) -> str:
    """更新指定 ID 的记忆内容。

    Args:
        memory_id: 记忆 UUID
        data:      新的记忆文本
    """
    result = _api_post("/update", {"memory_id": memory_id, "data": data})
    return _ok(result)


@mcp.tool()
def mem_delete(memory_id: str) -> str:
    """删除指定 ID 的记忆。

    Args:
        memory_id: 记忆 UUID
    """
    result = _api_post("/delete", {"memory_id": memory_id})
    return _ok(result)


@mcp.tool()
def mem_delete_all(user_id: str = DEFAULT_USER_ID) -> str:
    """⚠️ 危险：清空指定用户的全部记忆。操作不可逆，请谨慎使用。

    Args:
        user_id: 用户标识
    """
    result = _api_post("/delete_all", {"user_id": user_id})
    return _ok(result)


# ═══════════════════════════════════════════════════════
# ② 统计与健康
# ═══════════════════════════════════════════════════════

@mcp.tool()
def mem_stats(user_id: str = DEFAULT_USER_ID) -> str:
    """查看记忆统计信息（总数、用户分布、显著性统计等）。

    Args:
        user_id: 用户标识
    """
    result = _api_get("/stats", {"user_id": user_id})
    return _ok(result)


@mcp.tool()
def mem_health() -> str:
    """检查 aiduMEM 服务健康状态，包含所有子模块探针结果和版本信息。"""
    result = _api_get("/health")
    return _ok(result)


@mcp.tool()
def mem_usage() -> str:
    """查看 API 使用量统计（各模型调用次数、Token 消耗等）。"""
    result = _api_get("/usage")
    return _ok(result)


# ═══════════════════════════════════════════════════════
# ③ Facts 结构化知识库
# ═══════════════════════════════════════════════════════

@mcp.tool()
def facts_search(query: str, limit: int = 10) -> str:
    """搜索结构化知识事实库（facts）。

    Facts 是经过实体提取的高质量结构化知识，与向量记忆互补。

    Args:
        query: 搜索关键词
        limit: 返回条数，默认 10
    """
    result = _api_get("/facts/search", {"query": query, "limit": limit})
    return _ok(result)


@mcp.tool()
def facts_list(category: str = "", limit: int = 20) -> str:
    """列出结构化知识事实。

    Args:
        category: 筛选类别（如 preference / event / skill / identity）
        limit:    返回条数，默认 20
    """
    params: dict = {"limit": limit}
    if category:
        params["category"] = category
    result = _api_get("/facts", params)
    return _ok(result)


@mcp.tool()
def facts_add(content: str, category: str = "general", source: str = "mcp") -> str:
    """向结构化知识库添加一条 Fact。

    Args:
        content:  事实内容文本
        category: 分类（preference / event / skill / identity / general）
        source:   来源标识
    """
    result = _api_post("/facts/add", {"content": content, "category": category, "source": source})
    return _ok(result)


@mcp.tool()
def facts_entities(entity: str = "") -> str:
    """查询 facts 实体图谱。

    Args:
        entity: 实体名称（为空则返回所有实体列表）
    """
    if entity:
        result = _api_get("/facts/related", {"entity": entity})
    else:
        result = _api_get("/facts/entities/list")
    return _ok(result)


@mcp.tool()
def facts_preferences() -> str:
    """获取用户偏好 facts（category=preference 的结构化知识）。"""
    result = _api_get("/facts/preferences")
    return _ok(result)


# ═══════════════════════════════════════════════════════
# ④ 代码图谱（Zeus v18.0 新增）
# ═══════════════════════════════════════════════════════

@mcp.tool()
def code_impact(file_path: str) -> str:
    """分析修改指定文件的波及范围（爆炸半径）。

    在修改代码前调用，快速了解哪些其他文件依赖此文件，防止意外破坏。

    Args:
        file_path: 要分析的文件路径（相对或绝对路径）
    """
    result = _api_post("/code/impact", {"file_path": file_path})
    return _ok(result)


@mcp.tool()
def code_graph_view(path: str = "") -> str:
    """查看代码依赖图谱。

    Args:
        path: 筛选路径前缀（为空则返回全图摘要）
    """
    params: dict = {}
    if path:
        params["path"] = path
    result = _api_get("/code/graph", params or None)
    return _ok(result)


# ═══════════════════════════════════════════════════════
# ⑤ Session 会话持久化
# ═══════════════════════════════════════════════════════

@mcp.tool()
def session_start(session_id: str, metadata: str = "{}") -> str:
    """开始一个新会话，建立记忆锚点。

    Args:
        session_id: 会话唯一标识（如时间戳字符串）
        metadata:   JSON 字符串，附加元数据（如 {"source": "feishu"}）
    """
    try:
        meta = json.loads(metadata)
    except json.JSONDecodeError:
        meta = {}
    result = _api_post("/session/start", {"session_id": session_id, "metadata": meta})
    return _ok(result)


@mcp.tool()
def session_end(session_id: str) -> str:
    """结束会话，触发记忆沉淀和显著性更新。

    Args:
        session_id: 要结束的会话标识
    """
    result = _api_post("/session/end", {"session_id": session_id})
    return _ok(result)


@mcp.tool()
def session_list() -> str:
    """列出所有已记录的历史会话摘要。"""
    result = _api_get("/session/list")
    return _ok(result)


@mcp.tool()
def session_report(session_id: str) -> str:
    """获取指定会话的详细记忆报告。

    Args:
        session_id: 会话标识
    """
    result = _api_post("/session/report", {"session_id": session_id})
    return _ok(result)


# ═══════════════════════════════════════════════════════
# ⑥ 观察与反思（Observe & Reflect）
# ═══════════════════════════════════════════════════════

@mcp.tool()
def mem_observe(query: str = "", user_id: str = DEFAULT_USER_ID) -> str:
    """观察记忆全景 — 返回高层次记忆摘要和热点话题。

    Args:
        query:   可选筛选关键词
        user_id: 用户标识
    """
    params: dict = {"user_id": user_id}
    if query:
        params["query"] = query
    result = _api_get("/observe", params)
    return _ok(result)


@mcp.tool()
def mem_reflect(topic: str, user_id: str = DEFAULT_USER_ID) -> str:
    """对某个话题进行深度反思 — 联结相关记忆，生成洞察。

    Args:
        topic:   反思话题
        user_id: 用户标识
    """
    result = _api_post("/reflect", {"topic": topic, "user_id": user_id})
    return _ok(result)


# ═══════════════════════════════════════════════════════
# ⑦ 核心记忆块（Core Memory / Persona）
# ═══════════════════════════════════════════════════════

@mcp.tool()
def core_memory_list() -> str:
    """列出所有核心记忆块（高优先级、永不过期的结构化事实）。"""
    result = _api_get("/api/core-memory")
    return _ok(result)


@mcp.tool()
def core_memory_get(block_key: str) -> str:
    """获取指定核心记忆块。

    Args:
        block_key: 记忆块键名（如 user_profile / preferences / identity）
    """
    result = _api_get(f"/api/core-memory/{urllib.parse.quote(block_key)}")
    return _ok(result)


@mcp.tool()
def mem_persona() -> str:
    """获取 AI 自我人设定义（ai-self persona block）。"""
    result = _api_get("/persona/ai-self")
    return _ok(result)


# ── v19.0 人格记忆基座（Persona Memory Layer）MCP 工具 ──
# 与旧 /persona/ai-self 静态人设互补：新基座是按情境检索的自传体记忆库。

@mcp.tool()
def mem_persona_build(
    persona_card: str = "",
    persona_name: str = "",
    persona_key: str = "",
    mode: str = "synthesis",
    source_material: str = "",
    use_llm: bool = True,
) -> str:
    """构建人格记忆基座（v19.0 Persona Memory Layer）。

    两种模式：
      - synthesis 合成：从一句话人设生成虚构自传体记忆库（面向虚构角色）
      - grounded 真实：从 source_material 抽取组织记忆，零虚构，每条可回溯来源

    Args:
        persona_card:    合成模式用，简短人设
        persona_name:    人设名称
        persona_key:     稳定标识（省略则自动推导）
        mode:            synthesis | grounded
        source_material: 真实模式用，素材原文（多行）
        use_llm:         是否调用 LLM（False 时合成模式走规则降级）
    """
    body: dict = {
        "persona_card": persona_card,
        "persona_name": persona_name,
        "persona_key": persona_key,
        "mode": mode,
        "source_material": source_material,
        "use_llm": use_llm,
    }
    result = _api_post("/persona/build", body)
    return _ok(result)


@mcp.tool()
def mem_persona_retrieve(
    situation: str,
    persona_key: str = "",
    bank_id: int = 0,
    k: int = 5,
    level: str = "",
) -> str:
    """按当前情境检索人格记忆（动态条件化，替代整卡注入）。

    Args:
        situation:   当前情境描述（如「用户在做前端开发」）
        persona_key: 基座标识（省略则用 bank_id）
        bank_id:     指定基座版本 ID（与 persona_key 二选一）
        k:           返回条数
        level:       可选只取某层 L(人生阶段)/G(一般事件)/E(具体经历)
    """
    body: dict = {
        "situation": situation,
        "persona_key": persona_key,
        "bank_id": bank_id,
        "k": k,
        "level": level,
    }
    result = _api_post("/persona/retrieve", body)
    return _ok(result)


@mcp.tool()
def mem_persona_banks(persona_key: str = "", status: str = "") -> str:
    """列出人格记忆基座（含版本历史、L/G/E 计数，可回滚）。

    Args:
        persona_key: 可选，只列某个基座
        status:      可选，按状态过滤（ready | superseded | building | failed）
    """
    params: dict = {}
    if persona_key:
        params["persona_key"] = persona_key
    if status:
        params["status"] = status
    result = _api_get("/persona/banks", params)
    return _ok(result)


# ═══════════════════════════════════════════════════════
# ⑧ AutoDream 自动梦境（后台自演化）
# ═══════════════════════════════════════════════════════

@mcp.tool()
def autodream_status() -> str:
    """查看 AutoDream 自动记忆演化的当前状态。"""
    result = _api_get("/api/autodream/status")
    return _ok(result)


@mcp.tool()
def autodream_report() -> str:
    """获取最近一次 AutoDream 的执行报告（合并、蒸馏、归档摘要）。"""
    result = _api_get("/api/autodream/report")
    return _ok(result)


@mcp.tool()
def autodream_trigger() -> str:
    """手动触发一次 AutoDream 演化（正常由定时任务驱动，此工具用于调试）。"""
    result = _api_post("/api/autodream/trigger", {})
    return _ok(result)


# ═══════════════════════════════════════════════════════
# ⑨ Raw Drawer 统计
# ═══════════════════════════════════════════════════════

@mcp.tool()
def raw_stats() -> str:
    """查看原味抽屉（Raw Drawer）的存储统计。"""
    result = _api_get("/raw/stats")
    return _ok(result)


# ═══════════════════════════════════════════════════════
# ⑩ 知识树与场景
# ═══════════════════════════════════════════════════════

@mcp.tool()
def knowledge_tree() -> str:
    """查看记忆知识树结构（按话题层级组织的记忆全景）。"""
    result = _api_get("/knowledge/tree")
    return _ok(result)


@mcp.tool()
def mem_scene(query: str = "") -> str:
    """查看场景记忆簇（自动聚类的话题场景）。

    Args:
        query: 可选筛选关键词
    """
    params: dict = {}
    if query:
        params["query"] = query
    result = _api_get("/scene", params or None)
    return _ok(result)


# ═══════════════════════════════════════════════════════
# ⑪ 技能结晶（Skill Crystallizer — v16.0 Octopus）
# ═══════════════════════════════════════════════════════

@mcp.tool()
def crystals_list() -> str:
    """列出已结晶的技能（从高频解决方案中自动提炼的可复用模式）。"""
    result = _api_get("/crystals")
    return _ok(result)


@mcp.tool()
def crystals_detect() -> str:
    """手动触发技能结晶检测（分析近期记忆，识别可提炼为技能的高频模式）。"""
    result = _api_post("/crystals/detect", {})
    return _ok(result)


# ═══════════════════════════════════════════════════════
# ⑫ 冲突解决（Conflict Resolver — v16.0 Octopus）
# ═══════════════════════════════════════════════════════

@mcp.tool()
def conflict_resolve(topic: str = "", user_id: str = DEFAULT_USER_ID) -> str:
    """检测并解决记忆冲突（矛盾的记忆会影响召回质量）。

    Args:
        topic:   可选，聚焦在某个话题范围内检测冲突
        user_id: 用户标识
    """
    body: dict = {"user_id": user_id}
    if topic:
        body["topic"] = topic
    result = _api_post("/conflict/resolve", body)
    return _ok(result)


# ═══════════════════════════════════════════════════════
# 后台自动记忆线程（直读宿主 state.db，不走 HTTP）
# ═══════════════════════════════════════════════════════

def _read_last_id() -> int:
    if os.path.exists(LAST_ID_FILE):
        try:
            return int(Path(LAST_ID_FILE).read_text().strip())
        except (ValueError, OSError):
            return 0
    return 0


def _write_last_id(msg_id: int) -> None:
    os.makedirs(os.path.dirname(LAST_ID_FILE), exist_ok=True)
    Path(LAST_ID_FILE).write_text(str(msg_id))


def _fetch_new_messages(last_id: int, limit: int = 200) -> tuple[list, int]:
    """从宿主 state.db 增量读取新对话消息。"""
    if not STATE_DB:
        return [], last_id
    if not os.path.exists(STATE_DB):
        logger.warning(f"宿主会话库不存在: {STATE_DB}")
        return [], last_id
    try:
        conn = sqlite3.connect(STATE_DB)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT m.id, m.session_id, m.role, m.content, m.timestamp, s.source
            FROM messages m
            LEFT JOIN sessions s ON m.session_id = s.id
            WHERE m.id > ?
              AND m.role IN ('user', 'assistant')
              AND m.content IS NOT NULL
              AND m.content != ''
              AND length(m.content) > 10
            ORDER BY m.id ASC
            LIMIT ?
            """,
            (last_id, limit),
        )
        rows = cur.fetchall()
        conn.close()
        messages = [
            {
                "id": r["id"],
                "session_id": r["session_id"],
                "role": r["role"],
                "content": r["content"][:2000],
                "timestamp": r["timestamp"],
                "source": r["source"],
            }
            for r in rows
        ]
        max_id = max((m["id"] for m in messages), default=last_id)
        return messages, max_id
    except Exception as e:
        logger.error(f"读取 state.db 失败: {e}")
        return [], last_id


def _group_by_session(messages: list) -> dict:
    sessions: dict = {}
    for msg in messages:
        sid = msg["session_id"]
        sessions.setdefault(sid, []).append(msg)
    for sid in sessions:
        sessions[sid].sort(key=lambda x: x["id"])
    return sessions


def _format_conversation(messages: list) -> str:
    parts = []
    for msg in messages:
        label = "User" if msg["role"] == "user" else "Assistant"
        parts.append(f"{label}: {msg['content']}")
    return "\n\n".join(parts)


def run_auto_memory() -> None:
    """执行一次自动记忆提取（通过 HTTP 调用 /add 端点）。"""
    last_id = _read_last_id()
    messages, max_id = _fetch_new_messages(last_id)
    if not messages:
        return

    sessions = _group_by_session(messages)
    stored_total = 0
    for sid, msgs in sessions.items():
        if len(msgs) < 2:
            continue
        conversation = _format_conversation(msgs)
        msg_list = [{"role": "user", "content": conversation}]
        resp = _api_post("/add", {"messages": msg_list, "user_id": DEFAULT_USER_ID}, timeout=60)
        if "error" not in resp:
            n = len(resp.get("results", []))
            stored_total += n
            logger.info(f"[auto-memory] 会话 {sid[:8]}… → {n} 条记忆")
        else:
            logger.warning(f"[auto-memory] 会话 {sid[:8]}… 存入失败: {resp.get('error')}")

    _write_last_id(max_id)
    if stored_total:
        logger.info(f"[auto-memory] 本轮共存入 {stored_total} 条记忆（游标→{max_id}）")


def auto_memory_loop() -> None:
    while True:
        try:
            if STATE_DB:
                run_auto_memory()
        except Exception as e:
            logger.error(f"❌ 自动记忆异常: {e}", exc_info=True)
        time.sleep(21600)  # 每 6 小时一次


# ═══════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="aiduMEM MCP Server v18.0.0 Zeus")
    parser.add_argument("--sse", action="store_true", help="以 SSE HTTP 模式启动（默认 stdio）")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--host", type=str, default="127.0.0.1")
    args = parser.parse_args()

    # 启动后台自动记忆线程
    threading.Thread(target=auto_memory_loop, daemon=True, name="auto-memory").start()
    logger.info(f"🧠 aiduMEM MCP Server v18.0.0-zeus 启动（API_BASE={API_BASE}）")

    if args.sse:
        logger.info(f"🌐 SSE 模式，监听 {args.host}:{args.port}")
        import uvicorn
        app = mcp.sse_app(mount_path="/sse")
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    else:
        logger.info("📟 stdio 模式启动")
        mcp.run(transport="stdio")

# ═══════════════════════════════════════════════════════
# ⑬ EvolveMem 检索自进化（v18.1 新增）
# ═══════════════════════════════════════════════════════

@mcp.tool()
def evolve_feedback(memory_id: str, signal: str, query: str = "", correction_text: str = "") -> str:
    """提交对某条记忆的调用反馈，用于自动调整记忆权重。

    Args:
        memory_id: 记忆 UUID
        signal:    反馈信号 ('useful' | 'useless' | 'correction')
        query:     触发该记忆的搜索词（可选）
        correction_text: 若为 'correction'，填入修正后的内容（可选）
    """
    body = {"memory_id": memory_id, "signal": signal}
    if query:
        body["query"] = query
    if correction_text:
        body["correction_text"] = correction_text
    result = _api_post("/evolve/feedback", body)
    return _ok(result)

@mcp.tool()
def evolve_report() -> str:
    """获取 EvolveMem 检索自进化的近期统计报告（搜索命中率、动态权重调整）。"""
    result = _api_get("/evolve/report")
    return _ok(result)
