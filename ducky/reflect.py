"""
ducky.reflect — Reflect 反思引擎（v19.0 · P0-3 核心亮点）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
借鉴 Hindsight 的 Reflect 操作，补上 aiduMEI 一直缺失的「主动认知」：
Agent 不再只会存了再搜，而是能定期/触发式回顾记忆，提炼出模式、
关系、预测、矛盾与知识缺口，并把洞察落库，供后续对话注入引用。

设计原则
    · 降级友好：LLM 未配置 / 调用失败 / 解析失败都不抛异常，返回空洞察
    · 幂等落库：同一条洞察（content 哈希）不重复入库
    · 洞察即记忆：reflections 是一等公民，可列表查询、可注入上下文
    · 后台循环：默认每 6 小时一次（AIDUMEM_REFLECT_INTERVAL_HOURS 可调），
      AIDUMEM_REFLECT_ENABLED=false 可整体关闭后台主动反思（手动 /reflect 不受影响）

触发方式（对齐调研报告三选一）
    a) 手动触发  → POST /reflect
    b) 后台定期  → reflect_background_loop（默认开）
    c) 会话结束  → 由集成方调用 run_reflect(user_id, source="session_end")
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

from ducky.llm_client import call_llm
from ducky.utils import DEFAULT_USER_ID, get_facts_conn

logger = logging.getLogger("aiduMEM.reflect")

# 后台反思间隔（小时），环境变量可调
def _parse_reflect_interval() -> float:
    """解析 AIDUMEM_REFLECT_INTERVAL_HOURS，非法值安全降级为默认 6h。

    - 非数值 / 空串 → 6.0（不 crash 导入）
    - <= 0 → 6.0（"0" 是 truthy，`or 6` 拦不住，必须在数值层拦截，
      否则后台线程 time.sleep(0) 会变成烧 LLM 的忙循环）
    """
    try:
        raw = os.environ.get("AIDUMEM_REFLECT_INTERVAL_HOURS", "6")
        val = float(raw) if str(raw).strip() else 6.0
    except (TypeError, ValueError):
        return 6.0
    return val if val > 0 else 6.0


REFLECT_INTERVAL_HOURS = _parse_reflect_interval()
REFLECT_ENABLED = os.environ.get("AIDUMEM_REFLECT_ENABLED", "true").strip().lower() not in {
    "0", "false", "no", "off",
}

# 每类洞察的标签（用于展示与注入）
INSIGHT_LABELS = {
    "pattern": "模式识别",
    "relation": "关系发现",
    "prediction": "预测洞察",
    "conflict": "矛盾检测",
    "gap": "知识缺口",
}

REFLECT_SYSTEM = (
    "你是 aiduMEI 的记忆反思引擎。回顾给定的记忆片段，提炼真正有价值、"
    "可被后续对话引用的洞察。只输出一个 JSON 数组，不要输出任何解释文字。"
)

REFLECT_USER_TEMPLATE = """请回顾以下记忆片段，生成洞察：

【记忆片段】
{memories}

【结构化事实】
{facts}

请从五个维度提炼洞察（每个维度最多 2 条，宁缺毋滥）：
1. pattern：最近出现了哪些重复主题/偏好/需求？
2. relation：哪些看似无关的事实之间存在关联？
3. prediction：基于历史，用户接下来可能需要什么？
4. conflict：哪些记忆之间存在冲突需要澄清？
5. gap：有哪些重要的用户信息我们还没有？

每条洞察用 JSON 对象表示：
{{"type": "pattern|relation|prediction|conflict|gap", "content": "洞察内容", "confidence": 0.0-1.0, "evidence": ["引用的记忆片段编号或事实 key"]}}

只输出 JSON 数组，例如：
[{{"type": "pattern", "content": "用户近三次交互都提到产品定位与差异化", "confidence": 0.8, "evidence": ["m1", "m3"]}}]"""

_checked = False


def ensure_reflect_schema() -> None:
    """幂等建 reflections 表（放在 facts.db，跟随核心库备份与迁移）。"""
    global _checked
    if _checked:
        return
    conn = get_facts_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reflections (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      TEXT NOT NULL DEFAULT 'default',
                insight_type TEXT DEFAULT 'pattern',
                content      TEXT NOT NULL,
                confidence   REAL DEFAULT 0.5,
                evidence     TEXT DEFAULT '[]',
                source       TEXT DEFAULT 'manual',
                recorded_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reflections_user ON reflections(user_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reflections_type ON reflections(insight_type)"
        )
        conn.commit()
        _checked = True
    except Exception as e:
        logger.warning(f"reflections 表初始化失败（服务继续）: {e}")
    finally:
        conn.close()


def _gather_recent_memories(memory, user_id: str, top_k: int) -> list[dict]:
    """收集最近记忆（mem0 全量最近的 top_k 条），编号 m1..mN。"""
    try:
        from ducky.mem0_runtime import _normalize_user_id
        user_id = _normalize_user_id(user_id)
    except Exception:
        pass

    try:
        all_mem = None
        # 部分 mem0 版本的 get_all 不支持 filters 参数，先试带 filter，
        # 失败再降级为全量拉取后内存过滤。
        # 注意：mem0 2.0.x get_all 的关键字是 top_k（不是 limit），
        # 传 limit 会被 **kwargs 静默吞掉导致限数失效。
        try:
            all_mem = memory.get_all(filters={"user_id": user_id}, top_k=max(top_k, 20))
        except TypeError:
            all_mem = memory.get_all(top_k=max(top_k, 200))
        results = all_mem.get("results", all_mem) if isinstance(all_mem, dict) else all_mem
        if not isinstance(results, list):
            return []
        snippets = []
        for i, item in enumerate(results, start=1):
            if not isinstance(item, dict):
                continue
            if str(item.get("user_id") or "") not in ("", user_id, "default"):
                continue
            content = (item.get("memory") or "").strip()
            if not content:
                continue
            snippets.append({
                "ref": f"m{i}",
                "id": item.get("id", ""),
                "text": content[:240],
                "created_at": item.get("created_at", ""),
            })
            if len(snippets) >= top_k:
                break
        return snippets
    except Exception as e:
        logger.debug(f"收集最近记忆失败（降级为空）: {e}")
        return []


def _gather_topic_memories(memory, user_id: str, topic: str, top_k: int) -> list[dict]:
    """围绕指定主题语义检索相关记忆（供 MCP mem_reflect 兼容）。"""
    try:
        from ducky.mem0_runtime import _normalize_user_id
        user_id = _normalize_user_id(user_id)
    except Exception:
        pass
    try:
        try:
            raw = memory.search(topic, filters={"user_id": user_id}, top_k=max(top_k, 10))
        except TypeError:
            raw = memory.search(topic, filters={"user_id": user_id}, limit=max(top_k, 10))
        results = raw.get("results", raw) if isinstance(raw, dict) else raw
        if not isinstance(results, list):
            return []
        snippets = []
        for i, item in enumerate(results, start=1):
            if not isinstance(item, dict):
                continue
            content = (item.get("memory") or "").strip()
            if not content:
                continue
            snippets.append({
                "ref": f"m{i}",
                "id": item.get("id", ""),
                "text": content[:240],
                "created_at": item.get("created_at", ""),
            })
            if len(snippets) >= top_k:
                break
        return snippets
    except Exception as e:
        logger.debug(f"主题检索失败（降级为空）: {e}")
        return []


def _gather_recent_facts(top_k: int) -> list[dict]:
    """收集最近更新的结构化事实，编号 f1..fN。"""
    try:
        conn = get_facts_conn()
        rows = conn.execute(
            "SELECT id, category, fact_key, fact_value, updated_at FROM facts "
            "WHERE archived=0 ORDER BY COALESCE(updated_at, created_at) DESC LIMIT ?",
            (top_k,),
        ).fetchall()
        conn.close()
        facts = []
        for i, row in enumerate(rows, start=1):
            value = (row["fact_value"] or "").strip()
            if not value:
                continue
            facts.append({
                "ref": f"f{i}",
                "id": row["id"],
                "key": row["fact_key"],
                "text": f"[{row['category']}] {row['fact_key']}: {value[:200]}",
            })
        return facts
    except Exception as e:
        logger.debug(f"收集最近事实失败（降级为空）: {e}")
        return []


def _build_prompt(memories: list[dict], facts: list[dict]) -> str:
    mem_lines = []
    for m in memories:
        meta = f" (记录于 {m['created_at'][:10]})" if m.get("created_at") else ""
        mem_lines.append(f"{m['ref']}{meta}: {m['text']}")
    fact_lines = [f"{f['ref']} {f['text']}" for f in facts]

    memories_block = "\n".join(mem_lines) if mem_lines else "（无近期记忆）"
    facts_block = "\n".join(fact_lines) if fact_lines else "（无结构化事实）"
    return REFLECT_USER_TEMPLATE.format(memories=memories_block, facts=facts_block)


def _parse_insights(raw: str) -> list[dict]:
    """把 LLM 输出解析成洞察列表。容忍 JSON 数组外裹文字、单对象、纯文本行。"""
    if not raw:
        return []
    text = raw.strip()

    # 1) 优先：整段 JSON 数组
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            data = [data]
        if isinstance(data, list):
            return [
                {
                    "type": _norm_type(d.get("type")),
                    "content": str(d.get("content") or "").strip(),
                    "confidence": _clamp_confidence(d.get("confidence")),
                    "evidence": d.get("evidence") or [],
                }
                for d in data
                if isinstance(d, dict) and str(d.get("content") or "").strip()
            ]
    except json.JSONDecodeError:
        pass

    # 2) 降级：截取首个 [ 到最后一个 ] 再解析
    start, end = text.find("["), text.rfind("]")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            if isinstance(data, list):
                return [
                    {
                        "type": _norm_type(d.get("type")),
                        "content": str(d.get("content") or "").strip(),
                        "confidence": _clamp_confidence(d.get("confidence")),
                        "evidence": d.get("evidence") or [],
                    }
                    for d in data
                    if isinstance(d, dict) and str(d.get("content") or "").strip()
                ]
        except json.JSONDecodeError:
            pass

    # 3) 兜底：按行解析「类型:内容 (置信度)」形式的纯文本。
    #    只识别带明确类型标签的行（如「模式识别：…」「- 知识缺口：…」），
    #    无标签的行跳过，避免把噪音误当洞察。
    insights = []
    for line in text.splitlines():
        line = line.strip().lstrip("-•·* ").strip()
        if not line:
            continue
        ins_type, rest = _split_typed_line(line)
        if ins_type and rest:
            insights.append({"type": ins_type, "content": rest, "confidence": 0.5, "evidence": []})
    return insights


def _split_typed_line(line: str) -> tuple[Optional[str], str]:
    """从一行纯文本里识别「类型标签」前缀，返回 (归一化类型, 正文)。

    支持：'模式识别：用户频繁提到部署'、'- 知识缺口: 缺少联系方式'、'[关系发现] A与B相关'。
    未识别出标签时返回 (None, '')，由调用方跳过。
    """
    if not line:
        return None, ""
    stripped = line.strip()
    if not stripped:
        return None, ""
    # 去掉常见的列表/引用前缀
    cleaned = stripped.lstrip("-•·*#>0123456789.、 ").strip()
    label_hits = [
        ("模式识别", "pattern"),
        ("关系发现", "relation"),
        ("预测洞察", "prediction"),
        ("矛盾检测", "conflict"),
        ("知识缺口", "gap"),
        ("pattern", "pattern"),
        ("relation", "relation"),
        ("prediction", "prediction"),
        ("conflict", "conflict"),
        ("gap", "gap"),
    ]
    for label, ins_type in label_hits:
        if cleaned.startswith(label):
            rest = cleaned[len(label):].lstrip("：:—| ").strip()
            if rest:
                return ins_type, rest
    # 兜底：行首形如「类型:内容」，且类型在已知枚举里（英文或中文标签）
    if ":" in cleaned:
        head, _, tail = cleaned.partition(":")
        known_types = {"pattern", "relation", "prediction", "conflict", "gap"}
        if head.strip().lower() in known_types and tail.strip():
            return _norm_type(head), tail.strip()
    return None, ""


def _norm_type(value: Any) -> str:
    mapping = {
        "pattern": "pattern", "模式": "pattern", "模式识别": "pattern",
        "relation": "relation", "关系": "relation", "关系发现": "relation",
        "prediction": "prediction", "预测": "prediction", "预测洞察": "prediction",
        "conflict": "conflict", "矛盾": "conflict", "矛盾检测": "conflict",
        "gap": "gap", "缺口": "gap", "知识缺口": "gap",
    }
    key = str(value or "").strip().lower()
    return mapping.get(key, mapping.get(str(value or "").strip(), "pattern"))


def _clamp_confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.5


def save_insights(insights: list[dict], user_id: str, source: str) -> int:
    """把洞察写入 reflections 表（按 content 哈希去重）。返回实际新增条数。"""
    if not insights:
        return 0
    ensure_reflect_schema()
    now = datetime.now(timezone.utc).isoformat()
    conn = get_facts_conn()
    added = 0
    try:
        for ins in insights:
            content = (ins.get("content") or "").strip()
            if not content:
                continue
            dup = conn.execute(
                "SELECT id FROM reflections WHERE user_id=? AND content=?",
                (user_id, content),
            ).fetchone()
            if dup:
                continue
            conn.execute(
                "INSERT INTO reflections (user_id, insight_type, content, confidence, evidence, source, recorded_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    user_id,
                    ins.get("type", "pattern"),
                    content,
                    _clamp_confidence(ins.get("confidence")),
                    json.dumps(ins.get("evidence") or [], ensure_ascii=False),
                    source,
                    now,
                ),
            )
            added += 1
        conn.commit()
    except Exception as e:
        logger.warning(f"洞察落库失败（降级不抛）: {e}")
    finally:
        conn.close()
    return added


def run_reflect(
    memory=None,
    user_id: str = DEFAULT_USER_ID,
    top_k: int = 20,
    source: str = "manual",
    save: bool = True,
    topic: str = "",
) -> dict:
    """
    执行一次反思：收集近期记忆 + 事实 → LLM 提炼 → 解析 → 落库。

    topic: 可选主题。提供时围绕该主题检索相关记忆（兼容 MCP mem_reflect
           的旧调用契约），否则收集最近记忆。
    Returns:
        {"status": "ok", "insights": [...], "saved": int, "source": ..., "llm_used": bool}
    """
    ensure_reflect_schema()
    mem = memory
    if mem is None:
        try:
            from ducky.mem0_runtime import get_memory
            mem = get_memory()
        except Exception:
            mem = None

    if topic and mem is not None:
        memories = _gather_topic_memories(mem, user_id, topic, top_k)
    else:
        memories = _gather_recent_memories(mem, user_id, top_k) if mem is not None else []
    facts = _gather_recent_facts(max(10, top_k // 2))

    if not memories and not facts:
        return {"status": "ok", "insights": [], "saved": 0, "source": source, "llm_used": False}

    raw = call_llm(
        _build_prompt(memories, facts),
        system=REFLECT_SYSTEM,
        max_tokens=1024,
        temperature=0.3,
    )
    insights = _parse_insights(raw)
    saved = save_insights(insights, user_id, source) if (save and insights) else 0

    logger.info(
        "🧠 Reflect 完成: user=%s source=%s 提炼=%d 落库=%d",
        user_id, source, len(insights), saved,
    )
    return {
        "status": "ok",
        "insights": insights,
        "saved": saved,
        "source": source,
        "llm_used": raw is not None,
    }


def get_reflections(user_id: str = DEFAULT_USER_ID, limit: int = 20, insight_type: str = "") -> list[dict]:
    """查询已落库的洞察（新的在前）。"""
    ensure_reflect_schema()
    conn = get_facts_conn()
    try:
        sql = "SELECT * FROM reflections WHERE user_id=?"
        params: list[Any] = [user_id]
        if insight_type:
            sql += " AND insight_type=?"
            params.append(insight_type)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 200)))
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        for row in rows:
            try:
                row["evidence"] = json.loads(row.get("evidence") or "[]")
            except json.JSONDecodeError:
                row["evidence"] = []
            row["type_label"] = INSIGHT_LABELS.get(row.get("insight_type", ""), row.get("insight_type"))
        return rows
    finally:
        conn.close()


def inject_reflections(user_id: str = DEFAULT_USER_ID, limit: int = 5) -> str:
    """把最近洞察格式化为可注入上下文的文本（P0-3 验收：后续对话可引用）。"""
    rows = get_reflections(user_id, limit=limit)
    if not rows:
        return ""
    lines = ["[Reflections · 近期反思洞察]"]
    for r in rows:
        label = INSIGHT_LABELS.get(r["insight_type"], r["insight_type"])
        conf = f" (置信度 {r['confidence']:.2f})" if r.get("confidence") else ""
        lines.append(f"- [{label}]{conf}: {r['content']}")
    return "\n".join(lines)


def reflect_background_loop() -> None:
    """后台线程：每 REFLECT_INTERVAL_HOURS 小时主动反思一次。"""
    if not REFLECT_ENABLED:
        logger.info("🧠 Reflect 后台主动反思已禁用（AIDUMEM_REFLECT_ENABLED=false）")
        return
    logger.info(f"🧠 Reflect 后台反思线程启动（间隔 {REFLECT_INTERVAL_HOURS}h）")
    # 先睡一个间隔再反思：避免服务每次重启都立刻消耗一次 LLM 调用。
    # 手动 POST /reflect 不受此影响。
    try:
        time.sleep(REFLECT_INTERVAL_HOURS * 3600)
    except Exception:
        pass
    while True:
        try:
            report = run_reflect(source="background")
            if report.get("llm_used"):
                logger.info(
                    "[reflect-bg] 提炼 %d 条洞察，落库 %d",
                    len(report.get("insights", [])), report.get("saved", 0),
                )
        except Exception as e:
            logger.error(f"[reflect-bg] 反思异常: {e}", exc_info=True)
        time.sleep(REFLECT_INTERVAL_HOURS * 3600)
