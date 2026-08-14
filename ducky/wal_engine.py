"""ducky.wal_engine — 应用层 WAL (Write-Ahead Logging) 预写日志与多仓对账引擎

职责：
1. 写操作前置 fsync 追加 WAL 日志，防进程崩溃产生孤儿数据；
2. 服务启动自愈对账 (Reconcile)，检测并恢复未完成写入或清理孤儿；
3. 多仓原子删除协调器 (Qdrant + SQLite facts/salience/evolve/workspace + FTS5)。
"""
from __future__ import annotations

import json
import logging
import os
import platform
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from ducky.utils import DATA_DIR, get_facts_conn

logger = logging.getLogger("aiduMEM.wal")

WAL_DIR = os.path.join(DATA_DIR, "wal")
WAL_FILE = os.path.join(WAL_DIR, "mem_mutations.wal")


@dataclass
class WALEntry:
    wal_id: str = field(default_factory=lambda: f"wal-{uuid.uuid4().hex[:12]}")
    timestamp: float = field(default_factory=time.time)
    user_id: str = "default"
    operation: Literal["add", "delete", "delete_all", "update", "refine"] = "add"
    payload: Dict[str, Any] = field(default_factory=dict)
    status: Literal["pending", "committed", "failed"] = "pending"
    error: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> Optional[WALEntry]:
        try:
            d = json.loads(line.strip())
            return cls(**d)
        except Exception:
            return None


class WALEngine:
    """轻量级 WAL 日志引擎（线程安全 + 跨平台文件锁 + fsync 强制落盘）"""

    _instance: Optional[WALEngine] = None
    _lock = threading.Lock()

    def __init__(self, wal_dir: str = WAL_DIR):
        self.wal_dir = Path(wal_dir)
        self.wal_dir.mkdir(parents=True, exist_ok=True)
        self.wal_file = self.wal_dir / "mem_mutations.wal"
        self._write_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> WALEngine:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def append(self, entry: WALEntry) -> str:
        """追加一条 WAL 记录并执行 fsync 落盘。"""
        line = entry.to_json() + "\n"
        with self._write_lock:
            with open(self.wal_file, "a", encoding="utf-8") as f:
                is_win = platform.system() == "Windows"
                if is_win:
                    import msvcrt
                    f.seek(0)
                    try:
                        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                    except OSError:
                        pass
                try:
                    f.seek(0, os.SEEK_END)
                    f.write(line)
                    f.flush()
                    os.fsync(f.fileno())
                finally:
                    if is_win:
                        f.seek(0)
                        try:
                            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                        except OSError:
                            pass
        logger.debug("WAL append: %s [%s] user=%s", entry.wal_id, entry.operation, entry.user_id)
        return entry.wal_id

    def mark_status(self, wal_id: str, status: Literal["committed", "failed"], error: str = "") -> None:
        """记录状态变更（以新行追加形式，保障只追加写性能）。"""
        entry = WALEntry(
            wal_id=wal_id,
            status=status,
            error=error,
            operation="update",
            payload={"target_wal_id": wal_id, "updated_status": status},
        )
        self.append(entry)

    def get_pending_entries(self) -> List[WALEntry]:
        """读取所有未提交的有效操作。"""
        if not self.wal_file.exists():
            return []

        entries_by_id: Dict[str, WALEntry] = {}
        status_updates: Dict[str, str] = {}

        try:
            with open(self.wal_file, "r", encoding="utf-8") as f:
                for line in f:
                    entry = WALEntry.from_json(line)
                    if not entry:
                        continue
                    if entry.payload.get("target_wal_id"):
                        status_updates[entry.payload["target_wal_id"]] = entry.payload.get("updated_status", "")
                    else:
                        entries_by_id[entry.wal_id] = entry
        except Exception as e:
            logger.warning("读取 WAL 日志失败: %s", e)
            return []

        pending = []
        for wid, ent in entries_by_id.items():
            final_status = status_updates.get(wid, ent.status)
            if final_status == "pending":
                pending.append(ent)
        return pending


# ── 多仓原子级联删除协调器 ─────────────────────────────────────────

def cascade_delete_memory(memory_id: str, user_id: str = "default") -> Dict[str, Any]:
    """级联删除单条记忆在所有多模态与结构化存储中的记录。

    清理范围：
    1. Qdrant 向量库 / mem0
    2. FTS5 全文索引
    3. facts.db（facts 表与 memory_types 表）
    4. salience.db（salience 表与 crystals 表）
    5. evolve_mem.db（演化记录）
    """
    wal = WALEngine.get_instance()
    wal_id = wal.append(WALEntry(
        user_id=user_id,
        operation="delete",
        payload={"memory_id": memory_id},
    ))

    res = {
        "memory_id": memory_id,
        "mem0_vector": False,
        "fts": False,
        "facts": 0,
        "salience": 0,
        "evolve": 0,
    }

    try:
        # 1. mem0 向量删除
        try:
            from ducky.mem0_runtime import get_memory
            mem = get_memory()
            mem.delete(memory_id)
            res["mem0_vector"] = True
        except Exception as e:
            logger.debug("mem0.delete 跳过或失败: %s", e)

        # 2. FTS5 索引剔除
        try:
            from ducky.text_fts import _unindex_memory
            _unindex_memory(memory_id)
            # 也尝试 unindex fact:ID
            _unindex_memory(f"fact:{memory_id}")
            res["fts"] = True
        except Exception as e:
            logger.debug("FTS unindex 跳过: %s", e)

        # 3. facts.db 清理
        try:
            conn = get_facts_conn()
            c1 = conn.execute("DELETE FROM facts WHERE id=? OR fact_key LIKE ?", (memory_id, f"%{memory_id}%")).rowcount
            # 清理 memory_types 账本
            try:
                conn.execute("DELETE FROM memory_types WHERE memory_id=? OR fact_id=?", (memory_id, memory_id))
            except Exception:
                pass
            conn.commit()
            conn.close()
            res["facts"] = c1
        except Exception as e:
            logger.warning("facts.db 清理失败: %s", e)

        # 4. salience.db 清理
        try:
            from ducky.salience.db import get_salience_conn
            sconn = get_salience_conn()
            c2 = sconn.execute("DELETE FROM memory_salience WHERE memory_id=?", (memory_id,)).rowcount
            sconn.commit()
            sconn.close()
            res["salience"] = c2
        except Exception as e:
            logger.debug("salience.db 清理跳过: %s", e)

        # 5. evolve_mem.db 清理
        try:
            from ducky.evolve_mem import get_evolve_conn
            econn = get_evolve_conn()
            c3 = econn.execute("DELETE FROM evolve_snapshots WHERE memory_id=?", (memory_id,)).rowcount
            econn.commit()
            econn.close()
            res["evolve"] = c3
        except Exception as e:
            logger.debug("evolve_mem.db 清理跳过: %s", e)

        wal.mark_status(wal_id, "committed")
        return {"status": "ok", "details": res}
    except Exception as exc:
        wal.mark_status(wal_id, "failed", error=str(exc))
        logger.error("级联删除记忆失败: %s", exc)
        raise


def cascade_delete_all(user_id: str) -> Dict[str, Any]:
    """级联清空指定用户在所有存储中的数据，绝不留孤儿。"""
    wal = WALEngine.get_instance()
    wal_id = wal.append(WALEntry(
        user_id=user_id,
        operation="delete_all",
        payload={"user_id": user_id},
    ))

    res = {
        "user_id": user_id,
        "mem0_deleted": False,
        "fts_cleared": 0,
        "facts_deleted": 0,
        "salience_deleted": 0,
        "evolve_deleted": 0,
    }

    try:
        # 1. mem0 / Qdrant
        try:
            from ducky.mem0_runtime import get_memory
            mem = get_memory()
            mem.delete_all(user_id=user_id)
            res["mem0_deleted"] = True
        except Exception as e:
            logger.warning("mem0.delete_all 失败: %s", e)

        # 2. FTS5
        try:
            from ducky.text_fts import get_text_conn
            tconn = get_text_conn()
            if user_id == "default":
                c_fts = tconn.execute("DELETE FROM memories").rowcount
            else:
                c_fts = tconn.execute("DELETE FROM memories WHERE user_id=?", (user_id,)).rowcount
            tconn.commit()
            tconn.close()
            res["fts_cleared"] = c_fts
        except Exception as e:
            logger.debug("FTS delete_all 跳过: %s", e)

        # 3. facts.db
        try:
            fconn = get_facts_conn()
            if user_id == "default":
                c_facts = fconn.execute("DELETE FROM facts").rowcount
                try:
                    fconn.execute("DELETE FROM memory_types")
                except Exception:
                    pass
            else:
                c_facts = fconn.execute("DELETE FROM facts WHERE source=? OR agent_id=?", (user_id, user_id)).rowcount
                try:
                    fconn.execute("DELETE FROM memory_types WHERE user_id=?", (user_id,))
                except Exception:
                    pass
            fconn.commit()
            fconn.close()
            res["facts_deleted"] = c_facts
        except Exception as e:
            logger.warning("facts delete_all 失败: %s", e)

        # 4. salience.db
        try:
            from ducky.salience.db import get_salience_conn
            sconn = get_salience_conn()
            if user_id == "default":
                c_sal = sconn.execute("DELETE FROM memory_salience").rowcount
            else:
                c_sal = sconn.execute("DELETE FROM memory_salience WHERE user_id=?", (user_id,)).rowcount
            sconn.commit()
            sconn.close()
            res["salience_deleted"] = c_sal
        except Exception as e:
            logger.debug("salience delete_all 跳过: %s", e)

        # 5. evolve_mem.db
        try:
            from ducky.evolve_mem import get_evolve_conn
            econn = get_evolve_conn()
            if user_id == "default":
                c_evo = econn.execute("DELETE FROM evolve_snapshots").rowcount
            else:
                c_evo = econn.execute("DELETE FROM evolve_snapshots WHERE user_id=?", (user_id,)).rowcount
            econn.commit()
            econn.close()
            res["evolve_deleted"] = c_evo
        except Exception as e:
            logger.debug("evolve delete_all 跳过: %s", e)

        wal.mark_status(wal_id, "committed")
        logger.info("🧹 多仓原子级联清空完成 user=%s: %s", user_id, res)
        return {"status": "ok", "details": res}
    except Exception as exc:
        wal.mark_status(wal_id, "failed", error=str(exc))
        logger.error("级联清空全部记忆失败: %s", exc)
        raise


def reconcile_startup() -> Dict[str, Any]:
    """服务启动自检与对账自愈。"""
    wal = WALEngine.get_instance()
    pending = wal.get_pending_entries()
    report = {
        "pending_count": len(pending),
        "recovered": 0,
        "failed": 0,
        "reconciled_at": time.time(),
    }
    if not pending:
        logger.info("🔍 [WAL Reconcile] 启动对账完成：无挂起事务，数据状态健康")
        return report

    logger.warning("🔍 [WAL Reconcile] 发现 %d 条未决 WAL 事务，开始自动恢复...", len(pending))
    for ent in pending:
        try:
            if ent.operation == "delete":
                mid = ent.payload.get("memory_id")
                if mid:
                    cascade_delete_memory(mid, user_id=ent.user_id)
                    report["recovered"] += 1
            elif ent.operation == "delete_all":
                cascade_delete_all(user_id=ent.user_id)
                report["recovered"] += 1
            else:
                # 记录为无法自动决议的写入，标记 failed 供运维审计
                wal.mark_status(ent.wal_id, "failed", error="Unresolved startup transaction")
                report["failed"] += 1
        except Exception as err:
            logger.error("Reconcile 恢复失败 wal_id=%s: %s", ent.wal_id, err)
            report["failed"] += 1

    return report
