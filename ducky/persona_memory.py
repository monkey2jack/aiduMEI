"""
ducky.persona_memory — 人格记忆基座（v19.0 · Persona Memory Layer）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
借鉴 MemoryForge《Synthesize Lifelong Memory for Human-Like LLM Agents》
的「memory-based conditioning」范式：把一句话人设展开成一整套可按情境
检索的自传体记忆库，替代每轮硬塞同一张静态人设卡。

与 v19.0 运营记忆的关系（两层并行，不冲突）：
    · 运营记忆（engine / reflect / self_edit）——持续生长的「活记忆」
    · 人格记忆基座（本模块）——离线构建的「人生底座」，相对静态
    两者通过各自检索接口对上层透明，上层按情境混排注入。

双模式（由 build(mode=...) 切换）：
    · synthesis（合成）——面向虚构角色：从简短人设自动生成 L/G/E 三层
    · grounded（真实）——面向真实人格：只允许从用户提供的真实素材中
      抽取与组织记忆，禁止任何虚构生成（每条记忆可回溯到 source_ref）

三层结构（论文 Mπ = (L, G, E)）：
    L 生平期 life periods → G 一般事件 general events → E 具体经历
    event-specific experiences，对应人类自传体记忆的三级抽象。

护栏（真实模式必做）：
    · LLM 只能「重组织素材」，不得编造；输出无 source_ref 的条目一律丢弃
    · 每条记忆带 provenance（synthesis / grounded）+ source_ref，可审计
    · 基座离线构建、版本化、可回滚（rollback 切回旧版本，数据不删）

配置：
    AIDUMEM_PERSONA_ENABLED=false 可整体关闭路由注册（默认 true）
    AIDUMEM_PERSONA_MAX_MEMORIES  单基座最大记忆条数（默认 2000，防爆表）
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

from ducky.utils import DATA_DIR, _get_thread_conn

logger = logging.getLogger("aiduMEM.persona_memory")

PERSONA_DB = os.path.join(DATA_DIR, "persona.db")

PERSONA_ENABLED = os.environ.get("AIDUMEM_PERSONA_ENABLED", "true").strip().lower() not in {
    "0", "false", "no", "off",
}
_MAX_MEMORIES = 2000

VALID_LEVELS = ("L", "G", "E")
LEVEL_LABELS = {"L": "生平期", "G": "一般事件", "E": "具体经历"}
LEVEL_WEIGHTS = {"E": 1.0, "G": 0.75, "L": 0.5}  # 具体经历 > 一般事件 > 生平期

_SYNTH_SYSTEM = (
    "你是 aiduMEI 的人格记忆基座构建引擎（MemoryForge 方法）。"
    "根据给定人设，生成一套按情境可检索的自传体记忆库。"
    "只输出一个 JSON 对象，不要输出任何解释文字。"
)

_GROUNDED_SYSTEM = (
    "你是 aiduMEI 的人格记忆基座构建引擎（真实模式）。"
    "你只能从用户提供的真实素材中提取与组织记忆，严禁编造任何素材中不存在的事实。"
    "每条记忆必须给出 source_ref（对应素材段落编号），没有 source_ref 的条目会被丢弃。"
    "只输出一个 JSON 对象，不要输出任何解释文字。"
)

_SYNTH_USER = """人设：
{persona_card}

请按自传体记忆三级抽象生成记忆库，输出 JSON：
{{
  "life_periods": [
    {{"age_range": "0-6岁", "theme": "童年", "developmental_tasks": "建立安全依恋", "pressures": "", "milestones": ["出生", "入学"]}}
  ],
  "general_events": [
    {{"title": "示例标题", "frequency": "多次", "first_person_summary": "第一人称摘要", "life_period_ref": "对应 age_range", "theme": ""}}
  ],
  "experiences": [
    {{"scene": "场景描述", "multi_turn_trace": "当时的互动片段", "participants": ["人物"], "thought": "当时的想法", "age_range": "", "recorded_at": "YYYY-MM 或 YYYY"}}
  ]
}}

要求：
1. life_periods 3-6 段、general_events 5-12 条、experiences 6-20 条，宁缺毋滥
2. 排除 3 岁前（童年失忆）的「具体经历」；高保真细节集中在最近 5 年
3. 全部使用第一人称，语气与该人设一致
4. 里程碑要锚定人设关键节点，防止人设漂移
5. 只输出 JSON 对象"""

_GROUNDED_USER = """真实素材（按段落编号）：
{material}

请从素材中提取自传体记忆，输出 JSON：
{{
  "life_periods": [
    {{"age_range": "区间", "theme": "主题", "developmental_tasks": "发展任务", "pressures": "", "milestones": ["里程碑"], "source_refs": ["素材段落编号"]}}
  ],
  "general_events": [
    {{"title": "标题", "frequency": "频率", "first_person_summary": "第一人称摘要", "life_period_ref": "对应 age_range", "source_refs": ["素材段落编号"]}}
  ],
  "experiences": [
    {{"scene": "场景", "multi_turn_trace": "片段", "participants": ["人物"], "thought": "想法", "age_range": "", "recorded_at": "时间", "source_refs": ["素材段落编号"]}}
  ]
}}

铁律：
1. 只能基于素材原文组织记忆，禁止编造素材中不存在的事件、时间、人物
2. 每个条目必须带 source_refs（对应素材段落编号），编号必须来自上述素材
3. 素材没提到的维度留空字符串，不得脑补
4. 只输出 JSON 对象"""

_checked = False


def _get_conn():
    return _get_thread_conn(PERSONA_DB)


def ensure_persona_schema() -> None:
    """幂等建 persona 基座表（独立 persona.db，不混入 facts.db）。"""
    global _checked
    if _checked:
        return
    conn = _get_conn()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS persona_banks (
                bank_id        INTEGER PRIMARY KEY AUTOINCREMENT,
                persona_key    TEXT NOT NULL,
                persona_name   TEXT NOT NULL,
                mode           TEXT NOT NULL,          -- synthesis | grounded
                persona_card   TEXT DEFAULT '',
                source_material TEXT DEFAULT '',
                version        INTEGER NOT NULL DEFAULT 1,
                status         TEXT NOT NULL DEFAULT 'ready',  -- building|ready|superseded|failed
                counts_l       INTEGER DEFAULT 0,
                counts_g       INTEGER DEFAULT 0,
                counts_e       INTEGER DEFAULT 0,
                build_ms       INTEGER DEFAULT 0,
                created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_persona_banks_key ON persona_banks(persona_key)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS persona_memories (
                mem_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                bank_id     INTEGER NOT NULL,
                level       TEXT NOT NULL,             -- L | G | E
                content     TEXT NOT NULL,             -- 检索主文本（第一人称自传体）
                payload     TEXT DEFAULT '{}',         -- 各层特有字段 JSON
                source_refs TEXT DEFAULT '',           -- 真实模式溯源：JSON 数组
                provenance  TEXT NOT NULL DEFAULT 'synthesis',
                age_range   TEXT DEFAULT '',
                theme       TEXT DEFAULT '',
                recorded_at TEXT DEFAULT '',           -- 记忆对应的人生时间（非构建时间）
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_persona_mem_bank ON persona_memories(bank_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_persona_mem_level ON persona_memories(bank_id, level)"
        )
        conn.commit()
        _checked = True
    except Exception as e:
        logger.warning(f"persona 表初始化失败（服务继续）: {e}")
    finally:
        conn.close()


# ── JSON 解析（与 memory_types._llm_classify 同款：先直 parse 再剥围栏）──
def _parse_json(raw: str) -> Optional[dict]:
    if not raw:
        return None
    text = raw.strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return None


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _as_str(value: Any) -> str:
    return str(value or "").strip()


def _slugify(raw: str) -> str:
    """人设 key 规范化：ASCII 保留，中文/非 ASCII 用稳定短哈希，防相互覆盖。"""
    import hashlib
    text = (raw or "persona").strip().lower()
    ascii_part = "".join(ch for ch in text if ch.isascii() and (ch.isalnum() or ch == "-"))
    ascii_part = re.sub(r"-+", "-", ascii_part).strip("-")
    if not re.search(r"[a-z0-9]", ascii_part):
        ascii_part = "persona-" + hashlib.md5(text.encode()).hexdigest()[:10]
    return ascii_part[:48] or "persona"


# ── 检索打分：中文 bigram + 英文词 的命中率（零第三方依赖，离线可用）──
def _tokenize(text: str) -> set:
    text = (text or "").lower()
    tokens = set(re.findall(r"[a-z0-9]+", text))
    # 中文 2-gram
    zh = re.findall(r"[一-鿿]+", text)
    for seg in zh:
        tokens.update(seg[i:i + 2] for i in range(len(seg) - 1))
        tokens.update(seg)  # 整词命中权重更高
    return tokens


def _score_memory(query: str, content: str) -> float:
    """关键词命中率打分（0-1）。命中越多、越具体分越高。"""
    q_tokens = _tokenize(query)
    c_tokens = _tokenize(content)
    if not q_tokens:
        return 0.0
    hits = q_tokens & c_tokens
    # 命中率为主，命中绝对数做轻微加成
    coverage = len(hits) / len(q_tokens)
    bonus = min(len(hits) / 10.0, 0.2)
    return min(1.0, coverage * 0.8 + bonus)


def _norm_refs(value: Any) -> list[str]:
    """source_refs 可能是 str/list，统一成 list。"""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if value:
        return [str(value).strip()]
    return []


def _has_valid_source(item: dict) -> bool:
    refs = _norm_refs(item.get("source_refs"))
    return bool(refs)


# ── 构建流程：Context Generator → Life Organizer → Multi-Resolution Simulator ──
def build_persona(
    persona_card: str,
    *,
    mode: str = "synthesis",
    persona_key: str = "",
    persona_name: str = "",
    source_material: str = "",
    use_llm: bool = True,
) -> dict:
    """构建一套人格记忆基座（离线、版本化、可回滚）。

    Args:
        persona_card: 简短人设（合成模式的唯一输入；真实模式下可留空）
        mode: synthesis（合成，可虚构）| grounded（真实，只抽取不虚构）
        persona_key: 唯一标识（省略则从 persona_name/card 推导）
        persona_name: 展示名
        source_material: 真实模式素材原文（按行/段落编号后喂给 LLM）
        use_llm: 是否调用 LLM；False 时合成模式走规则降级，真实模式拒绝构建
    """
    ensure_persona_schema()
    mode = mode if mode in ("synthesis", "grounded") else "synthesis"
    name = _as_str(persona_name) or _as_str(persona_card)[:24] or "未命名人格"
    key = _slugify(persona_key or name)

    # 先把旧基座标记 superseded（保留数据，天然可回滚）
    conn = _get_conn()
    try:
        prev = conn.execute(
            "SELECT MAX(version) AS v FROM persona_banks WHERE persona_key=?", (key,)
        ).fetchone()
        next_version = int(prev["v"] or 0) + 1
        conn.execute(
            "UPDATE persona_banks SET status='superseded', updated_at=CURRENT_TIMESTAMP "
            "WHERE persona_key=? AND status='ready'", (key,)
        )
        cur = conn.execute(
            "INSERT INTO persona_banks (persona_key, persona_name, mode, persona_card, source_material, version, status) "
            "VALUES (?,?,?,?,?,?, 'building')",
            (key, name, mode, _as_str(persona_card), _as_str(source_material), next_version),
        )
        bank_id = int(cur.lastrowid or 0)
        conn.commit()
        conn.close()
    except Exception as e:
        try:
            conn.close()
        except Exception as close_err:
            logger.debug(f"build_persona: close after failure suppressed: {close_err}")
        return {"status": "error", "detail": f"基座建档失败: {e}"}

    start = time.time()
    data: Optional[dict] = None
    llm_used = False

    if mode == "grounded" and not _as_str(source_material):
        _mark_failed(bank_id)
        return {"status": "error", "detail": "真实模式（grounded）必须提供 source_material 素材", "bank_id": bank_id}

    if use_llm:
        try:
            from ducky.llm_client import call_llm
            if mode == "synthesis":
                prompt = _SYNTH_USER.format(persona_card=_as_str(persona_card) or name)
                system = _SYNTH_SYSTEM
            else:
                prompt = _GROUNDED_USER.format(material=_numbered_material(source_material))
                system = _GROUNDED_SYSTEM
            raw = call_llm(prompt, system=system, max_tokens=2400, temperature=0.4)
            data = _parse_json(raw or "")
            llm_used = data is not None
        except Exception as e:
            logger.warning(f"persona LLM 构建失败（降级）: {e}")

    if data is None:
        if mode == "grounded":
            _mark_failed(bank_id)
            return {"status": "error", "detail": "真实模式 LLM 构建失败，拒绝规则降级（防虚构）", "bank_id": bank_id}
        # 合成模式规则降级：从人设卡提取若干关键句作为生平期/事件
        data = _rule_fallback(persona_card or name)

    # 落库（grounded 模式在此做零幻觉硬校验：无 source_ref 的条目丢弃）
    try:
        counts = _store_memories(bank_id, data, mode=mode)
    except Exception as e:
        _mark_failed(bank_id)
        return {"status": "error", "detail": f"记忆落库失败: {e}", "bank_id": bank_id}

    build_ms = int((time.time() - start) * 1000)
    conn = _get_conn()
    try:
        conn.execute(
            "UPDATE persona_banks SET status='ready', counts_l=?, counts_g=?, counts_e=?, "
            "build_ms=?, updated_at=CURRENT_TIMESTAMP WHERE bank_id=?",
            (counts["L"], counts["G"], counts["E"], build_ms, bank_id),
        )
        conn.commit()
    finally:
        conn.close()

    logger.info(
        "👤 persona_memory: 构建完成 key=%s v%d mode=%s L/G/E=%d/%d/%d（%dms, llm=%s）",
        key, next_version, mode, counts["L"], counts["G"], counts["E"], build_ms, llm_used,
    )
    return {
        "status": "ok",
        "bank_id": bank_id,
        "persona_key": key,
        "persona_name": name,
        "mode": mode,
        "version": next_version,
        "counts": counts,
        "total": counts["L"] + counts["G"] + counts["E"],
        "llm_used": llm_used,
        "build_ms": build_ms,
    }


def _numbered_material(material: str) -> str:
    """把素材按非空行编号，供 LLM 引用 source_ref。"""
    lines = [ln.strip() for ln in (_as_str(material)).splitlines() if ln.strip()]
    if not lines:
        return "（无素材）"
    return "\n".join(f"[{i + 1}] {ln}" for i, ln in enumerate(lines))


def _mark_failed(bank_id: int) -> None:
    try:
        conn = _get_conn()
        conn.execute(
            "UPDATE persona_banks SET status='failed', updated_at=CURRENT_TIMESTAMP WHERE bank_id=?",
            (bank_id,),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"_mark_failed: 标记失败状态未落库: {e}")


def _rule_fallback(persona_card: str) -> dict:
    """合成模式规则降级：LLM 不可用时，从人设卡里拆句子作为 G 层事件。"""
    card = _as_str(persona_card)
    periods = [{
        "age_range": "成年", "theme": "当前人生阶段",
        "developmental_tasks": card[:80], "pressures": "", "milestones": [card[:40]],
    }]
    events = []
    for i, clause in enumerate(re.split(r"[。；;\n]", card), 1):
        clause = clause.strip()
        if len(clause) >= 6:
            events.append({
                "title": clause[:24], "frequency": "持续",
                "first_person_summary": clause[:160],
                "life_period_ref": "成年", "theme": "",
            })
        if len(events) >= 8:
            break
    return {"life_periods": periods, "general_events": events, "experiences": []}


def _store_memories(bank_id: int, data: dict, *, mode: str) -> dict[str, int]:
    """把 LLM 输出落库。grounded 模式丢弃所有无 source_ref 的条目（零幻觉硬校验）。"""
    conn = _get_conn()
    counts = {"L": 0, "G": 0, "E": 0}
    total = 0
    try:
        for level, key_name in (("L", "life_periods"), ("G", "general_events"), ("E", "experiences")):
            for item in _as_list(data.get(key_name)):
                if not isinstance(item, dict):
                    continue
                if total >= _MAX_MEMORIES:
                    break
                content, payload, source_refs = _render_item(level, item)
                if not content:
                    continue
                # 真实模式护栏：无 source_ref 的条目直接丢弃
                if mode == "grounded" and not source_refs:
                    logger.debug("persona grounded: 丢弃无 source_ref 的条目 → %s", content[:60])
                    continue
                conn.execute(
                    "INSERT INTO persona_memories "
                    "(bank_id, level, content, payload, source_refs, provenance, age_range, theme, recorded_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        bank_id, level, content,
                        json.dumps(payload, ensure_ascii=False),
                        json.dumps(source_refs, ensure_ascii=False),
                        "grounded" if mode == "grounded" else "synthesis",
                        _as_str(item.get("age_range")),
                        _as_str(item.get("theme")),
                        _as_str(item.get("recorded_at")),
                    ),
                )
                counts[level] += 1
                total += 1
            if total >= _MAX_MEMORIES:
                break
        conn.commit()
        return counts
    finally:
        conn.close()


def _render_item(level: str, item: dict) -> tuple[str, dict, list[str]]:
    """把 LLM 的某层条目渲染成「检索主文本 + payload + source_refs」。"""
    source_refs = _norm_refs(item.get("source_refs"))
    payload = dict(item)
    if level == "L":
        theme = _as_str(item.get("theme"))
        tasks = _as_str(item.get("developmental_tasks"))
        milestones = "、".join(_as_list(item.get("milestones"))[:8])
        content = f"{_as_str(item.get('age_range'))} · {theme}"
        if tasks:
            content += f"：{tasks}"
        if milestones:
            content += f"（里程碑：{milestones}）"
        return content, payload, source_refs
    if level == "G":
        title = _as_str(item.get("title"))
        summary = _as_str(item.get("first_person_summary"))
        content = f"{title}：{summary}" if title else summary
        return content, payload, source_refs
    # E
    scene = _as_str(item.get("scene"))
    trace = _as_str(item.get("multi_turn_trace"))
    thought = _as_str(item.get("thought"))
    parts = [p for p in (scene, trace, thought) if p]
    content = "。".join(parts)
    return content, payload, source_refs


# ── 检索接口 ─────────────────────────────────────────────
def retrieve_persona(
    situation: str,
    *,
    persona_key: str = "",
    bank_id: int = 0,
    k: int = 5,
    level: str = "",
) -> dict:
    """按当前情境检索人格记忆片段（替代整卡注入的 dynamic conditioning）。

    Args:
        situation: 当前情境描述（技术咨询 / 情感对话 / 决策…）
        persona_key / bank_id: 二选一定位基座（bank_id 优先）
        k: 返回条数
        level: 可选只取某层（L/G/E）
    """
    ensure_persona_schema()
    bank = _resolve_bank(persona_key=persona_key, bank_id=bank_id)
    if not bank:
        return {"status": "error", "detail": "未找到 ready 状态的基座", "results": []}

    conn = _get_conn()
    try:
        sql = "SELECT * FROM persona_memories WHERE bank_id=?"
        params: list[Any] = [bank["bank_id"]]
        if level in VALID_LEVELS:
            sql += " AND level=?"
            params.append(level)
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()

    scored = []
    for row in rows:
        raw_score = _score_memory(situation, row["content"])
        if raw_score <= 0 and _as_str(situation).strip():
            continue
        layer_weight = LEVEL_WEIGHTS.get(row["level"], 0.5)
        row["score"] = round(min(1.0, raw_score * layer_weight), 4)
        row["level_label"] = LEVEL_LABELS.get(row["level"], row["level"])
        try:
            row["payload"] = json.loads(row.get("payload") or "{}")
        except json.JSONDecodeError:
            row["payload"] = {}
        try:
            row["source_refs"] = json.loads(row.get("source_refs") or "[]")
        except json.JSONDecodeError:
            row["source_refs"] = []
        scored.append(row)

    scored.sort(key=lambda r: r["score"], reverse=True)
    top = scored[: max(1, min(int(k), 50))]

    context = _format_context(top)
    return {
        "status": "ok",
        "bank_id": bank["bank_id"],
        "persona_key": bank["persona_key"],
        "persona_name": bank["persona_name"],
        "mode": bank["mode"],
        "count": len(top),
        "results": top,
        "context": context,
    }


def _resolve_bank(*, persona_key: str, bank_id: int) -> Optional[dict]:
    conn = _get_conn()
    try:
        if bank_id:
            row = conn.execute(
                "SELECT * FROM persona_banks WHERE bank_id=? AND status='ready'", (bank_id,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM persona_banks WHERE persona_key=? AND status='ready' "
                "ORDER BY version DESC LIMIT 1", (_slugify(persona_key),)
            ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _format_context(results: list[dict]) -> str:
    """把检索结果格式化为可注入上下文的文本。"""
    if not results:
        return ""
    lines = ["[Persona Memory · 情境相关人格记忆]"]
    for r in results:
        meta = f"{r['level_label']}"
        if r.get("age_range"):
            meta += f" · {r['age_range']}"
        if r.get("recorded_at"):
            meta += f" · {r['recorded_at']}"
        lines.append(f"- [{meta}] {r['content'][:200]}")
    return "\n".join(lines)


# ── 查询 / 回滚 ─────────────────────────────────────────
def list_banks(persona_key: str = "", status: str = "") -> list[dict]:
    ensure_persona_schema()
    conn = _get_conn()
    try:
        sql = "SELECT * FROM persona_banks"
        clauses, params = [], []
        if persona_key:
            clauses.append("persona_key=?")
            params.append(_slugify(persona_key))
        if status:
            clauses.append("status=?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY version DESC LIMIT 50"
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        for r in rows:
            r["total"] = r["counts_l"] + r["counts_g"] + r["counts_e"]
        return rows
    finally:
        conn.close()


def get_bank_detail(bank_id: int) -> dict:
    ensure_persona_schema()
    conn = _get_conn()
    try:
        bank = conn.execute("SELECT * FROM persona_banks WHERE bank_id=?", (bank_id,)).fetchone()
        if not bank:
            return {"status": "error", "detail": f"bank_id={bank_id} 不存在"}
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM persona_memories WHERE bank_id=? ORDER BY level, mem_id", (bank_id,)
        ).fetchall()]
        for r in rows:
            r["level_label"] = LEVEL_LABELS.get(r["level"], r["level"])
            try:
                r["payload"] = json.loads(r.get("payload") or "{}")
            except json.JSONDecodeError:
                r["payload"] = {}
            try:
                r["source_refs"] = json.loads(r.get("source_refs") or "[]")
            except json.JSONDecodeError:
                r["source_refs"] = []
        return {"status": "ok", "bank": dict(bank), "memories": rows, "count": len(rows)}
    finally:
        conn.close()


def rollback_persona(persona_key: str, to_version: int) -> dict:
    """回滚到指定版本：把该版本设为 ready，其余版本置 superseded（数据不删）。"""
    ensure_persona_schema()
    key = _slugify(persona_key)
    conn = _get_conn()
    try:
        target = conn.execute(
            "SELECT * FROM persona_banks WHERE persona_key=? AND version=?", (key, to_version)
        ).fetchone()
        if not target:
            return {"status": "error", "detail": f"版本 {to_version} 不存在"}
        conn.execute(
            "UPDATE persona_banks SET status='superseded', updated_at=CURRENT_TIMESTAMP "
            "WHERE persona_key=? AND status='ready'", (key,)
        )
        conn.execute(
            "UPDATE persona_banks SET status='ready', updated_at=CURRENT_TIMESTAMP WHERE bank_id=?",
            (target["bank_id"],),
        )
        conn.commit()
        return {
            "status": "ok",
            "persona_key": key,
            "restored_version": to_version,
            "bank_id": target["bank_id"],
        }
    finally:
        conn.close()


def get_persona_context(persona_key: str, situation: str = "", k: int = 5) -> str:
    """便捷入口：给下游 agent 注入情境相关人格记忆（供对话前调用）。"""
    result = retrieve_persona(situation or "当前对话", persona_key=persona_key, k=k)
    return result.get("context", "")
