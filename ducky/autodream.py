"""
aiduMEM AutoDream — 7 天自动记忆蒸馏
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
v11 Hyperion · 定期蒸馏：合并重复事实、提炼操作日志、标记过时记忆
"""
import json
import logging
import threading
import time
import os
from datetime import datetime, timedelta
from pathlib import Path

from ducky.utils import get_facts_conn
from ducky.version import FULL_VERSION

logger = logging.getLogger("aiduMEM.AutoDream")

REPORT_FILE = str(Path(__file__).resolve().parent.parent / "logs" / "autodream_report.json")

# 蒸馏间隔（7 天）
DREAM_INTERVAL_SECONDS = 7 * 24 * 3600

_lock = threading.Lock()
_last_dream_time: str | None = None
_table_checked = False


def _ensure_table():
    """确保 autodream_log 表存在"""
    global _table_checked
    if _table_checked:
        return
    with _lock:
        if _table_checked:
            return
        conn = get_facts_conn()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS autodream_log (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    action      TEXT NOT NULL,       -- merge / refine / supersede
                    source_ids  TEXT,                -- JSON array of source fact IDs
                    target_id   INTEGER,
                    new_content TEXT,
                    reason      TEXT,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
            _table_checked = True
        except Exception as e:
            logger.error(f"AutoDream ensure table error: {e}")
            raise
        finally:
            conn.close()


def _load_last_dream_time():
    """从报告文件读取上次蒸馏时间"""
    global _last_dream_time
    if os.path.exists(REPORT_FILE):
        try:
            with open(REPORT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                _last_dream_time = data.get("dream_time")
        except Exception as e:
            logger.warning(f"Failed to read last dream time from report file: {e}")


def _save_report(dream_time: str, stats: dict):
    """保存蒸馏报告"""
    global _last_dream_time
    _last_dream_time = dream_time
    report = {
        "dream_time": dream_time,
        "stats": stats,
        "version": FULL_VERSION,
    }
    try:
        os.makedirs(os.path.dirname(REPORT_FILE), exist_ok=True)
        with open(REPORT_FILE, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to save dream report: {e}")


def get_dream_status() -> dict:
    """获取 AutoDream 状态"""
    _load_last_dream_time()
    next_dream = None
    status = "never_run"
    
    if _last_dream_time:
        try:
            last = datetime.fromisoformat(_last_dream_time)
            next_dt = last + timedelta(seconds=DREAM_INTERVAL_SECONDS)
            next_dream = next_dt.isoformat()
            status = "ready"
        except Exception as e:
            logger.warning(f"Parse last dream time error: {e}")
            
    return {
        "last_dream": _last_dream_time,
        "next_dream": next_dream,
        "interval_days": 7,
        "status": status,
    }


def _get_recent_facts(days: int = 7) -> list:
    """获取最近 N 天新增且未被归档的事实"""
    conn = get_facts_conn()
    # row_factory=sqlite3.Row 已由 utils._get_thread_conn 统一设置

    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    rows = []
    try:
        rows = conn.execute("""
            SELECT id, fact_key, fact_value, category, created_at 
            FROM facts 
            WHERE created_at >= ? AND (archived IS NULL OR archived = 0)
            ORDER BY fact_key
        """, (cutoff,)).fetchall()
    except Exception as e:
        logger.error(f"Failed to query facts table: {e}")
        conn.close()
        return []

    facts = []
    for r in rows:
        facts.append({
            "id": r["id"],
            "fact_key": r["fact_key"] if "fact_key" in r.keys() else "",
            "content": r["fact_value"] if "fact_value" in r.keys() else "",
            "category": r["category"] if "category" in r.keys() else "general",
            "created_at": r["created_at"] if "created_at" in r.keys() else "",
        })
    conn.close()
    return facts


def _cluster_by_prefix(facts: list) -> dict:
    """按 fact_key 前缀聚类"""
    clusters = {}
    for f in facts:
        key = f.get("fact_key", "")
        prefix = key.split(":")[0].split("_")[0] if key else "other"
        clusters.setdefault(prefix, []).append(f)
    return clusters


def _simple_merge(clusters: dict) -> dict:
    """基于事实特征规则的合并（轻量、低内存）"""
    stats = {"merged": 0, "refined": 0, "superseded": 0, "total_facts": 0}

    conn = get_facts_conn()
    now = datetime.now().isoformat()

    try:
        for prefix, facts in clusters.items():
            stats["total_facts"] += len(facts)
            if len(facts) < 2:
                continue

            content_groups = {}
            for f in facts:
                content = f.get("content", "")
                short = content[:30].strip().lower()
                content_groups.setdefault(short, []).append(f)

            for short, group in content_groups.items():
                if len(group) < 2:
                    continue

                best = max(group, key=lambda x: len(x.get("content", "")))
                merged_ids = [f["id"] for f in group if f["id"] != best["id"]]

                if merged_ids:
                    conn.execute(
                        "INSERT INTO autodream_log (action, source_ids, target_id, new_content, reason, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        ("merge", json.dumps(merged_ids), best["id"], best["content"],
                         f"合并 {len(merged_ids)} 条同类事实（前缀: {prefix}）", now)
                    )
                    stats["merged"] += len(merged_ids)

                    for mid in merged_ids:
                        try:
                            # 🟢24：不再物理改写 fact_value（原「|| ' [superseded by #id]'」会破坏原文）。
                            # 仅置 archived=1 归档；被谁取代的溯源信息已完整记在 autodream_log
                            # （action=merge, source_ids, target_id, reason），可无损回溯。
                            conn.execute(
                                "UPDATE facts SET archived = 1, archived_at = ? WHERE id = ?",
                                (now, mid)
                            )
                        except Exception as e:
                            logger.error(f"Failed to update fact #{mid}: {e}")
        conn.commit()
    except Exception as e:
        logger.error(f"AutoDream simple merge database write error: {e}")
        conn.rollback()
    finally:
        conn.close()
        
    return stats


def trigger_dream() -> dict:
    """手动或自动触发蒸馏"""
    _ensure_table()
    global _last_dream_time

    with _lock:
        now = datetime.now()
        dream_time = now.isoformat()

        logger.info("🌙 AutoDream 蒸馏开始...")

        # 1. 获取最近 7 天事实
        facts = _get_recent_facts(days=7)
        logger.info(f"  → 扫描到 {len(facts)} 条事实")

        if len(facts) < 5:
            logger.info("  → 事实太少，跳过蒸馏")
            _save_report(dream_time, {"skipped": True, "reason": "too_few_facts", "total": len(facts)})
            return {"status": "skipped", "reason": "too_few_facts", "total_facts": len(facts)}

        # 2. 聚类
        clusters = _cluster_by_prefix(facts)
        logger.info(f"  → 聚类: {len(clusters)} 组")

        # 3. 简单合并
        stats = _simple_merge(clusters)
        logger.info(f"  → 蒸馏完成: 合并 {stats['merged']} 条")

        # 4. 保存报告
        _save_report(dream_time, stats)
        logger.info(f"✅ AutoDream 蒸馏完成: {json.dumps(stats)}")

        return {"status": "completed", "dream_time": dream_time, "stats": stats}


def get_dream_report() -> dict:
    """获取最近一次蒸馏报告"""
    if not os.path.exists(REPORT_FILE):
        return {"status": "no_report"}
    try:
        with open(REPORT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to read dream report: {e}")
        return {"status": "error", "message": str(e)}


def autodream_background_loop():
    """后台线程：智能调度 7 天周期蒸馏"""
    logger.info("🌙 AutoDream 后台线程启动（检查间隔 10 分钟）")

    while True:
        try:
            _ensure_table()
            status = get_dream_status()
            
            # 如果从没运行过，1小时后触发第一次
            if status["status"] == "never_run":
                logger.info("  → 首次运行，等待 1 小时后触发首轮蒸馏...")
                time.sleep(3600)
                trigger_dream()
                continue
                
            next_dream_str = status["next_dream"]
            if next_dream_str:
                next_dream = datetime.fromisoformat(next_dream_str)
                now = datetime.now()
                
                # 如果当前时间已超过 next_dream，立即触发
                if now >= next_dream:
                    logger.info(f"  → 当前时间 {now.isoformat()} 已过蒸馏周期 {next_dream_str}，启动蒸馏...")
                    trigger_dream()
                else:
                    diff = (next_dream - now).total_seconds()
                    # 每次最长休眠 10 分钟，以响应服务的优雅退出/重启检测，不一次 sleep 很多天
                    sleep_time = min(diff, 600)
                    if sleep_time > 0:
                        time.sleep(sleep_time)
            else:
                time.sleep(600)
        except Exception as e:
            logger.error(f"AutoDream 后台异常: {e}")
            time.sleep(600)  # 出错后等 10 分钟再试


if __name__ == "__main__":
    _ensure_table()
    _load_last_dream_time()

    print("=== 状态 ===")
    print(json.dumps(get_dream_status(), ensure_ascii=False, indent=2))

    print("\n=== 触发蒸馏 ===")
    result = trigger_dream()
    print(json.dumps(result, ensure_ascii=False, indent=2))

    print("\n=== 报告 ===")
    print(json.dumps(get_dream_report(), ensure_ascii=False, indent=2))
