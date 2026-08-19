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

from ducky.utils import DATA_DIR, DEFAULT_USER_ID, get_facts_conn

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
    3. facts.db（facts 表与 memory_types 表，严格校验归属与精确匹配）
    4. salience.db（salience 表与 crystals 表）
    5. evolve_mem.db（演化记录）
    6. verbatim_turns + verbatim_fts_map（原文保真层）

    🔴P0-4（v19.4.1）：第 6 项此前缺失 —— v19.4.0 新增原文层后，单条删除
        只清了 1-5，逐字原文留在库里且仍能被 /search 召回。实测：写入一条
        含身份证号的原文 → cascade_delete_memory → count_verbatim 仍为 1、
        /search 照样命中。原文比蒸馏后的事实敏感得多，「删除」不覆盖它
        等于删除权没有真正兑现，也与文档「绝不留孤儿」的承诺不符。
    """
    wal = WALEngine.get_instance()
    wal_id = wal.append(WALEntry(
        user_id=user_id,
        operation="delete",
        payload={"memory_id": memory_id, "user_id": user_id},
    ))

    res = {
        "memory_id": memory_id,
        "user_id": user_id,
        "mem0_vector": False,
        "fts": False,
        "facts": 0,
        "salience": 0,
        "evolve": 0,
        "verbatim": 0,
        "tombstone_id": None,
    }

    try:
        # 0z. 🔴P0-4b（v19.4.1 实机冒烟）：memory_id 形如 "verbatim:<n>" 时，
        #     这是 /search 返回原文证据时给出的句柄 —— 调用方手里只有它。
        #     此类条目往往没有对应的 mem0 记忆，走下面的常规链一条也删不掉
        #     （实机：verbatim=0、原文照旧可检索），成为「可检索但删不掉的孤儿」。
        #     因此直接按 id 精确删除原文层并留 tombstone，然后结束。
        if str(memory_id).lower().startswith("verbatim:"):
            try:
                from ducky.tombstone import snapshot_before_delete
                res["tombstone_id"] = snapshot_before_delete(
                    memory_id, user_id=user_id, reason="cascade_delete_verbatim", actor="wal_engine"
                )
            except Exception as te:
                logger.debug("tombstone 快照跳过: %s", te)
            try:
                from ducky.verbatim_vault import delete_verbatim_by_id
                res["verbatim"] = delete_verbatim_by_id(user_id, memory_id)
            except Exception as ve:
                logger.warning("原文层按 id 删除失败: %s", ve)
            wal.mark_status(wal_id, "committed")
            logger.info("🧹 原文条目删除完成 %s: %s", memory_id, res)
            return {"status": "ok", "details": res}

        # 0a. 🔴P0-4：先把这条记忆的正文抓出来（用于定位原文层对应行）。
        #     必须在物理删除之前做 —— 一旦 facts/FTS 行被删，就再也无从
        #     反查该记忆的内容，原文层将永久成为孤儿。
        _content_for_verbatim = ""
        try:
            from ducky.tombstone import _capture_facts_row, _capture_fts_content
            _content_for_verbatim = _capture_fts_content(memory_id, user_id) or (
                (_capture_facts_row(memory_id, user_id) or {}).get("fact_value", "")
            )
        except Exception as ce:
            logger.debug("原文定位内容抓取跳过: %s", ce)

        # 0. 🪦 tombstone 快照（v19.4.0 Mímir 借鉴 B3）：物理删除前先把全文+理由留痕，
        #    误删可一键恢复。快照失败只记日志，绝不阻断删除主链路。
        try:
            from ducky.tombstone import snapshot_before_delete
            res["tombstone_id"] = snapshot_before_delete(
                memory_id, user_id=user_id, reason="cascade_delete", actor="wal_engine"
            )
        except Exception as te:
            logger.debug("tombstone 快照跳过: %s", te)

        # 1. mem0 向量删除
        try:
            from ducky.mem0_runtime import get_memory
            mem = get_memory()
            mem.delete(memory_id)
            res["mem0_vector"] = True
        except Exception as e:
            logger.debug("mem0.delete 跳过或失败: %s", e)

        # 2. FTS5 索引剔除（带 user_id 作用域）
        try:
            from ducky.text_fts import get_text_conn
            tconn = get_text_conn()
            if user_id == "default":
                tconn.execute("DELETE FROM memories WHERE id=? OR id=?", (memory_id, f"fact:{memory_id}"))
            else:
                tconn.execute("DELETE FROM memories WHERE (id=? OR id=?) AND (user_id=? OR user_id='default')", (memory_id, f"fact:{memory_id}", user_id))
            tconn.commit()
            tconn.close()
            res["fts"] = True
        except Exception as e:
            logger.debug("FTS unindex 跳过: %s", e)

        # 3. facts.db 清理（🔴P0-1 严格归属校验 + 🔴P0-2 精确匹配，彻底消除 LIKE 误删）
        try:
            conn = get_facts_conn()
            exact_keys = (memory_id, f"fact:{memory_id}", f"raw:{memory_id}")
            if user_id == "default":
                c1 = conn.execute(
                    """DELETE FROM facts 
                       WHERE id=? OR fact_key=? OR fact_key=? OR fact_key=?""",
                    (memory_id, exact_keys[0], exact_keys[1], exact_keys[2])
                ).rowcount
                try:
                    conn.execute("DELETE FROM memory_types WHERE memory_ref=? OR memory_ref=? OR ref_alt=?", (memory_id, f"fact:{memory_id}", memory_id))
                except Exception as e:
                    logger.debug(f"cascade_delete_memory: suppressed exception: {e}")
            else:
                c1 = conn.execute(
                    """DELETE FROM facts 
                       WHERE (id=? OR fact_key=? OR fact_key=? OR fact_key=?)
                         AND (source=? OR agent_id=?)""",
                    (memory_id, exact_keys[0], exact_keys[1], exact_keys[2], user_id, user_id)
                ).rowcount
                try:
                    conn.execute("DELETE FROM memory_types WHERE (memory_ref=? OR memory_ref=? OR ref_alt=?)", (memory_id, f"fact:{memory_id}", memory_id))
                except Exception as e:
                    logger.debug(f"cascade_delete_memory: suppressed exception: {e}")
            # 📒 事件账本（v19.4.0 Mímir 借鉴 B5）：与删除同事务留痕，同生共死
            try:
                from ducky.event_ledger import record_event
                record_event(conn, actor=user_id or "system", action="delete",
                             target_id=memory_id, reason="cascade_delete_memory")
            except Exception as le:
                logger.debug("ledger 记录跳过: %s", le)
            conn.commit()
            conn.close()
            res["facts"] = c1
        except Exception as e:
            logger.warning("facts.db 清理失败: %s", e)

        # 4. salience.db 清理（v19.4.1 修复：此前同样从未真正执行）
        #
        #    原实现 `DELETE FROM memory_salience WHERE memory_id=? AND user_id=?`
        #    有两个错误：真实表名是 `salience`（不是 memory_salience），
        #    且该表**没有 user_id 列**（显著性是记忆级信号，不按租户分区）。
        #    两个错误都被 except 吞成 debug，res["salience"] 恒为 0。
        #
        #    实测后果远不止「留了脏数据」：生产 salience 1099 条里有 252 条
        #    是向量库中早已不存在的幽灵 id。幽灵被 decay_all 当正常记忆持续衰减，
        #    最终进入 evicted 列表，consolidator 再逐个调 /delete 去删
        #    「早就不存在的东西」——日志报「删除成功 25/25」，实际全是空转。
        try:
            from ducky.salience import delete_salience
            res["salience"] = delete_salience([memory_id])
        except Exception as e:
            logger.warning("salience.db 清理失败: %s", e)

        # 5. evolve_mem.db 清理（v19.4.1 修复：此前这一步从未真正执行过）
        #
        #    原实现 `from ducky.evolve_mem import get_evolve_conn` +
        #    `DELETE FROM evolve_snapshots` 有两个错误：该模块只有私有的
        #    `_get_evolve_conn`，且**不存在** evolve_snapshots 表
        #    （真实表是 evolve_queries / evolve_feedback / evolve_adjustments）。
        #    两个错误都被 except 吞成 debug 日志，res["evolve"] 一直如实报 0，
        #    于是删掉的记忆在检索自进化库里留下永久的反馈与调权孤儿。
        try:
            from ducky.evolve_mem import delete_evolve_by_memory_ids
            res["evolve"] = delete_evolve_by_memory_ids([memory_id])
        except Exception as e:
            logger.warning("evolve_mem.db 清理失败: %s", e)

        # 6. 📼 原文保真层清理（🔴P0-4 v19.4.1）：删除权必须兑现到逐字原文。
        #    以 content_hash 精确匹配（延续 v19.2.0 精确匹配铁律，杜绝 LIKE 误伤）。
        try:
            if _content_for_verbatim:
                from ducky.verbatim_vault import delete_verbatim_by_content
                res["verbatim"] = delete_verbatim_by_content(user_id, _content_for_verbatim)
            else:
                logger.debug("原文层清理跳过：未能定位该记忆正文 (%s)", memory_id)
        except Exception as ve:
            logger.debug("原文层清理跳过: %s", ve)

        wal.mark_status(wal_id, "committed")
        return {"status": "ok", "details": res}
    except Exception as exc:
        wal.mark_status(wal_id, "failed", error=str(exc))
        logger.error("级联删除记忆失败: %s", exc)
        raise


def cascade_delete_all(user_id: str, confirm: bool = False) -> Dict[str, Any]:
    """级联清空**指定租户**在所有存储中的数据，绝不留孤儿。

    🔴P0-3（v19.4.1）：所有子仓的删除一律 `WHERE user_id=?` 精确匹配。
        此前各仓都有 `if user_id == "default": DELETE FROM <table>`
        的无 WHERE 分支 —— 清 default 会连带清空所有其他租户的数据。
        `default` 是系统默认 user_id，误触概率极高。
        现在删 default 只删 default；跨租户全清必须由调用方逐租户循环，
        或走各模块的显式 purge 入口。
    """
    if not user_id or not user_id.strip():
        raise ValueError("user_id 必须显式指定")
    # v19.4.2：闸门原先只认字面量 "default"。这道闸的立意（见上方 docstring）
    # 是「default 是系统默认 user_id，误触概率极高」—— 它保护的是**大家会
    # 误触的那个租户**。部署方配了 AIDUMEM_DEFAULT_USER_ID 之后，误触面就
    # 换了人，而闸门还守在旧名字上：保护罩和被保护对象错位。
    # HTTP /delete_all 那层用的是 DEFAULT_USER_ID 常量、口径本来就对，
    # 所以线上无暴露；这里补齐内层的直接调用路径，两个名字都守，只加不减。
    if user_id in ("default", DEFAULT_USER_ID) and not confirm:
        raise ValueError(f"清空默认用户({user_id})全量记忆必须传递 confirm=True")
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
        "verbatim_deleted": 0,
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
        _tenant_memory_ids: list = []
        try:
            from ducky.text_fts import get_text_conn
            tconn = get_text_conn()
            # 先取出本租户的 memory_id 集合：evolve 各表无 user_id 列，
            # 只能靠这个集合精确清理（必须在 DELETE 之前取）。
            try:
                _tenant_memory_ids = [
                    r[0] for r in tconn.execute(
                        "SELECT id FROM memories WHERE user_id=?", (user_id,)
                    ).fetchall()
                ]
            except Exception as idexc:
                logger.debug("租户 memory_id 集合获取跳过: %s", idexc)
            # 🔴P0-3（v19.4.1）：一律精确按 user_id 删除。此前 default 分支
            # 走无 WHERE 全表删，会把其他租户数据一并灭掉 —— 而 default 正是
            # 系统默认 user_id，属于高频误触路径。全库清空另走显式入口。
            c_fts = tconn.execute("DELETE FROM memories WHERE user_id=?", (user_id,)).rowcount
            tconn.commit()
            tconn.close()
            res["fts_cleared"] = c_fts
        except Exception as e:
            logger.debug("FTS delete_all 跳过: %s", e)

        # 3. facts.db
        try:
            fconn = get_facts_conn()
            # 🔴P0-3（v19.4.1）：facts 侧同样取消 default 无 WHERE 全表删分支。
            try:
                fconn.execute(
                    """DELETE FROM memory_types 
                       WHERE memory_ref IN (SELECT CAST(id AS TEXT) FROM facts WHERE source=? OR agent_id=?)
                          OR memory_ref IN (SELECT 'fact:' || CAST(id AS TEXT) FROM facts WHERE source=? OR agent_id=?)
                          OR memory_ref IN (SELECT fact_key FROM facts WHERE (source=? OR agent_id=?) AND fact_key IS NOT NULL)""",
                    (user_id, user_id, user_id, user_id, user_id, user_id),
                )
            except Exception as e:
                logger.debug(f"cascade_delete_all: suppressed exception: {e}")
            c_facts = fconn.execute("DELETE FROM facts WHERE source=? OR agent_id=?", (user_id, user_id)).rowcount
            fconn.commit()
            fconn.close()
            res["facts_deleted"] = c_facts
        except Exception as e:
            logger.warning("facts delete_all 失败: %s", e)

        # 4. salience.db（v19.4.1 修复：同上，表名与列名双错，从未执行）
        #    salience 表无 user_id 列，故按「本租户已删除的 memory_id 集合」清理。
        try:
            from ducky.salience import delete_salience
            res["salience_deleted"] = delete_salience(_tenant_memory_ids)
        except Exception as e:
            logger.warning("salience delete_all 失败: %s", e)

        # 5. evolve_mem.db（v19.4.1 修复：同上，此前从未真正执行）
        #    evolve 各表没有 user_id 列 —— 它记录的是检索质量信号而非租户数据。
        #    因此按「本租户已删除的 memory_id 集合」来清，而不是按 user_id 过滤。
        #    memory_id 集合取自本次清空前的 FTS 索引（已按租户收窄）。
        try:
            from ducky.evolve_mem import delete_evolve_by_memory_ids
            res["evolve_deleted"] = delete_evolve_by_memory_ids(_tenant_memory_ids)
        except Exception as e:
            logger.warning("evolve delete_all 失败: %s", e)

        # 6. Verbatim Vault 原文保真层（v19.4.0 明镜工程 Phase 1）
        try:
            from ducky.verbatim_vault import cascade_delete_verbatim
            res["verbatim_deleted"] = cascade_delete_verbatim(user_id)
        except Exception as e:
            logger.debug("verbatim delete_all 跳过: %s", e)

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
