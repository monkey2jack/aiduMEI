"""
aiduMEM Checkpoint — 5 段会话快照
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
v11 Hyperion · 每次上下文压缩时自动生成，下次会话启动时注入

v11.1 Opus 升级：30天失效标注，陈旧快照注入时自动标注
"""
import json
import logging
import threading
from datetime import datetime, timedelta

from ducky.utils import get_facts_conn

logger = logging.getLogger("aiduMEM.Checkpoint")

# 五段快照的键名和显示标签
CP_BLOCKS = {
    "cp_active_intent":   "🎯 在做",
    "cp_next_action":     "⏭️ 下一步",
    "cp_current_work":    "📁 工作区",
    "cp_key_decisions":   "🔑 决策",
    "cp_open_notes":      "📝 待办",
}

MAX_SESSIONS = 5  # 只保留最近 5 个会话的快照
STALENESS_DAYS = 30  # 快照超过此天数注入时标注为陈旧
_init_lock = threading.Lock()
_table_checked = False


def _ensure_table():
    """确保 checkpoints 表及索引存在"""
    global _table_checked
    if _table_checked:
        return
    with _init_lock:
        if _table_checked:
            return
        conn = get_facts_conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS checkpoints (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id  TEXT NOT NULL,
                    block_key   TEXT NOT NULL,
                    content     TEXT NOT NULL,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_checkpoints_session
                ON checkpoints(session_id)
            """)
            conn.commit()
            _table_checked = True
        except Exception as e:
            logger.error(f"Checkpoint ensure table error: {e}")
            raise
        finally:
            conn.close()


def write_checkpoint(session_id: str, blocks: dict) -> dict:
    """写入一个会话的 5 段快照"""
    _ensure_table()
    if not session_id or len(session_id.strip()) < 3:
        raise ValueError("session_id 无效，长度至少 3 字符")

    session_id = session_id.strip()
    now = datetime.now().isoformat()
    conn = get_facts_conn()

    count = 0
    try:
        # 为防止同一个 session 重复写入，我们先删除该 session 已有的快照
        conn.execute("DELETE FROM checkpoints WHERE session_id = ?", (session_id,))
        
        for key, label in CP_BLOCKS.items():
            content = blocks.get(key, "")
            if content and len(str(content).strip()) >= 3:
                conn.execute(
                    "INSERT INTO checkpoints (session_id, block_key, content, created_at) VALUES (?, ?, ?, ?)",
                    (session_id, key, str(content).strip()[:600], now)
                )
                count += 1
        conn.commit()
    except Exception as e:
        logger.error(f"Checkpoint write error for session {session_id}: {e}")
        raise
    finally:
        conn.close()

    logger.info(f"Checkpoint 写入: session={session_id}, {count}/5 段")
    return {"session_id": session_id, "blocks_written": count, "status": "ok"}


def get_latest_checkpoint() -> dict | None:
    """获取最近一次会话的完整快照"""
    _ensure_table()
    conn = get_facts_conn()

    try:
        # 找最新 session_id
        row = conn.execute("""
            SELECT session_id, MAX(created_at) as latest 
            FROM checkpoints 
            GROUP BY session_id 
            ORDER BY latest DESC 
            LIMIT 1
        """).fetchone()

        if not row:
            return None

        session_id = row["session_id"]
        rows = conn.execute(
            "SELECT block_key, content, created_at FROM checkpoints WHERE session_id = ? ORDER BY id",
            (session_id,)
        ).fetchall()

        blocks = {}
        for r in rows:
            blocks[r["block_key"]] = r["content"]

        return {
            "session_id": session_id,
            "blocks": blocks,
            "created_at": rows[0]["created_at"] if rows else None,
        }
    except Exception as e:
        logger.error(f"Checkpoint get_latest_checkpoint error: {e}")
        return None
    finally:
        conn.close()


def get_checkpoint(session_id: str) -> dict | None:
    """获取指定会话的快照"""
    _ensure_table()
    conn = get_facts_conn()
    try:
        rows = conn.execute(
            "SELECT block_key, content, created_at FROM checkpoints WHERE session_id = ? ORDER BY id",
            (session_id,)
        ).fetchall()

        if not rows:
            return None

        blocks = {}
        for r in rows:
            blocks[r["block_key"]] = r["content"]

        return {
            "session_id": session_id,
            "blocks": blocks,
            "created_at": rows[0]["created_at"],
        }
    except Exception as e:
        logger.error(f"Checkpoint get_checkpoint error for {session_id}: {e}")
        return None
    finally:
        conn.close()


def cleanup_old_checkpoints() -> dict:
    """清理超过 MAX_SESSIONS 个会话之前的旧快照"""
    _ensure_table()
    conn = get_facts_conn()
    try:
        # 找所有 session_id 按时间排序
        rows = conn.execute("""
            SELECT session_id, MAX(created_at) as latest 
            FROM checkpoints 
            GROUP BY session_id 
            ORDER BY latest DESC
        """).fetchall()

        if len(rows) <= MAX_SESSIONS:
            return {"kept": len(rows), "deleted": 0, "status": "no_cleanup_needed"}

        # 保留最近 MAX_SESSIONS 个，删除更早的
        keep_sessions = [r["session_id"] for r in rows[:MAX_SESSIONS]]
        
        # 构造安全占位符删除
        placeholders = ",".join("?" for _ in keep_sessions)
        cursor = conn.execute(
            f"DELETE FROM checkpoints WHERE session_id NOT IN ({placeholders})",
            keep_sessions
        )
        deleted = cursor.rowcount
        conn.commit()
        
        logger.info(f"Checkpoint 清理: 保留 {len(keep_sessions)} 个会话, 删除 {deleted} 行记录")
        return {"kept": len(keep_sessions), "deleted": deleted, "status": "cleaned"}
    except Exception as e:
        logger.error(f"Checkpoint cleanup error: {e}")
        return {"status": "error", "message": str(e)}
    finally:
        conn.close()


def inject_context() -> str:
    """生成 Checkpoint 注入文本（超过 30 天自动标注陈旧）"""
    cp = get_latest_checkpoint()
    if not cp or not cp.get("blocks"):
        return ""

    # 判断是否陈旧
    stale = False
    created = cp.get("created_at", "")
    if created:
        try:
            created_dt = datetime.fromisoformat(created)
            stale = datetime.now() - created_dt > timedelta(days=STALENESS_DAYS)
        except (ValueError, TypeError):
            stale = True

    header = "[Checkpoint · 上次会话]"
    if stale:
        header = f"[Checkpoint · 上次会话 ⚠️ {STALENESS_DAYS}天+前，仅供参考]"

    lines = [header]
    for key, label in CP_BLOCKS.items():
        content = cp["blocks"].get(key, "")
        if content.strip():
            lines.append(f"{label}: {content}")
    return "\n".join(lines) if len(lines) > 1 else ""


if __name__ == "__main__":
    _ensure_table()
    test_blocks = {
        "cp_active_intent": "aiduMEM v11 Hyperion 智慧引擎",
        "cp_next_action": "完成 AutoDream 模块后集成测试",
        "cp_current_work": "ducky/core_memory.py 已完成 · ducky/checkpoint.py 测试中",
        "cp_key_decisions": "三模块独立开发→独立测试→合入 api_server.py",
        "cp_open_notes": "集成测试后更新白皮书和架构文档",
    }

    result = write_checkpoint("test-session-20260727", test_blocks)
    print("写入:", json.dumps(result, ensure_ascii=False, indent=2))

    cp = get_latest_checkpoint()
    print("\n最新快照:", json.dumps(cp, ensure_ascii=False, indent=2))

    print("\n=== 注入上下文 ===")
    print(inject_context())

    cleanup = cleanup_old_checkpoints()
    print("\n清理:", json.dumps(cleanup, ensure_ascii=False))
