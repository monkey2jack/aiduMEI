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

from ducky.utils import get_facts_conn

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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_types (
                memory_ref   TEXT PRIMARY KEY,  -- mem0 id 或 facts 表名:rowid
                memory_type  TEXT NOT NULL DEFAULT 'FACTS',
                source       TEXT DEFAULT 'rule',
                confidence   REAL DEFAULT 0.5,
                updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_types_type ON memory_types(memory_type)"
        )
        conn.commit()
        _checked = True
    except Exception as e:
        logger.warning(f"memory_types 表初始化失败（服务继续）: {e}")
    finally:
        conn.close()


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


def classify_and_record(memory_ref: str, text: str, *, use_llm: bool = False) -> dict:
    """判型并写入账本。返回 {memory_type, source, confidence}。"""
    ensure_memory_types_schema()

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
        conn.execute(
            "INSERT INTO memory_types (memory_ref, memory_type, source, confidence, updated_at) "
            "VALUES (?,?,?,?,CURRENT_TIMESTAMP) "
            "ON CONFLICT(memory_ref) DO UPDATE SET "
            "memory_type=excluded.memory_type, source=excluded.source, "
            "confidence=excluded.confidence, updated_at=CURRENT_TIMESTAMP",
            (memory_ref, memory_type, source, confidence),
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"记忆类型落账失败: {e}")
    finally:
        conn.close()

    return {"memory_type": memory_type, "source": source, "confidence": confidence}


def get_memory_type(memory_ref: str) -> str:
    """查询某条记忆的类型；未记录返回 FACTS（老数据默认事实）。"""
    ensure_memory_types_schema()
    conn = get_facts_conn()
    try:
        row = conn.execute(
            "SELECT memory_type FROM memory_types WHERE memory_ref=?", (memory_ref,)
        ).fetchone()
        return row["memory_type"] if row else "FACTS"
    finally:
        conn.close()


def list_types(user_id: str = "default") -> list[dict]:
    """按类型统计已分类记忆数量（供控制台/审计）。"""
    ensure_memory_types_schema()
    conn = get_facts_conn()
    try:
        rows = conn.execute(
            "SELECT memory_type, COUNT(*) AS cnt, ROUND(AVG(confidence),3) AS avg_conf "
            "FROM memory_types GROUP BY memory_type ORDER BY cnt DESC"
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


def reset_all_types() -> int:
    """清空类型账本（重建用）。返回删除条数。"""
    ensure_memory_types_schema()
    conn = get_facts_conn()
    try:
        cur = conn.execute("DELETE FROM memory_types")
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def backfill_from_facts(limit: int = 2000) -> dict:
    """从 facts.db 现有数据重建类型账本（存量数据 P1-1 迁移）。

    规则：memory_ref = "fact:{id}"，用 fact_key + fact_value 判型。
    返回 {scanned, classified}。
    """
    ensure_memory_types_schema()
    conn = get_facts_conn()
    classified = 0
    scanned = 0
    try:
        rows = conn.execute(
            "SELECT id, fact_key, fact_value FROM facts WHERE archived=0 ORDER BY id LIMIT ?",
            (max(1, min(int(limit), 5000)),),
        ).fetchall()
        scanned = len(rows)
        for r in rows:
            ref = f"fact:{r['id']}"
            mem_type = classify_text(f"{r['fact_key']} {r['fact_value']}")
            conn.execute(
                "INSERT INTO memory_types (memory_ref, memory_type, source, confidence, updated_at) "
                "VALUES (?,?,?,?,CURRENT_TIMESTAMP) "
                "ON CONFLICT(memory_ref) DO UPDATE SET "
                "memory_type=excluded.memory_type, source='backfill', confidence=0.5, "
                "updated_at=CURRENT_TIMESTAMP",
                (ref, mem_type, "backfill", 0.5),
            )
            classified += 1
        conn.commit()
    except Exception as e:
        logger.warning(f"backfill_from_facts 失败: {e}")
    finally:
        conn.close()
    logger.info("P1-1 backfill: scanned=%d classified=%d", scanned, classified)
    return {"scanned": scanned, "classified": classified}
