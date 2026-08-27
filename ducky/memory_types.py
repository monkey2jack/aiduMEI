"""
ducky.memory_types — 记忆类型分离（v19.0 · P1-1 · Hindsight 四网络借鉴）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
把混在同一个「memory」池里的内容按认知类型分开管理。不推翻现有
facts / mem0 存储，而是在上面加一层显式的类型标签与查询视图。

六种类型（对调研报告 P1-1 的 Hindsight 四网络做了 aiduMEI 化落地）：
    FACTS        客观事实（世界记忆 𝒲）
    PREFERENCES  偏好 + 置信度（观点记忆 𝒪）
    EXPERIENCES  第一人称经历（经验记忆 ℰ）
    OBSERVATIONS 中性观察摘要（观察记忆 𝒮）
    REFLECTIONS  反思洞察（P0-3 产物）
    DECISIONS    关键决策与约定（决策账本）

设计原则
    · 向后兼容：不强制迁移，所有老数据默认归入 FACTS，旧 API 照常工作
    · 一处真源：memory_types 表是分类账本，可以随时重建/重算
    · 渐进启用：默认关闭 LLM 分类（AIDUMEM_TYPE_CLASSIFY_ENABLED=false），
      开启后写入 /add 或 /facts/add 时用 LLM 判型并落账本
    · 零依赖降级：LLM 不可用时用确定性规则分类，绝不阻断主链路
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from ducky.utils import DEFAULT_USER_ID, get_facts_conn
from ducky.bank_contract import (
    DEFAULT_BANK_ID,
    LEGACY_PLACEHOLDER_USER_ID,
    ensure_bank_registered,
    ensure_memory_banks_schema,
    make_scope,
    scoped_storage_key,
    raw_storage_key,
    visible_user_clause,
)

logger = logging.getLogger("aiduMEM.memory_types")

# 类型 → 中文标签 + 四网络角色
TYPE_LABELS = {
    "FACTS": "客观事实",
    "PREFERENCES": "偏好",
    "EXPERIENCES": "经验",
    "OBSERVATIONS": "观察",
    "REFLECTIONS": "反思",
    "DECISIONS": "决策",
}

VALID_TYPES = frozenset(TYPE_LABELS)

_checked = False


def ensure_memory_types_schema() -> None:
    """幂等建 memory_types 账本表。"""
    global _checked
    if _checked:
        return
    conn = get_facts_conn()
    try:
        # The bank registry/columns are additive and shared by all storage
        # layers.  Calling it here also makes standalone imports (without the
        # API server's schema bootstrap) safe in tests and maintenance jobs.
        ensure_memory_banks_schema(conn)
        # 🔴v20.0 卫生（**不是**假红修复）：两个 DEFAULT 从字面量 'default' 换成
        # 常量插值。发出的 SQL 逐字节不变（两个常量的值就是 'default'），所以一个
        # 红灯都不会变色 —— 换的是**可读性与可追溯性**：读代码的人能立刻看出这
        # 里写的是「历史占位符」而不是「当前默认身份」，两者在改过名的部署上是
        # 不同的东西。写死字面量正是 甲9 那一串缺陷的温床。
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS memory_types (
                memory_ref   TEXT PRIMARY KEY,  -- mem0 id 或 facts 表名:rowid
                memory_type  TEXT NOT NULL DEFAULT 'FACTS',
                source       TEXT DEFAULT 'rule',
                confidence   REAL DEFAULT 0.5,
                updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                user_id      TEXT NOT NULL DEFAULT '{LEGACY_PLACEHOLDER_USER_ID}',
                bank_id      TEXT NOT NULL DEFAULT '{DEFAULT_BANK_ID}',
                memory_ref_raw TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_types_type ON memory_types(memory_type)"
        )
        # v20.2.4（外审 F-16）：**三列唯一约束**。
        #
        # 表的主键是单列 memory_ref。具名域靠派生 storage key 躲开了碰撞，
        # 但默认域为了保持旧 key 形状**原样使用 ref** —— 于是两个用户拿同一个
        # ref 写入时会撞 ON CONFLICT(memory_ref)，后写者不但覆盖类型，
        # 还把 user_id/bank_id 一并改成自己的（外审实测：表内只剩 1 行，
        # alice 查询回退 FACTS）。
        #
        # 做法是 additive 加唯一索引，**不重建表**（SQLite 改主键要重建，
        # 而重建是不可逆动作；生产 dry-run 实测 346 行、新约束下冲突组 0、
        # 同 ref 跨用户 0 组 —— 加索引不会丢任何行）。upsert 的冲突目标
        # 同步改成这三列。
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_types_scope_ref "
            "ON memory_types(user_id, bank_id, memory_ref_raw)"
        )
        # P2-4：ref 空间统一兼容。主链写时用 mem0 UUID，backfill 用 fact:{id}，
        # 两者可能指向同一条记忆。这里幂等补 ref_alt 列，查询时双 ref 可命中。
        try:
            conn.execute("ALTER TABLE memory_types ADD COLUMN ref_alt TEXT")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_types_ref_alt ON memory_types(ref_alt) "
                "WHERE ref_alt IS NOT NULL"
            )
        except Exception:
            # ref_alt 已存在或 ALTER 不被支持时忽略，查询侧会回退单 ref。
            pass
        # v20.0 scope columns are additive so old memory_types.db/facts.db
        # snapshots remain readable.  Do not rebuild the table: preserving
        # the legacy memory_ref primary key is what keeps existing joins and
        # exports stable.  Named banks use a deterministic scoped storage key
        # (see _storage_ref below), while memory_ref_raw retains the public id.
        # 🔴v20.0 卫生：同上，字面量换常量插值，发出的 SQL 逐字节不变。
        # 这条 ALTER 正是 甲9 的成因本体：``ADD COLUMN … DEFAULT 'default'`` 会把
        # **所有**存量行一次性写满字面量，于是后面那种
        # ``WHERE user_id IS NULL OR TRIM(user_id)=''`` 的补写永远不可能触发。
        for column, ddl in (
            ("user_id", f"TEXT NOT NULL DEFAULT '{LEGACY_PLACEHOLDER_USER_ID}'"),
            ("bank_id", f"TEXT NOT NULL DEFAULT '{DEFAULT_BANK_ID}'"),
            ("memory_ref_raw", "TEXT"),
        ):
            try:
                conn.execute(f"ALTER TABLE memory_types ADD COLUMN {column} {ddl}")
            except Exception:
                pass
        try:
            conn.execute(
                "UPDATE memory_types SET memory_ref_raw=memory_ref "
                "WHERE memory_ref_raw IS NULL OR memory_ref_raw=''"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_types_scope_ref "
                "ON memory_types(user_id, bank_id, memory_ref_raw)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_memory_types_scope "
                "ON memory_types(user_id, bank_id, memory_type)"
            )
        except Exception as exc:
            logger.debug("memory_types scope index/backfill skipped: %s", exc)
        conn.commit()
        _checked = True
    except Exception as e:
        logger.warning(f"memory_types 表初始化失败（服务继续）: {e}")
    finally:
        conn.close()


def _scope(user_id: str | None = None, bank_id: str | None = None):
    """Create/validate a scope and lazily register its bank."""
    scope = make_scope(
        DEFAULT_USER_ID if user_id is None else user_id,
        DEFAULT_BANK_ID if bank_id is None else bank_id,
    )
    # Registration is intentionally best-effort here; schema initialisation
    # itself remains the source of truth and a read-only deployment should be
    # able to query existing rows without inventing a bank record.
    try:
        ensure_bank_registered(scope)
    except Exception as exc:
        logger.debug("memory_types bank registration skipped: %s", exc)
    return scope


def _storage_ref(memory_ref: str, scope) -> str:
    """Return a collision-free DB PK while preserving default legacy ids."""
    return scoped_storage_key(memory_ref, scope)


def _readable_owners(scope) -> tuple[str, list[str]]:
    """🔴v20.0：账本**读**侧的租户口径。返回 (sql_fragment, params)。

    ``memory_types`` 的 scope 列是 v20.0 增补的，``ALTER TABLE … DEFAULT
    'default'`` 会把所有存量行一次性写成字面量 ``default``。部署方一旦配了
    ``AIDUMEM_DEFAULT_USER_ID``，读侧只认新身份，存量账本就整体失明 ——
    表现是「类型明明标过，查出来全是 FACTS」。参见 ``reflect.py`` 里
    ``_identity_ids`` 记录的 2026-08-19 实例：改名让 10 条真实反思消失。

    与 ``facts`` 不同，本表**没有**可用的归属信道：``source`` 列的取值是
    ``rule``/``backfill``/``llm`` 这类**来源标签**，不是租户名，拿它当
    ``(source=? OR agent_id=?)`` 那样的认领信道会张冠李戴。没有信道可验，就
    只能走 :func:`~ducky.bank_contract.visible_user_ids` 的「只加不减」口径 ——
    占位符行只发给默认租户，具名租户一行不多给、一行不少给。

    起草这段时踩过一次：一开始图省事写的是 ``unclaimed_user_ids()``。那函数
    不带调用方身份，返回的是「默认身份 + 占位符」，于是具名租户 ``alice``
    会同时**丢掉自己的行**（``alice`` 不在集合里）和**读到默认租户的行** ——
    一次功能损坏加一次越租户泄漏。两个函数名字像、语义不同，别再抄错。

    **只给读用。** 删除路径（``reset_all_types``）必须保持精确匹配：放宽读
    是让用户看见的变多，放宽删是让用户的数据变少，两者绝不共用一个口径。
    """
    return visible_user_clause(scope.user_id)


# ── 确定性规则分类（无 LLM 兜底）────────────────────────────────────────
# 规则顺序 = 优先级；命中即返回，不叠加判断。
_RULE_PATTERNS: list[tuple[str, list[str]]] = [
    ("DECISIONS", [
        r"决定|约定|铁律|红线|必须|禁止|不允许|不可逆|拍板",
    ]),
    ("PREFERENCES", [
        r"偏好|喜欢|不喜欢|讨厌|更愿意|倾向|希望|想要|偏爱",
    ]),
    ("EXPERIENCES", [
        r"我帮|我们完成|部署了|修复了|执行|跑通|调试|上线|迁移|解决了",
    ]),
    ("OBSERVATIONS", [
        r"观察到|注意到|看起来|似乎|状态|监听|占用|暴露|配置",
    ]),
    ("REFLECTIONS", [
        r"反思|洞察|模式|矛盾|预测|缺口|接下来可能需要",
    ]),
]


def classify_text(text: str) -> str:
    """确定性规则判型。失败/无信号一律 FACTS（安全默认）。"""
    if not text:
        return "FACTS"
    for mem_type, patterns in _RULE_PATTERNS:
        for pat in patterns:
            if re.search(pat, text):
                return mem_type
    return "FACTS"


def _llm_classify(text: str) -> Optional[str]:
    """LLM 判型（可选增强）。失败返回 None，由调用方回退规则。"""
    try:
        from ducky.llm_client import call_llm

        system = (
            "你是 aiduMEI 的记忆分类器。只输出一个 JSON 对象："
            '{"memory_type":"FACTS|PREFERENCES|EXPERIENCES|OBSERVATIONS|REFLECTIONS|DECISIONS",'
            '"confidence":0.0-1.0}。不要输出解释。'
        )
        raw = call_llm(
            f"请给这段记忆分类：{text[:400]}",
            system=system,
            max_tokens=64,
            temperature=0.0,
        )
        if not raw:
            return None
        # 先直接解析裸 JSON；只有解析失败才尝试剥掉 ```json 围栏。
        # 注意不能用 lstrip("`{")/rstrip("}`")：它们按字符集剥离，会把
        # 合法 JSON 的开头 { 和结尾 } 也剥掉，导致 LLM 分类永远失败。
        text = raw.strip()
        data = None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                try:
                    data = json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    data = None
        if isinstance(data, dict) and data.get("memory_type") in VALID_TYPES:
            return str(data["memory_type"])
    except Exception as e:
        logger.debug(f"LLM 判型失败（回退规则）: {e}")
    return None


def classify_and_record(
    memory_ref: str,
    text: str,
    *,
    use_llm: bool = False,
    user_id: str = DEFAULT_USER_ID,
    bank_id: str = DEFAULT_BANK_ID,
) -> dict:
    """判型并写入账本。

    ``user_id``/``bank_id`` are keyword-only to preserve every v19 caller.
    The public ``memory_ref`` remains unchanged in the return value, while a
    deterministic scoped key is used internally for named banks so identical
    ids can safely coexist.
    """
    ensure_memory_types_schema()
    scope = _scope(user_id, bank_id)
    raw_ref = str(memory_ref or "").strip()
    if not raw_ref:
        raise ValueError("memory_ref 不能为空")
    storage_ref = _storage_ref(raw_ref, scope)

    confidence = 0.5
    source = "rule"
    memory_type = classify_text(text)

    if use_llm:
        llm_type = _llm_classify(text)
        if llm_type:
            memory_type = llm_type
            source = "llm"
            confidence = 0.8

    conn = get_facts_conn()
    try:
        alt = None
        if raw_ref.startswith("fact:"):
            alt = raw_ref[5:]
        conn.execute(
            "INSERT INTO memory_types "
            "(memory_ref, ref_alt, memory_type, source, confidence, updated_at, user_id, bank_id, memory_ref_raw) "
            "VALUES (?,?,?,?,?,CURRENT_TIMESTAMP,?,?,?) "
            # v20.2.4 F-16：冲突目标 = 三列唯一约束，不再是单列 memory_ref
                "ON CONFLICT(user_id, bank_id, memory_ref_raw) DO UPDATE SET "
            "ref_alt=COALESCE(excluded.ref_alt, memory_types.ref_alt), "
            "memory_type=excluded.memory_type, source=excluded.source, "
            "confidence=excluded.confidence, updated_at=CURRENT_TIMESTAMP, "
            "user_id=excluded.user_id, bank_id=excluded.bank_id, "
            "memory_ref_raw=excluded.memory_ref_raw",
            (storage_ref, alt, memory_type, source, confidence,
             scope.user_id, scope.bank_id, raw_ref),
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"记忆类型落账失败: {e}")
    finally:
        conn.close()

    return {
        "memory_type": memory_type,
        "source": source,
        "confidence": confidence,
        "user_id": scope.user_id,
        "bank_id": scope.bank_id,
        "memory_ref": raw_ref,
    }


def memory_type_ref(item: dict) -> str:
    """从检索结果条目构造类型账本的查询键。**唯一构造点。**

    v20.2.4（外审 F-15 顺带整改）：此前两处各自构造 —— hot/search 是
    `fact:{fact_id}` 优先、UUID 回退，而 scoring 只用 UUID。于是同一条记忆
    在两条路径上可能拿到**不同的类型**（一处查得到、一处查不到回退 FACTS），
    而没有任何东西会告诉你这件事。构造逻辑收敛到这里，两边都调它。
    """
    if not isinstance(item, dict):
        return ""
    meta = item.get("metadata") or {}
    if isinstance(meta, dict) and meta.get("fact_id") is not None:
        return f"fact:{meta['fact_id']}"
    return str(item.get("id") or item.get("memory_id") or "")


def get_batch_memory_types(
    memory_refs: list[str],
    *,
    user_id: str = DEFAULT_USER_ID,
    bank_id: str = DEFAULT_BANK_ID,
) -> dict[str, str]:
    """批量查询多条记忆的类型（0 N+1 数据库往返）。
    未记录的默认返回 FACTS。
    """
    if not memory_refs:
        return {}
    ensure_memory_types_schema()
    scope = _scope(user_id, bank_id)
    conn = get_facts_conn()
    unique_refs = list(set(str(r) for r in memory_refs if r))
    if not unique_refs:
        return {}
    result: dict[str, str] = {r: "FACTS" for r in unique_refs}
    try:
        placeholders = ",".join("?" for _ in unique_refs)
        storage_refs = [_storage_ref(ref, scope) for ref in unique_refs]
        # 🔴v20.0：租户口径走 _readable_owners，不再是 user_id=? 精确匹配。
        # bank 轴仍是精确相等 —— bank_id 不可被环境变量改名，没有存量占位符
        # 问题，而 bank 一旦放宽就是跨库串味。
        owner_sql, owner_params = _readable_owners(scope)
        query = f"""
            SELECT memory_ref, memory_ref_raw, ref_alt, memory_type
            FROM memory_types
            WHERE {owner_sql} AND bank_id=? AND
              (memory_ref IN ({placeholders})
               OR memory_ref_raw IN ({placeholders})
               OR (ref_alt IS NOT NULL AND ref_alt IN ({placeholders})))
        """
        rows = conn.execute(
            query,
            owner_params + [scope.bank_id] + storage_refs + unique_refs + unique_refs,
        ).fetchall()
        for r in rows:
            mtype = r["memory_type"]
            mref = r["memory_ref"]
            raw = r["memory_ref_raw"] or raw_storage_key(mref, scope)
            ralt = r["ref_alt"]
            if mref:
                result[raw] = mtype
            if ralt:
                result[ralt] = mtype
        return result
    except Exception as e:
        logger.warning(f"get_batch_memory_types 失败: {e}")
        return result
    finally:
        conn.close()

def get_memory_type(
    memory_ref: str,
    *,
    user_id: str = DEFAULT_USER_ID,
    bank_id: str = DEFAULT_BANK_ID,
) -> str:
    """查询某条记忆的类型；未记录返回 FACTS（老数据默认事实）。

    P2-4：支持双 ref 命中——主链 UUID 与 backfill 的 fact:{id} 任一
    匹配都返回同一分类，避免同一条记忆出现两条对不上的账本记录。
    """
    ensure_memory_types_schema()
    scope = _scope(user_id, bank_id)
    raw_ref = str(memory_ref or "").strip()
    if not raw_ref:
        return "FACTS"
    storage_ref = _storage_ref(raw_ref, scope)
    conn = get_facts_conn()
    owner_sql, owner_params = _readable_owners(scope)   # 🔴v20.0：见 _readable_owners
    try:
        row = conn.execute(
            "SELECT memory_type FROM memory_types "
            f"WHERE {owner_sql} AND bank_id=? AND "
            "(memory_ref=? OR memory_ref_raw=? OR (ref_alt IS NOT NULL AND ref_alt=?))",
            (*owner_params, scope.bank_id, storage_ref, raw_ref, raw_ref),
        ).fetchone()
        return row["memory_type"] if row else "FACTS"
    finally:
        conn.close()


def list_types(
    user_id: str = DEFAULT_USER_ID,
    bank_id: str = DEFAULT_BANK_ID,
) -> list[dict]:
    """按指定 bank 统计已分类记忆数量（供控制台/审计）。"""
    ensure_memory_types_schema()
    scope = _scope(user_id, bank_id)
    conn = get_facts_conn()
    owner_sql, owner_params = _readable_owners(scope)   # 🔴v20.0：见 _readable_owners
    try:
        rows = conn.execute(
            "SELECT memory_type, COUNT(*) AS cnt, ROUND(AVG(confidence),3) AS avg_conf "
            f"FROM memory_types WHERE {owner_sql} AND bank_id=? "
            "GROUP BY memory_type ORDER BY cnt DESC",
            (*owner_params, scope.bank_id),
        ).fetchall()
        return [
            {
                "memory_type": r["memory_type"],
                "label": TYPE_LABELS.get(r["memory_type"], r["memory_type"]),
                "count": r["cnt"],
                "avg_confidence": r["avg_conf"] or 0,
            }
            for r in rows
        ]
    finally:
        conn.close()


def reset_all_types(
    user_id: str = DEFAULT_USER_ID,
    bank_id: str = DEFAULT_BANK_ID,
    *,
    all_scopes: bool = False,
) -> int:
    """清空指定 bank 的类型账本（重建用）。

    ``all_scopes`` is an explicit escape hatch for offline maintenance only;
    the HTTP route never enables it.  This prevents a v19-style unscoped
    ``DELETE FROM memory_types`` from erasing another bank.
    """
    ensure_memory_types_schema()
    scope = _scope(user_id, bank_id)
    conn = get_facts_conn()
    try:
        if all_scopes:
            cur = conn.execute("DELETE FROM memory_types")
        else:
            cur = conn.execute(
                "DELETE FROM memory_types WHERE user_id=? AND bank_id=?",
                (scope.user_id, scope.bank_id),
            )
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def backfill_from_facts(
    limit: int = 2000,
    *,
    user_id: str = DEFAULT_USER_ID,
    bank_id: str = DEFAULT_BANK_ID,
) -> dict:
    """从 facts.db 现有数据重建类型账本（存量数据 P1-1 迁移）。

    规则：memory_ref = "fact:{id}"，用 fact_key + fact_value 判型。
    返回 {scanned, classified}。
    """
    ensure_memory_types_schema()
    scope = _scope(user_id, bank_id)
    conn = get_facts_conn()
    classified = 0
    scanned = 0
    try:
        # ``ensure_memory_banks_schema`` adds these columns to old facts
        # tables.  A bank must never be a substring/LIKE filter, and the LIMIT
        # must apply after isolation.
        #
        # 🔴v20.0：这里读的是 **facts**，但租户口径**不能**照抄
        # ``facts_recall.tenant_clause``。实测（改名轴、宽严两档都一样）：默认
        # 租户拿到的片段只有 `` AND bank_id=?``，**完全没有 user_id 谓词**，
        # 于是连 ``alice`` 的行一起扫进来。``/search`` 那样读一下就算了，可
        # 本函数**要写账本**（写侧盖的是 ``scope.user_id``）—— 那就等于把别的
        # 租户的事实永久登记成默认租户的资产，还让默认租户的 ``list_types``
        # 数出别人的条数。读侧宽一格是看得见的变多，写侧宽一格是越租户污染，
        # 两者绝不能共用一个口径。
        #
        # 所以只做**红灯真正需要的那一格**：把 ALTER TABLE 一次性写满的存量
        # 字面量算作可读（``_readable_owners`` 的只加不减集合），bank 轴保持
        # 精确相等。原来写死 ``user_id=?``，存量行在改过名的部署上一条都扫不
        # 到，scanned=0、classified=0 —— 迁移工具静默空跑，不报错，只是什么都
        # 没干。
        owner_sql, owner_params = _readable_owners(scope)
        rows = conn.execute(
            "SELECT id, fact_key, fact_value FROM facts "
            f"WHERE archived=0 AND {owner_sql} AND bank_id=? "
            "ORDER BY id LIMIT ?",
            (*owner_params, scope.bank_id, max(1, min(int(limit), 5000))),
        ).fetchall()
        scanned = len(rows)
        for r in rows:
            ref = f"fact:{r['id']}"
            mem_type = classify_text(f"{r['fact_key']} {r['fact_value']}")
            storage_ref = _storage_ref(ref, scope)
            conn.execute(
                "INSERT INTO memory_types "
                "(memory_ref, memory_type, source, confidence, updated_at, user_id, bank_id, memory_ref_raw) "
                "VALUES (?,?,?,?,CURRENT_TIMESTAMP,?,?,?) "
                # v20.2.4 F-16：冲突目标 = 三列唯一约束，不再是单列 memory_ref
                "ON CONFLICT(user_id, bank_id, memory_ref_raw) DO UPDATE SET "
                "memory_type=excluded.memory_type, source='backfill', confidence=0.5, "
                "updated_at=CURRENT_TIMESTAMP, user_id=excluded.user_id, "
                "bank_id=excluded.bank_id, memory_ref_raw=excluded.memory_ref_raw",
                (storage_ref, mem_type, "backfill", 0.5,
                 scope.user_id, scope.bank_id, ref),
            )
            classified += 1
        conn.commit()
    except Exception as e:
        logger.warning(f"backfill_from_facts 失败: {e}")
    finally:
        conn.close()
    logger.info("P1-1 backfill: scanned=%d classified=%d", scanned, classified)
    return {"scanned": scanned, "classified": classified}
