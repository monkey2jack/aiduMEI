"""ducky.wal_engine — 应用层 WAL (Write-Ahead Logging) 预写日志与多仓对账引擎

职责：
1. 写操作前置 fsync 追加 WAL 日志，防进程崩溃产生孤儿数据；
2. 服务启动自愈对账 (Reconcile)，检测并恢复未完成写入或清理孤儿；
3. 多仓原子删除协调器（清理面以 DELETE_CHAIN_MATRIX 为唯一真相源 ——
   v20.1 整改轮之前这行 docstring 宣称清 workspace 而代码从未清过，
   文档与代码的缝隙就是外审 z P1-01 的案发现场；矩阵 + 元测试让这类
   缝隙从此结构性不可能：新账本出现而矩阵未裁决 → 测试红）。
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
            # 🔴v20.0：作用域谓词只许有一处实现。
            #
            # 这里曾把 legacy_fact_scope_predicate 的 SQL 连注释一起**手抄
            # 一遍**，于是同一份契约有了两个副本。本文件第 24 行明明已经
            # import 了那个函数、cascade_delete_all 也在调它，唯独这条单条
            # 删除路径走的是复制品。后果是可以预料的：占位符口径在共享函数
            # 里放宽之后，手抄件没跟上，单条删除继续对存量行失明 ——
            # 删除返回 ok、rowcount=0，又是一次静默失败。
            #
            # 契约抄两遍，就一定会改一遍漏一遍。改成调用，副本消失。
            scope_sql, own_params = legacy_fact_scope_predicate(scope)
            c1 = conn.execute(
                f"""DELETE FROM facts
                   WHERE (id=? OR fact_key=? OR fact_key=? OR fact_key=?)
                     AND (1=1{scope_sql})""",
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

        # 7. Workspace 单条驱逐（v20.1 整改轮 R-01 · 外审 z P1-01）。
        #    只清全域不清单条的话，/delete 之后同一条还能从缓存里搜出来。
        try:
            from ducky.memory_workspace import ws_evict
            res["workspace_evicted"] = bool(ws_evict(user_id, memory_id, bank_id=bank_id))
        except Exception as we:
            logger.warning("workspace 单条驱逐失败: %s", we)

        # 8. 本地向量单删（v20.2 自动挡 WP-F）。双索引同源 id ——
        #    云侧删了本地不删，降挡时已删内容会从备胎索引复活。
        try:
            from ducky.dual_index import delete_local
            res["local_vector_deleted"] = delete_local([memory_id]) > 0
        except Exception as e:
            logger.debug("本地向量单删跳过: %s", e)

        # 8b. verbatim 本地点（v20.2.1 外审 R3）：这类点的 id 由 (原文, 域)
        #     派生、不与 memory_id 同源，§8 的钥匙够不着 —— 搭车 §0a 抓到
        #     的正文重演派生（dual_index.verbatim_local_pid 同一公式），
        #     精确删除。覆盖精度与 §6 原文层同级：正文与写入原文逐字一致
        #     才命中（保真写入/确定性通路全中）；蒸馏改写场景两条腿同受限，
        #     属 P0-4 已审计语义，delete_all 的按域谓词删仍是全量兜底。
        try:
            if _content_for_verbatim:
                from ducky.dual_index import delete_local as _dl, verbatim_local_pid
                _vpid = verbatim_local_pid(user_id, bank_id, _content_for_verbatim)
                res["verbatim_local_vector_deleted"] = _dl([_vpid]) > 0
        except Exception as e:
            logger.debug("verbatim 本地点单删跳过: %s", e)

        wal.mark_status(wal_id, "committed")
        return {"status": "ok", "details": res}
    except Exception as exc:
        wal.mark_status(wal_id, "failed", error=str(exc))
        logger.error("级联删除记忆失败: %s", exc)
        raise


# ── 删除链覆盖矩阵（v20.1 整改轮 R-01，外审 z 的方法论）────────────────
# 每一张会存租户内容的账本，必须在这里有一条**显式裁决**：
#   ("clean", …)  —— cascade_delete_all 按作用域清理；
#   ("exempt", …) —— 明确豁免并写明理由，绝不沉默。
# 配套元测试（tests/test_v20_1_delete_chain_closure.py）会实建全部 schema
# 后枚举 facts.db 的 sqlite_master，任何新表未在矩阵裁决即红 ——
# 「漏一张账本不会红」正是本轮外审揪出两张漏网表（workspace/core_memory）
# 的方法论根因。外部存储（向量库/FTS 库/工作区库等）按存储名列于下半段。
DELETE_CHAIN_MATRIX: Dict[str, tuple] = {
    # ── facts.db 内的表 ──
    "facts":            ("clean",  "作用域谓词删除（§3）"),
    "memory_types":     ("clean",  "fact 引用 + 可见租户契约双路删除（§3/§3b）"),
    "core_memory":      ("clean",  "可见租户契约删除（§8，v20.1 整改轮补齐；此前正本残留且被 inject_context 持续注入 —— 外审 w P0 / 自报 4.1）"),
    "refined_memories": ("clean",  "user 轴删除（§9，v20.1 整改轮补齐）。该表无 bank 列（v20 已登记限制 9c）：清任一 bank 会清掉该租户全部整合账本，宁可域内多删不留隐私残留，已文档化"),
    "checkpoints":      ("exempt", "会话轴（session_id）单租户遗产子系统，无租户列，读写 API 均无租户轴；随 MAX_SESSIONS=5 自然滚动 + /api/checkpoint/cleanup 管理端清理口。多租户化另立项（外审 w P0 的第三张表 —— 豁免是显式裁决，不是沉默）"),
    "memory_banks":     ("exempt", "bank 注册表：行是「域存在过」的元数据不含记忆内容；删除域数据不注销域名，避免删除后同名域立刻复用造成审计断代"),
    "entities":         ("exempt", "实体规范化词典（跨租户共享的无内容索引结构）"),
    "fact_entities":    ("clean",  "随 facts 行经外键/引用清理（facts 删除后无悬挂引用即视为达成）"),
    "fact_events":      ("exempt", "事件账本=审计履历：删除动作本身必须留痕，清账本等于销毁审计线索"),
    "memory_states":    ("exempt", "生命周期状态账本（同上，审计履历）"),
    "memory_events":    ("exempt", "B5 事件账本（哈希+动作+归因，无记忆正文）：审计履历，删除动作本身必须留痕"),
    "knowledge_evolution": ("exempt", "知识演化关系账本（source_id/target_id/relation，无记忆正文）：id 级引用随对应记忆删除自然失效；本表首个裁决由矩阵元守卫逼出 —— 守卫上岗第一天就抓到一张没人数过的表"),
    "tombstones":       ("clean",  "墓碑带 content_snapshot 全文快照（§10，v20.1 整改轮补齐）——留着它，delete_all 的擦除承诺就是空话；代价是全量清空后不可再 restore，这正是「清空一切」的语义"),
    "candidate_facts":  ("clean",  "治理候选队列含被拒/待审全文（§11，v20.1 整改轮补齐），按 scope_user_id/bank_id 精确清理"),
    "refine_wal":       ("exempt", "refine 操作 WAL（若存在）：操作流水，随 WAL 引擎自身生命周期管理"),
    "wal_entries":      ("exempt", "WAL 引擎自身账本：删除操作的执行凭证，清掉等于销毁「删过」的证据"),
    # ── facts.db 之外的存储 ──
    "store:qdrant":     ("clean",  "作用域枚举 + 复筛逐点删（§1，_delete_scoped_vectors）"),
    "store:text_fts":   ("clean",  "(user_id, bank_id) 谓词删除（§2）；verbatim_fts 随 §6 清理"),
    "store:salience":   ("clean",  "按本租户已删 memory_id 集合清理（§4，表无租户列）"),
    "store:evolve":     ("clean",  "按本租户已删 memory_id 集合清理（§5，检索质量信号）"),
    "store:verbatim":   ("clean",  "cascade_delete_verbatim（§6）"),
    "store:workspace":  ("clean",  "ws_clear 内存+SQLite 双清（§7，v20.1 整改轮补齐 —— 外审 z P1-01：此前已删内容以 found/workspace_hit 复活）"),
    "store:qdrant_local": ("clean", "v20.2 自动挡本地向量库（mem0_local，512 维）：(user_id, bank_id) 谓词删除（§14）；单删按同源 id 精确删"),
    "pending_embeddings": ("clean", "v20.2 自动挡欠账账本：载荷含用户原文，(user_id, bank_id) 谓词删除（§15）——欠着的债也是债，删除承诺覆盖它"),
    # ── 显式申报的已知残留（申报不是沉默；沉默才是本矩阵要消灭的东西）──
    "observations":       ("clean",  "R-18(v20.1.1)：user 轴谓词删除（§12）——表无 bank 列，user 轴是它拥有的全部作用域表达力；v7 存量空 user_id 行不属于任何租户，不动"),
    "scenes":             ("clean",  "R-18(v20.1.1)：(user_id, bank_id) 谓词删除（§13）"),
    "store:persona":      ("exempt", "人格库（独立 persona.db，不在 facts.db）无租户列 —— persona_key 是人格轴，与 (user_id, bank_id) 租户模型正交，作用域删除语义不成立；若未来 persona 归属租户，须先补列迁移再纳入（R-18 复核改判 v20.1.1：原裁决『接口未做』经侦察修正为『语义不成立』）"),
}


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
        "memory_types_deleted": 0,
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

        # 3b. memory_types 也可以由 infer=False 直接写入，未必有对应 facts
        # 行（生产冒烟实测：/add 成功、向量已删，类型账本却留下孤儿行）。
        # 不能只靠上面的 fact_ref_values 清理；按同一份可见租户契约精确删除，
        # 默认身份改名时允许 legacy placeholder，但绝不碰具名租户。
        try:
            from ducky.bank_contract import visible_user_clause
            from ducky.memory_types import ensure_memory_types_schema

            ensure_memory_types_schema()
            tconn = get_facts_conn()
            try:
                owner_sql, owner_params = visible_user_clause(scope.user_id)
                cur = tconn.execute(
                    "DELETE FROM memory_types WHERE " + owner_sql + " AND bank_id=?",
                    (*owner_params, scope.bank_id),
                )
                tconn.commit()
                res["memory_types_deleted"] = int(cur.rowcount or 0)
            finally:
                tconn.close()
        except Exception as type_scope_exc:
            logger.warning("memory_types 作用域清理失败: %s", type_scope_exc)

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

        # 7. Workspace 工作区缓存（v20.1 整改轮 R-01 · 外审 z P1-01）。
        #    工作区存记忆正文副本且被 /search **优先**命中 —— 不清它，
        #    已删内容会带着 found/workspace_hit 判语复活，重启后照样在
        #    （SQLite 落盘 + 启动重载）。内存与库由 ws_clear 一并清。
        try:
            from ducky.memory_workspace import ws_clear
            res["workspace_cleared"] = int(ws_clear(scope.user_id, bank_id=scope.bank_id) or 0)
        except Exception as e:
            logger.warning("workspace delete_all 清理失败: %s", e)

        # 8. CoreMemory 正本（v20.1 整改轮 R-01 · 外审 w P0 / 自报 4.1）。
        #    此前三副本里只有索引（FTS/向量）在删除链射程内，正本表残留，
        #    inject_context 从正本直读 ——「清空全部记忆」后画像仍持续进
        #    每一次对话上下文。谓词用与 memory_types §3b 相同的可见租户
        #    契约：改名默认身份连它搁浅在 'default' 上的存量行一并清掉，
        #    否则读侧放宽会让残留行继续被注入（w 的注入复活链①）。
        try:
            from ducky.bank_contract import visible_user_clause as _vuc
            cconn = get_facts_conn()
            try:
                owner_sql, owner_params = _vuc(scope.user_id)
                cur = cconn.execute(
                    "DELETE FROM core_memory WHERE " + owner_sql + " AND bank_id=?",
                    (*owner_params, scope.bank_id),
                )
                cconn.commit()
                res["core_memory_deleted"] = int(cur.rowcount or 0)
            finally:
                cconn.close()
        except Exception as e:
            logger.warning("core_memory delete_all 清理失败: %s", e)

        # 9. refined_memories 整合账本（v20.1 整改轮 R-01 · 外审 w P0）。
        #    表无 bank 列（v20 登记限制 9c），按 user 轴清理：清任一 bank
        #    会清掉该租户全部整合账本 —— 宁可域内多删不留隐私残留，
        #    该取舍已写入 DELETE_CHAIN_MATRIX 与文档。facts 表里的
        #    refined:N 摘要行由 §3 的 facts 作用域删除覆盖。
        try:
            from ducky.bank_contract import visible_user_clause as _vuc
            rconn = get_facts_conn()
            try:
                owner_sql, owner_params = _vuc(scope.user_id)
                cur = rconn.execute(
                    "DELETE FROM refined_memories WHERE " + owner_sql,
                    owner_params,
                )
                rconn.commit()
                res["refined_deleted"] = int(cur.rowcount or 0)
            finally:
                rconn.close()
        except Exception as e:
            logger.warning("refined_memories delete_all 清理失败: %s", e)

        # 10. 墓碑（v20.1 整改轮 R-01 · 覆盖矩阵裁决）。墓碑行带
        #     content_snapshot **全文快照** —— 不清它，被删内容以「可恢复
        #     备份」的名义永久留存，擦除承诺落空。代价说在明面上：全量
        #     清空后该域不可再 tombstone/restore，这正是「清空一切」的语义。
        try:
            tbconn = get_facts_conn()
            try:
                cur = tbconn.execute(
                    "DELETE FROM tombstones WHERE user_id=? AND bank_id=?",
                    (scope.user_id, scope.bank_id),
                )
                tbconn.commit()
                res["tombstones_deleted"] = int(cur.rowcount or 0)
            finally:
                tbconn.close()
        except Exception as e:
            logger.warning("tombstones delete_all 清理失败: %s", e)

        # 11. 治理候选队列（v20.1 整改轮 R-01 · 覆盖矩阵裁决）。候选行含
        #     被拒/待审的**全文**，按 v20 治理域戳精确清理。
        try:
            gconn = get_facts_conn()
            try:
                cur = gconn.execute(
                    "DELETE FROM candidate_facts WHERE scope_user_id=? AND bank_id=?",
                    (scope.user_id, scope.bank_id),
                )
                gconn.commit()
                res["governance_candidates_deleted"] = int(cur.rowcount or 0)
            finally:
                gconn.close()
        except Exception as e:
            logger.warning("candidate_facts delete_all 清理失败: %s", e)

        # 12. 观察库（v20.1.1 R-18 · 两轮外审共同挂账）。聚合观察含租户
        #     内容全文。表只有 user 轴（无 bank 列——老账本），user 轴就是
        #     它拥有的全部作用域表达力；v7 存量空 user_id 行不属于任何
        #     租户，不动。表未建过 = 该库从未启用，跳过不告警。
        try:
            oconn = get_facts_conn()
            try:
                if oconn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='observations'"
                ).fetchone():
                    ocols = {r[1] for r in oconn.execute("PRAGMA table_info(observations)")}
                    if "user_id" in ocols:
                        cur = oconn.execute(
                            "DELETE FROM observations WHERE user_id=?", (scope.user_id,))
                        oconn.commit()
                        res["observations_deleted"] = int(cur.rowcount or 0)
            finally:
                oconn.close()
        except Exception as e:
            logger.warning("observations delete_all 清理失败: %s", e)

        # 13. 场景库（v20.1.1 R-18）。v20 起自带全轴列，谓词直删。
        try:
            sconn = get_facts_conn()
            try:
                if sconn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scenes'"
                ).fetchone():
                    cur = sconn.execute(
                        "DELETE FROM scenes WHERE user_id=? AND bank_id=?",
                        (scope.user_id, scope.bank_id))
                    sconn.commit()
                    res["scenes_deleted"] = int(cur.rowcount or 0)
            finally:
                sconn.close()
        except Exception as e:
            logger.warning("scenes delete_all 清理失败: %s", e)

        # 14. 本地向量库（v20.2 自动挡 WP-F）。lite 挡语料与蒸馏本地副本
        #     都住这里，域谓词删除——已删内容绝不许从备胎索引复活。
        try:
            from ducky.dual_index import delete_local_by_scope
            res["local_vectors_deleted"] = delete_local_by_scope(
                scope.user_id, bank_id=scope.bank_id)
        except Exception as e:
            logger.warning("本地向量 delete_all 清理失败: %s", e)

        # 15. 欠账账本（v20.2 自动挡 WP-F）。lite 挡期间的原始请求载荷
        #     在这里排队等重放——不清它，已删租户的原文会在升挡重放时
        #     以「补蒸馏」的名义复活（与 w 的回填复活链同形）。
        try:
            from ducky.dual_index import delete_pending_by_scope
            res["pending_embeddings_deleted"] = delete_pending_by_scope(
                scope.user_id, bank_id=scope.bank_id)
        except Exception as e:
            logger.warning("欠账账本 delete_all 清理失败: %s", e)

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
        return _finish_reconcile(report)

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

    return _finish_reconcile(report)


def _finish_reconcile(report: Dict[str, Any]) -> Dict[str, Any]:
    """启动对账收尾（两条返回路径共用）。

    v20.2.1（外审 R2）：欠账重放此前只挂在「升挡事件」上，而重启把挡位
    重置回 closed —— 升挡事件重启后永不再来，lite 期欠账成永久赖账。
    这里兜底扫一遍（守护线程不阻塞启动；零欠账不起线程；claiming 抢占
    防与升挡重放并发）。顺带打一条挡位/限流参数自检日志（外审 R1 配套：
    回退语义下坏配置不再炸，就必须在启动面**出声**可查）。"""
    try:
        from ducky.dual_index import spawn_replay_daemon
        report["pending_replay_spawned"] = spawn_replay_daemon(
            source="reconcile_startup")
    except Exception as exc:
        logger.warning("启动欠账重放挂起失败（留账）: %s", exc)
        report["pending_replay_spawned"] = False
    try:
        from ducky.gear import gear_status
        from ducky.rate_guard import add_rate_limit, delete_all_rate_limit, rate_config_errors
        gs = gear_status()
        logger.info(
            "⚙️ 启动参数自检：gear trip=%s recover=%s cooldown=%ss config_errors=%s | "
            "rate add=%s/min delete_all=%s/min config_errors=%s",
            gs["trip_threshold"], gs["recover_threshold"], gs["cooldown_sec"],
            gs["config_errors"], add_rate_limit(), delete_all_rate_limit(),
            rate_config_errors() or None)
    except Exception as exc:
        logger.debug("启动参数自检日志跳过: %s", exc)
    return report
