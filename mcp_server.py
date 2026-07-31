#!/usr/bin/env python3
"""
mem0 MCP Server — AI Thought Engine
==============================================
通过 stdio 模式直接暴露 mem0 工具给宿主 Agent。
内置后台自动记忆线程，定期从宿主会话库提取新记忆，不受模型影响。

宿主相关路径通过环境变量配置（均为可选，未配置时自动记忆线程静默跳过）：
    AIDUMEM_HOST_STATE_DB    宿主 Agent 的会话 SQLite 路径
    AIDUMEM_HOST_LAST_ID     增量游标文件路径
"""

import json, logging, os, sys, argparse, threading, time, sqlite3
from pathlib import Path

# ── ducky 模块 ──
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ducky.memory_gate import relevance_check
from ducky.memory_salience import on_memory_accessed, on_memory_added
from ducky.tool_envelope import success, error, format_response
from ducky.utils import BASE_DIR, DATA_DIR, DEFAULT_USER_ID, LOG_DIR

# ── 路径常量 ──
STATE_DB = os.environ.get("AIDUMEM_HOST_STATE_DB", "")
LAST_ID_FILE = os.environ.get(
    "AIDUMEM_HOST_LAST_ID", os.path.join(DATA_DIR, "auto_memory_last_id.txt")
)
MEM0_CONFIG = os.path.join(BASE_DIR, "mem0_config_local.json")

# ── Qdrant 锁清理 ──
def _cleanup_zombie_lock():
    """启动时清理僵死的 Qdrant 锁——杀掉其他 mcp_server 僵尸进程"""
    import subprocess
    current_pid = os.getpid()
    try:
        # 找所有 mcp_server.py 进程（排除自己）
        result = subprocess.run(
            ["pgrep", "-f", "mcp_server.py"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            for pid_str in result.stdout.strip().split():
                pid = int(pid_str.strip())
                if pid != current_pid:
                    os.kill(pid, 9)
                    print(f"[startup] 🔪 杀掉僵尸MCP进程 PID={pid}", file=sys.stderr)
    except (subprocess.TimeoutExpired, ProcessLookupError, ValueError, OSError):
        pass
    # 等锁释放
    time.sleep(0.5)
    # 清理可能残留的 .lock 文件
    qdrant_dir = os.path.join(DATA_DIR, "qdrant")
    lock_file = os.path.join(qdrant_dir, ".lock")
    if os.path.exists(lock_file):
        try:
            os.remove(lock_file)
            print(f"[startup] 🧹 清理残留 .lock 文件", file=sys.stderr)
        except OSError:
            pass

_cleanup_zombie_lock()

# ── 日志 ──
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "mcp_server.log")),
        logging.StreamHandler(sys.stderr),
    ],
)
logger = logging.getLogger("mem0-mcp")

# ── mem0 ──
try:
    from mem0 import Memory
    logger.info("✅ mem0 SDK loaded")
except ImportError:
    logger.error("❌ mem0 not installed")
    sys.exit(1)

# 全局实例
_memory = None

def get_memory():
    global _memory
    if _memory is not None:
        return _memory
    config_path = Path(MEM0_CONFIG)
    if config_path.exists():
        with open(config_path) as f:
            cfg = json.load(f)
        # 从文件注入 SiliconFlow key
        if cfg.get("embedder", {}).get("config", {}).get("api_key") == "__SF_KEY__":
            kp = os.path.join(os.path.dirname(MEM0_CONFIG), ".sf_key")
            if os.path.exists(kp):
                with open(kp) as f:
                    cfg["embedder"]["config"]["api_key"] = f.read().strip()
                logger.info("✅ SiliconFlow key injected from file")
        # LLM key 从 .llm_key 文件注入（OpenAI 兼容接口）
        with open(os.path.join(BASE_DIR, ".llm_key")) as _kf:
            llm_key = _kf.read().strip()
        if cfg.get("llm", {}).get("config", {}).get("api_key") == "__SF_KEY__":
            cfg["llm"]["config"]["api_key"] = llm_key
            # base_url 保持 config 文件中的 https://opencode.ai/zen/go/v1，不覆盖
            if llm_key:
                logger.info("✅ LLM key injected from .llm_key (opencode-go)")
            else:
                logger.warning("⚠️ .llm_key 为空，LLM key 注入失败")
        _memory = Memory.from_config(cfg)
        logger.info("✅ mem0 初始化完成")
    else:
        _memory = Memory()
        logger.warning("⚠️ 使用默认配置")
    return _memory

# ── FastMCP ──
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("aidumem", log_level="INFO")


# ═══════════════════════════════════════════════
# MCP 工具
# ═══════════════════════════════════════════════

@mcp.tool()
def mem0_add(messages: str, user_id: str = DEFAULT_USER_ID) -> str:
    """添加记忆到 AI Agent 的大脑。

    Args:
        messages: JSON 字符串，格式 [{"role": "user/assistant", "content": "..."}]
        user_id: 用户标识，默认取 AIDUMEM_DEFAULT_USER_ID 环境变量
    """
    try:
        mem = get_memory()
        msg_list = json.loads(messages)
        result = mem.add(msg_list, user_id=user_id)
        # Phase 1.1: 注册 salience
        mids = []
        for r in result.get("results", []):
            mid = r.get("id", "")
            if mid:
                mids.append(mid)
                try:
                    on_memory_added(mid)
                except Exception:
                    pass
        return format_response(success({"stored": len(mids), "ids": mids}))
    except json.JSONDecodeError as e:
        return format_response(error("invalid_args", f"messages JSON 解析失败: {e}"))
    except Exception as e:
        logger.error(f"mem0_add 失败: {e}")
        return format_response(error("execution_error", str(e)))


@mcp.tool()
def mem0_search(query: str, user_id: str = DEFAULT_USER_ID, top_k: int = 5) -> str:
    """搜索记忆。Phase 1.3: 内置相关性闸门 + 显著性 boost。

    Args:
        query: 搜索关键词
        user_id: 用户标识，默认取 AIDUMEM_DEFAULT_USER_ID 环境变量
        top_k: 返回结果数量，默认 5
    """
    try:
        # ── Phase 1.3: 相关性闸门 ──
        gate = relevance_check(query)
        if not gate["needs_memory"]:
            return format_response(success({
                "count": 0,
                "results": [],
                "gate": gate
            }, warnings=[f"闸门跳过: {gate['reason']}"]))
        
        mem = get_memory()
        result = mem.search(query, filters={"user_id": user_id}, top_k=top_k)
        memories = result.get("results", [])
        
        # ── Phase 1.2: 显著性 boost（每次访问） ──
        for m in memories:
            mid = m.get("id", "")
            if mid:
                try:
                    on_memory_accessed(mid)
                except Exception:
                    pass
        
        return format_response(success({
            "count": len(memories),
            "results": memories,
            "gate": gate
        }))
    except Exception as e:
        return format_response(error("execution_error", str(e)))


@mcp.tool()
def mem0_recent(user_id: str = DEFAULT_USER_ID, limit: int = 10) -> str:
    """获取最近的记忆记录。"""
    try:
        mem = get_memory()
        result = mem.search("", filters={"user_id": user_id}, top_k=limit)
        memories = result.get("results", [])
        return format_response(success({"count": len(memories), "results": memories}))
    except Exception as e:
        return format_response(error("execution_error", str(e)))


@mcp.tool()
def mem0_stats(user_id: str = DEFAULT_USER_ID) -> str:
    """查看记忆统计信息。"""
    try:
        import sqlite3
        db_path = os.path.join(DATA_DIR, "qdrant", "collection", "mem0", "storage.sqlite")
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # 总记录数
        cursor.execute("SELECT COUNT(*) FROM points")
        total = cursor.fetchone()[0]
        
        # 解析所有点的payload，统计user_id分布
        cursor.execute("SELECT point FROM points")
        rows = cursor.fetchall()
        
        user_counts = {}
        hash_counts = {}
        tag_counts = {}
        
        for (point_blob,) in rows:
            try:
                point = json.loads(point_blob)
                payload = point.get('payload', {})
                uid = payload.get('user_id', 'unknown')
                h = payload.get('hash', '')
                mem_text = payload.get('data', '')
                
                user_counts[uid] = user_counts.get(uid, 0) + 1
                if h:
                    hash_counts[h] = hash_counts.get(h, 0) + 1
                
                if mem_text.startswith('['):
                    tag = mem_text.split(']')[0] + ']'
                    tag_counts[tag] = tag_counts.get(tag, 0) + 1
            except:
                pass
        
        conn.close()
        
        dupes = {h: c for h, c in hash_counts.items() if c > 1}
        total_dupes = sum(c - 1 for c in dupes.values())
        
        # Phase 1.2: 也返回 salience 统计
        from ducky.memory_salience import get_stats as salience_stats
        sal = salience_stats()
        
        return format_response(success({
            "user_id": user_id,
            "total_memories": total,
            "user_distribution": user_counts,
            "unique_hashes": len(hash_counts),
            "duplicate_hashes": len(dupes),
            "duplicate_count": total_dupes,
            "after_dedup": total - total_dupes,
            "top_tags": dict(sorted(tag_counts.items(), key=lambda x: -x[1])[:10]),
            "salience": sal
        }))
    except Exception as e:
        return format_response(error("execution_error", str(e)))


@mcp.tool()
def mem0_delete(memory_id: str) -> str:
    """删除指定记忆。

    Args:
        memory_id: 记忆ID
    """
    try:
        mem = get_memory()
        result = mem.delete(memory_id)
        return format_response(success({"deleted": memory_id}))
    except Exception as e:
        logger.error(f"mem0_delete 失败: {e}")
        return format_response(error("execution_error", str(e)))


@mcp.tool()
def mem0_delete_all(user_id: str = DEFAULT_USER_ID) -> str:
    """删除所有记忆（危险操作！）。"""
    try:
        mem = get_memory()
        result = mem.delete_all(user_id=user_id)
        return format_response(success({"cleared": True}))
    except Exception as e:
        logger.error(f"mem0_delete_all 失败: {e}")
        return format_response(error("execution_error", str(e)))


@mcp.tool()
def mem0_update(memory_id: str, data: str) -> str:
    """更新指定记忆。

    Args:
        memory_id: 记忆ID
        data: 新的记忆内容
    """
    try:
        mem = get_memory()
        result = mem.update(memory_id, data)
        return format_response(success({"updated": memory_id}))
    except Exception as e:
        logger.error(f"mem0_update 失败: {e}")
        return format_response(error("execution_error", str(e)))


# ═══════════════════════════════════════════════
# 后台自动记忆线程
# ═══════════════════════════════════════════════

def _read_last_id():
    if os.path.exists(LAST_ID_FILE):
        with open(LAST_ID_FILE) as f:
            return int(f.read().strip())
    return 0

def _write_last_id(msg_id):
    os.makedirs(os.path.dirname(LAST_ID_FILE), exist_ok=True)
    with open(LAST_ID_FILE, "w") as f:
        f.write(str(msg_id))

def _fetch_new_messages(last_id, limit=200):
    """获取未处理的新消息"""
    if not STATE_DB:
        logger.info("未配置 AIDUMEM_HOST_STATE_DB，跳过宿主会话自动记忆")
        return [], 0
    if not os.path.exists(STATE_DB):
        logger.warning(f"宿主会话库不存在: {STATE_DB}")
        return [], 0

    try:
        conn = sqlite3.connect(STATE_DB)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
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
        rows = cursor.fetchall()
        conn.close()

        messages = [{
            "id": r["id"],
            "session_id": r["session_id"],
            "role": r["role"],
            "content": r["content"][:2000],
            "timestamp": r["timestamp"],
            "source": r["source"],
        } for r in rows]

        max_id = max((m["id"] for m in messages), default=last_id)
        return messages, max_id
    except Exception as e:
        logger.error(f"读取 state.db 失败: {e}")
        return [], last_id

def _group_by_session(messages):
    """按 session 分组"""
    sessions = {}
    for msg in messages:
        sid = msg["session_id"]
        if sid not in sessions:
            sessions[sid] = []
        sessions[sid].append(msg)
    for sid in sessions:
        sessions[sid].sort(key=lambda x: x["id"])
    return sessions

def _format_conversation(messages):
    """格式化成对话文本"""
    parts = []
    for msg in messages:
        label = "User" if msg["role"] == "user" else "Assistant"
        parts.append(f"{label}: {msg['content']}")
    return "\n\n".join(parts)

def run_auto_memory():
    """执行一次自动记忆提取"""
    mem = get_memory()
    last_id = _read_last_id()
    messages, max_id = _fetch_new_messages(last_id)

    if not messages:
        logger.info(f"📭 自动记忆: 无新消息（上次 ID {last_id}）")
        return

    sessions = _group_by_session(messages)
    cron_sessions = sum(1 for s in sessions if any(m.get("source") == "cron" for m in sessions[s]))
    total_stored = 0

    for sid, msgs in sessions.items():
        # 跳过 cron 输出（避免自己记自己）
        if any(m.get("source") == "cron" for m in msgs):
            continue
        # 至少一问一答才有记忆价值
        if len(msgs) < 2:
            continue

        conversation = _format_conversation(msgs)
        try:
            result = mem.add(
                [
                    {"role": "system", "content": "你是 AI 助手，正在和用户聊天。"},
                    {"role": "user", "content": conversation},
                ],
                user_id=DEFAULT_USER_ID,
                metadata={"source": "auto_memory", "session_id": sid},
            )
            memories = result.get("results", [])
            if memories:
                total_stored += len(memories)
                for mem_entry in memories:
                    logger.info(f"  📝 自动记忆: {mem_entry.get('memory', '')[:100]}")
        except Exception as e:
            logger.warning(f"  ⚠️ session {sid} 记忆失败: {e}")

    _write_last_id(max_id)
    logger.info(f"✅ 自动记忆完成: 新增 {total_stored} 条记忆，下次从 ID {max_id} 开始")

def auto_memory_loop():
    """自动记忆后台线程"""
    logger.info("🕐 自动记忆线程已启动（首次执行在10分钟后，之后每小时一次）")
    time.sleep(600)  # 首次延迟10分钟，让系统先稳定
    while True:
        try:
            run_auto_memory()
        except Exception as e:
            logger.error(f"❌ 自动记忆异常: {e}", exc_info=True)
        time.sleep(21600)  # 每6小时执行一次


# ═══════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sse", action="store_true")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--host", type=str, default="127.0.0.1")
    args = parser.parse_args()

    # 预热 mem0
    logger.info("🚀 预热 mem0...")
    try:
        get_memory()
        logger.info("✅ mem0 预热完成")
    except Exception as e:
        logger.warning(f"⚠️ mem0 预热失败: {e}")

    # 启动后台自动记忆线程
    threading.Thread(target=auto_memory_loop, daemon=True).start()

    if args.sse:
        logger.info(f"🌐 SSE 模式，端口 {args.port}")
        import uvicorn
        app = mcp.sse_app(mount_path="/sse")
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    else:
        logger.info("📟 stdio 模式启动")
        mcp.run(transport="stdio")
