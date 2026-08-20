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
    ensure_bank_registered,
    ensure_memory_banks_schema,
    make_scope,
    scoped_storage_key,
    raw_storage_key,
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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_types (
                memory_ref   TEXT PRIMARY KEY,  -- mem0 id 或 facts 表名:rowid
                memory_type  TEXT NOT NULL DEFAULT 'FACTS',
                source       TEXT DEFAULT 'rule',
                confidence   REAL DEFAULT 0.5,
                updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                user_id      TEXT NOT NULL DEFAULT 'default',
                bank_id      TEXT NOT NULL DEFAULT 'default',
                memory_ref_raw TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_types_type ON memory_types(memory_type)"
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
        for column, ddl in (
            ("user_id", "TEXT NOT NULL DEFAULT 'default'"),
            ("bank_id", "TEXT NOT NULL DEFAULT 'default'"),
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
            "ON CONFLICT(memory_ref) DO UPDATE SET "
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
        query = f"""
            SELECT memory_ref, memory_ref_raw, ref_alt, memory_type
            FROM memory_types
            WHERE user_id=? AND bank_id=? AND
              (memory_ref IN ({placeholders})
               OR memory_ref_raw IN ({placeholders})
               OR (ref_alt IS NOT NULL AND ref_alt IN ({placeholders})))
        """
        rows = conn.execute(
            query,
            [scope.user_id, scope.bank_id] + storage_refs + unique_refs + unique_refs,
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
    try:
        row = conn.execute(
            "SELECT memory_type FROM memory_types "
            "WHERE user_id=? AND bank_id=? AND "
            "(memory_ref=? OR memory_ref_raw=? OR (ref_alt IS NOT NULL AND ref_alt=?))",
            (scope.user_id, scope.bank_id, storage_ref, raw_ref, raw_ref),
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
    try:
        rows = conn.execute(
            "SELECT memory_type, COUNT(*) AS cnt, ROUND(AVG(confidence),3) AS avg_conf "
            "FROM memory_types WHERE user_id=? AND bank_id=? "
            "GROUP BY memory_type ORDER BY cnt DESC",
            (scope.user_id, scope.bank_id),
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
        # tables.  Exact equality is intentional: a bank must never be a
        # substring/LIKE filter, and the LIMIT must apply after isolation.
        rows = conn.execute(
            "SELECT id, fact_key, fact_value FROM facts "
            "WHERE archived=0 AND user_id=? AND bank_id=? "
            "ORDER BY id LIMIT ?",
            (scope.user_id, scope.bank_id, max(1, min(int(limit), 5000))),
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
                "ON CONFLICT(memory_ref) DO UPDATE SET "
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
