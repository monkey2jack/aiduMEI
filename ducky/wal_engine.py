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
from ducky.bank_contract import (
    DEFAULT_BANK_ID,
    legacy_fact_scope_predicate,
    make_scope,
    scoped_storage_key,
    vector_item_in_bank,
    vector_scope_filters,
)

logger = logging.getLogger("aiduMEM.wal")

WAL_DIR = os.path.join(DATA_DIR, "wal")
WAL_FILE = os.path.join(WAL_DIR, "mem_mutations.wal")

# mem0 exposes ``delete_all(user_id=...)`` but has no bank-aware variant.  A
# v20 caller must therefore enumerate the exact vector scope and delete the
# returned ids one by one.  Keep the ceiling deliberately high, while still
# bounded, so a corrupt/unbounded backend cannot turn a maintenance request
# into an unreviewable full-store scan.
VECTOR_ENUM_LIMIT = 100_000


def _vector_items(raw: Any) -> list[dict[str, Any]]:
    """Normalize the shapes returned by mem0/fakes to a list of dicts."""
    if isinstance(raw, dict):
        raw = raw.get("results", raw.get("memories", []))
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _vector_item_id(item: dict[str, Any]) -> str:
    """Extract a public mem0 id without accepting content as an id."""
    value = item.get("id") or item.get("memory_id")
    return str(value).strip() if value is not None else ""


def _list_scoped_vector_ids(mem: Any, scope: Any) -> tuple[list[str], bool]:
    """Return vector ids belonging to exactly ``scope``.

    ``get_all`` is intentionally used instead of ``delete_all``: the latter
    only understands user_id and would delete every bank for that user.  The
    bool says whether enumeration completed without an API/shape error.  A
    failed enumeration is never treated as an empty set (that distinction is
    what prevents a health/dependency hiccup from looking like a successful
    purge).
    """
    filters = vector_scope_filters(scope.user_id, scope.bank_id)
    try:
        try:
            raw = mem.get_all(filters=filters, top_k=VECTOR_ENUM_LIMIT)
        except TypeError:
            # A small number of test doubles and older wrappers called this
            # argument ``limit``.  Supporting that shape costs nothing and
            # keeps the safety rule identical.
            raw = mem.get_all(filters=filters, limit=VECTOR_ENUM_LIMIT)
        items = _vector_items(raw)
    except Exception as exc:
        logger.warning(
            "无法枚举向量作用域 user=%s bank=%s，拒绝调用无作用域 delete_all: %s",
            scope.user_id, scope.bank_id, exc,
        )
        return [], False

    ids: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not vector_item_in_bank(item, scope.bank_id):
            continue
        mid = _vector_item_id(item)
        if mid and mid not in seen:
            seen.add(mid)
            ids.append(mid)
    if len(items) >= VECTOR_ENUM_LIMIT:
        # mem0 has no offset/cursor on get_all.  We cannot prove that the
        # result is complete at the ceiling, so report incomplete rather than
        # silently claiming a full purge.
        logger.error(
            "向量作用域枚举达到上限 %d user=%s bank=%s；向量清空结果不完整",
            VECTOR_ENUM_LIMIT, scope.user_id, scope.bank_id,
        )
        return ids, False
    return ids, True


def _delete_scoped_vectors(mem: Any, scope: Any) -> tuple[int, bool, list[str]]:
    """Enumerate and individually delete vectors in one bank."""
    ids, complete = _list_scoped_vector_ids(mem, scope)
    if not complete:
        return 0, False, ids
    deleted = 0
    ok = True
    for mid in ids:
        try:
            mem.delete(mid)
            deleted += 1
        except Exception as exc:
            ok = False
            logger.warning(
                "向量单条删除失败 user=%s bank=%s memory_id=%s: %s",
                scope.user_id, scope.bank_id, mid, exc,
            )
    return deleted, ok, ids


@dataclass
class WALEntry:
    wal_id: str = field(default_factory=lambda: f"wal-{uuid.uuid4().hex[:12]}")
    timestamp: float = field(default_factory=time.time)
    user_id: str = "default"
    bank_id: str = DEFAULT_BANK_ID
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

def cascade_delete_memory(
    memory_id: str,
    user_id: str = DEFAULT_USER_ID,
    bank_id: str = DEFAULT_BANK_ID,
) -> Dict[str, Any]:
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
    scope = make_scope(user_id, bank_id)
    user_id = scope.user_id
    bank_id = scope.bank_id
    wal = WALEngine.get_instance()
    wal_id = wal.append(WALEntry(
        user_id=user_id,
        operation="delete",
        payload={"memory_id": memory_id, "user_id": user_id, "bank_id": bank_id},
    ))

    res = {
        "memory_id": memory_id,
        "user_id": user_id,
        "bank_id": bank_id,
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
                    memory_id,
                    user_id=user_id,
                    bank_id=bank_id,
                    reason="cascade_delete_verbatim",
                    actor="wal_engine",
                )
            except Exception as te:
                logger.debug("tombstone 快照跳过: %s", te)
            try:
                from ducky.verbatim_vault import delete_verbatim_by_id
                res["verbatim"] = delete_verbatim_by_id(user_id, memory_id, bank_id=bank_id)
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
            _content_for_verbatim = _capture_fts_content(memory_id, user_id, bank_id) or (
                (_capture_facts_row(memory_id, user_id, bank_id) or {}).get("fact_value", "")
            )
        except Exception as ce:
            logger.debug("原文定位内容抓取跳过: %s", ce)

        # 0. 🪦 tombstone 快照（v19.4.0 Mímir 借鉴 B3）：物理删除前先把全文+理由留痕，
        #    误删可一键恢复。快照失败只记日志，绝不阻断删除主链路。
        try:
            from ducky.tombstone import snapshot_before_delete
            res["tombstone_id"] = snapshot_before_delete(
                memory_id,
                user_id=user_id,
                bank_id=bank_id,
                reason="cascade_delete",
                actor="wal_engine",
            )
        except Exception as te:
            logger.debug("tombstone 快照跳过: %s", te)

        # 1. mem0 向量删除。mem0.delete(memory_id) 本身没有 user/bank
        # 参数，直接调用会让拿到另一域 id 的请求越过 v20 作用域。因此先
        # 在同一作用域枚举并确认 id，再执行单条删除；枚举失败时宁可留
        # 向量孤儿，绝不把一个未验证的 id 交给全局删除原语。
        try:
            from ducky.mem0_runtime import get_memory
            mem = get_memory()
            scoped_ids, enumeration_ok = _list_scoped_vector_ids(mem, scope)
            if enumeration_ok and str(memory_id) in scoped_ids:
                mem.delete(memory_id)
                res["mem0_vector"] = True
            elif enumeration_ok:
                logger.info(
                    "向量 id 不属于请求作用域，跳过删除 user=%s bank=%s id=%s",
                    user_id, bank_id, memory_id,
                )
            else:
                logger.warning(
                    "向量归属无法确认，跳过删除 user=%s bank=%s id=%s",
                    user_id, bank_id, memory_id,
                )
        except Exception as e:
            logger.debug("mem0.delete 跳过或失败: %s", e)

        # 2. FTS5 索引剔除（带 user_id 作用域）
        try:
            from ducky.text_fts import get_text_conn
            tconn = get_text_conn()
            storage_id = scoped_storage_key(memory_id, scope)
            tconn.execute(
                "DELETE FROM memories WHERE id IN (?, ?) AND user_id=? AND bank_id=?",
                (storage_id, f"fact:{storage_id}", user_id, bank_id),
            )
            tconn.commit()
            tconn.close()
            res["fts"] = True
        except Exception as e:
            logger.debug("FTS unindex 跳过: %s", e)

        # 3. facts.db 清理（🔴P0-1 严格归属校验 + 🔴P0-2 精确匹配，彻底消除 LIKE 误删）
        try:
            conn = get_facts_conn()
            exact_keys = (memory_id, f"fact:{memory_id}", f"raw:{memory_id}")
            # 本地自算，**不复用第 2 步里的同名变量**：那一个定义在 FTS 的
            # try 内部，一旦 get_text_conn() 抛错就根本没被赋值，这里再引用
            # 就是 NameError —— 而它会被本块的 except 吞掉，表现为
            # 「facts 清理整段被跳过」，且日志只有一行 debug。
            storage_id = scoped_storage_key(memory_id, scope)
            # 🔴v20：作用域必须进入删除条件本身。
            #
            # 旧写法分两支，两支都漏了 bank_id，且 default 支**一个作用域
            # 条件都没有**：
            #
            #     if user_id == "default":
            #         DELETE FROM facts WHERE id=? OR fact_key=? ...   # 全库
            #     else:
            #         ... AND (source=? OR agent_id=?)                 # 无 bank
            #
            # 后果分两级。默认用户删 id=X，会把**所有租户、所有域**里叫 X
            # 的行一起删掉；具名租户删 X，会把自己 work 域和 home 域的 X
            # 一起删掉 —— 域隔离恰恰是 v20 的立身之本，却在唯一不可逆的
            # 那条路径上失效。而 `res["facts"] = c1` 只回报一个 rowcount，
            # 多删了照样是个好看的数字，不抛错、不告警：静默数据丢失。
            #
            # 删除路径的取舍与读取相反：少删可以重试，多删无法挽回。
            # 因此这里一律走**严格作用域**，渠道标记只对「确实没有正规主人」
            # 的老行在默认域内回落，且回落绝不越过已有归属。
            if bank_id == DEFAULT_BANK_ID:
                # 「未认领」= user_id 为 NULL/空白，或仍是占位符 default。
                # 迁移会把存量行统一回填成 default，若只认 NULL/空白，
                # v19 里靠 source/agent_id 记归属的租户将连自己的记忆都
                # 删不掉（删除返回 ok、rowcount=0，又是一次静默失败）。
                own_sql = (
                    "bank_id=? AND (user_id=? OR "
                    "((user_id IS NULL OR TRIM(user_id)='' OR user_id=?) "
                    "AND (source=? OR agent_id=?)))"
                )
                own_params = [bank_id, user_id, DEFAULT_USER_ID, user_id, user_id]
            else:
                own_sql = "user_id=? AND bank_id=?"
                own_params = [user_id, bank_id]

            c1 = conn.execute(
                f"""DELETE FROM facts
                   WHERE (id=? OR fact_key=? OR fact_key=? OR fact_key=?)
                     AND ({own_sql})""",
                (memory_id, exact_keys[0], exact_keys[1], exact_keys[2], *own_params),
            ).rowcount
            try:
                # memory_types.memory_ref 存的是**带作用域的**键（见
                # memory_types._storage_ref），memory_ref_raw 才是对外裸 id。
                # 旧写法拿裸 id 去比 memory_ref，在具名域里永远比不中 ——
                # 类型行会变成删不掉的孤儿；而它又没有作用域条件，
                # 在默认域里反而跨租户误删。两头都错，方向还相反。
                conn.execute(
                    "DELETE FROM memory_types "
                    "WHERE (memory_ref IN (?, ?) OR memory_ref_raw IN (?, ?) "
                    "OR (ref_alt IS NOT NULL AND ref_alt IN (?, ?))) "
                    "AND user_id=? AND bank_id=?",
                    (
                        storage_id, f"fact:{storage_id}",
                        memory_id, f"fact:{memory_id}",
                        storage_id, memory_id,
                        user_id, bank_id,
                    ),
                )
            except Exception as e:
                logger.debug(f"cascade_delete_memory: suppressed exception: {e}")
            # 📒 事件账本（v19.4.0 Mímir 借鉴 B5）：与删除同事务留痕，同生共死
            try:
                from ducky.event_ledger import record_event
                record_event(conn, actor=user_id or "system", action="delete",
                             target_id=memory_id, reason="cascade_delete_memory",
                             user_id=user_id, bank_id=bank_id)
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
                res["verbatim"] = delete_verbatim_by_content(
                    user_id, _content_for_verbatim, bank_id=bank_id
                )
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


def cascade_delete_all(
    user_id: str,
    confirm: bool = False,
    bank_id: str = DEFAULT_BANK_ID,
) -> Dict[str, Any]:
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
    scope = make_scope(user_id, bank_id)
    user_id = scope.user_id
    bank_id = scope.bank_id
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
        payload={"user_id": user_id, "bank_id": bank_id},
    ))

    res = {
        "user_id": user_id,
        "bank_id": bank_id,
        "mem0_deleted": False,
        "fts_cleared": 0,
        "facts_deleted": 0,
        "salience_deleted": 0,
        "evolve_deleted": 0,
        "verbatim_deleted": 0,
    }

    try:
        # ``evolve`` and the salience ledger do not historically carry a
        # tenant column, so collect every exact identifier *before* deleting
        # anything.  Never use a user-only query for this set: the same
        # memory id is valid in two banks.
        _tenant_memory_ids: set[str] = set()
        _fact_ids: set[str] = set()
        _fact_keys: set[str] = set()

        # 1. mem0 / Qdrant.  There is no bank-aware delete_all in mem0; the
        # only safe operation is scoped enumeration followed by single-id
        # deletes.  In particular, do not reintroduce ``mem.delete_all`` here
        # as a convenience fallback.
        try:
            from ducky.mem0_runtime import get_memory

            mem = get_memory()
            vector_deleted, vector_ok, vector_ids = _delete_scoped_vectors(mem, scope)
            _tenant_memory_ids.update(vector_ids)
            res["mem0_deleted"] = bool(vector_ok)
            res["mem0_vector_count"] = vector_deleted
            res["vector_enumeration_complete"] = bool(vector_ok)
        except Exception as e:
            logger.warning("mem0 作用域清理失败（未调用无作用域 delete_all）: %s", e)
            res["vector_enumeration_complete"] = False

        # 2. FTS5.  Both collection and DELETE repeat the full canonical
        # (user_id, bank_id) predicate.  The collection contains the internal
        # storage id (named banks are prefixed); vector ids collected above are
        # additionally retained for the unscoped auxiliary ledgers.
        try:
            from ducky.text_fts import get_text_conn

            tconn = get_text_conn()
            try:
                rows = tconn.execute(
                    "SELECT id FROM memories WHERE user_id=? AND bank_id=?",
                    (scope.user_id, scope.bank_id),
                ).fetchall()
                _tenant_memory_ids.update(str(r[0]) for r in rows if r[0])
                c_fts = tconn.execute(
                    "DELETE FROM memories WHERE user_id=? AND bank_id=?",
                    (scope.user_id, scope.bank_id),
                ).rowcount or 0
                tconn.commit()
                res["fts_cleared"] = c_fts
            finally:
                tconn.close()
        except Exception as e:
            logger.warning("FTS 作用域清理失败: %s", e)

        # 3. facts.db.  The default bank uses the additive-transition
        # predicate: a row with canonical user_id=default may still be an old
        # row whose source/agent marker identifies a named tenant.  That
        # fallback is constrained to rows with no real canonical owner and
        # never applies to a named bank.  Crucially, source/agent_id are not
        # used as a free-standing OR against already-owned rows.
        try:
            fconn = get_facts_conn()
            try:
                from ducky.bank_contract import ensure_memory_banks_schema
                ensure_memory_banks_schema(fconn)
                fact_scope_sql, fact_scope_params = legacy_fact_scope_predicate(scope)
                fact_rows = fconn.execute(
                    "SELECT id, fact_key FROM facts WHERE 1=1" + fact_scope_sql,
                    fact_scope_params,
                ).fetchall()
                for row in fact_rows:
                    if row[0] is not None:
                        _fact_ids.add(str(row[0]))
                    if row[1]:
                        _fact_keys.add(str(row[1]))

                # Build memory_types references from the exact fact ids.  A
                # fact id is globally unique; fact_key is not, so key-only
                # references are handled with the canonical scope below.
                fact_ref_values: set[str] = set()
                for fid in _fact_ids:
                    fact_ref_values.update({fid, f"fact:{fid}", f"raw:{fid}"})

                if fact_ref_values:
                    try:
                        from ducky.memory_types import ensure_memory_types_schema
                        ensure_memory_types_schema()
                        ref_ph = ",".join("?" for _ in fact_ref_values)
                        # A legacy named tenant in the default bank has its
                        # type row in the compatibility default scope.  It is
                        # safe to include that scope here because the
                        # reference is a globally unique fact id.
                        allowed = (
                            "((user_id=? AND bank_id=?) OR "
                            "(user_id=? AND bank_id=?))"
                        )
                        type_params = [
                            *fact_ref_values,
                            scope.user_id, scope.bank_id,
                            DEFAULT_USER_ID, DEFAULT_BANK_ID,
                        ]
                        fconn.execute(
                            "DELETE FROM memory_types WHERE "
                            f"(memory_ref IN ({ref_ph}) OR memory_ref_raw IN ({ref_ph}) "
                            f"OR (ref_alt IS NOT NULL AND ref_alt IN ({ref_ph}))) "
                            "AND " + allowed,
                            type_params,
                        )
                    except Exception as type_exc:
                        logger.debug("memory_types facts refs 清理跳过: %s", type_exc)

                c_facts = fconn.execute(
                    "DELETE FROM facts WHERE 1=1" + fact_scope_sql,
                    fact_scope_params,
                ).rowcount or 0
                fconn.commit()
                res["facts_deleted"] = c_facts
            finally:
                fconn.close()
        except Exception as e:
            logger.warning("facts 作用域清理失败: %s", e)

        # Fact keys are useful to old auxiliary records, but are not allowed
        # to widen a delete.  Only pass exact ids and scoped FTS/vector ids to
        # the unscoped ledgers; retain keys for diagnostics and future scoped
        # migrations rather than treating them as ownership proof.
        _tenant_memory_ids.update(_fact_ids)
        _tenant_memory_ids.update(f"fact:{fid}" for fid in _fact_ids)

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
            res["verbatim_deleted"] = cascade_delete_verbatim(user_id, bank_id=bank_id)
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
            # v20.0：重放必须恢复完整作用域。payload 里的 bank_id 是权威值
            # （v19 旧条目没有该键，回落到条目字段再回落 default）。
            replay_bank = ent.payload.get("bank_id") or ent.bank_id or DEFAULT_BANK_ID
            if ent.operation == "delete":
                mid = ent.payload.get("memory_id")
                if mid:
                    cascade_delete_memory(mid, user_id=ent.user_id, bank_id=replay_bank)
                    report["recovered"] += 1
            elif ent.operation == "delete_all":
                # WAL 条目的存在即证明原调用已通过 confirm 闸门；
                # 重放时补 confirm=True，否则 default 用户的恢复会永远失败。
                cascade_delete_all(user_id=ent.user_id, confirm=True, bank_id=replay_bank)
                report["recovered"] += 1
            else:
                # 记录为无法自动决议的写入，标记 failed 供运维审计
                wal.mark_status(ent.wal_id, "failed", error="Unresolved startup transaction")
                report["failed"] += 1
        except Exception as err:
            logger.error("Reconcile 恢复失败 wal_id=%s: %s", ent.wal_id, err)
            report["failed"] += 1

    return report
