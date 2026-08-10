"""
ducky.legacy_routes — 提取自 api_server.py §5-§10
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
把 629 行 SQLite legacy 端点从 api_server 主模块分离，
保持向后兼容，所有端点注册到同一个 FastAPI app 上。

v8 重构 (2026-07-13):
  - register_legacy_routes(app) → 一次性注册全部 legacy 端点
  - 导出 helper 函数供 api_server §11-§14 引用
"""

import json, logging, os, sqlite3, time, re
import datetime as _dt
from typing import Optional
from datetime import datetime, timezone
from collections import defaultdict

from fastapi import HTTPException, Form, Query

from ducky.utils import (
    DEFAULT_USER_ID,
    DATA_DIR,
    FACTS_DB,
    OBS_DB,
    SCENES_DB,
    _get_thread_conn,
)

# Pantheon v13：旧 /facts/add 端点也走统一分层，享受铁律零衰减。
# 只读工具函数 + 常量，无循环导入风险（federation.tier/schema 不反向依赖 hot.legacy）。
from ducky.federation import tier as _pantheon_tier
from ducky.federation.schema import (
    DEFAULT_AGENT as _PANTHEON_DEFAULT_AGENT,
    DEFAULT_PROFILE as _PANTHEON_DEFAULT_PROFILE,
)

logger = logging.getLogger("aiduMEM.legacy")

# ═══════════════════════════════════════════════
# 路径常量 — DB 路径统一从 ducky.utils 导入（单一真源）
# ═══════════════════════════════════════════════
TAGS_FILE     = os.path.join(DATA_DIR, "tags.json")
SKILL_PATTERNS_FILE = os.path.join(DATA_DIR, "skill_patterns.json")

# ═══════════════════════════════════════════════
# §5  DB 连接工厂 — 统一委托 ducky.utils 线程本地连接池
# ═══════════════════════════════════════════════
def _get_db(path: str) -> sqlite3.Connection:
    """统一 DB 连接工厂 — 线程本地复用 + WAL + row_factory=Row

    v12.0.1 重构：不再自建连接，委托 ducky.utils._get_thread_conn，
    消除「legacy 一套 + utils 一套」的双连接体系。
    返回的是 _ConnProxy：close() 是 no-op（连接线程内复用），commit() 正常透传，
    所以 legacy 里既有的 conn.close() / conn.commit() 调用点全部无需改动。
    """
    if os.path.dirname(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    return _get_thread_conn(path)

def _get_facts_conn():  return _get_db(FACTS_DB)
def _get_obs_conn():    return _get_db(OBS_DB)
def _get_scenes_conn(): return _get_db(SCENES_DB)

# ═══════════════════════════════════════════════
# §6  共享 helper（_extract_entities, _extract_key_facts 等）
#     这些也被 §11-§13 的活跃端点使用，导出供 api_server 引用
# ═══════════════════════════════════════════════
_RE_QUOTED      = re.compile(r'["\\\']([^"\\\']+)["\\\']')
_RE_AKA         = re.compile(r'(\w+(?:\s+\w+)*)\s+(?:aka|also known as)\s+(\w+(?:\s+\w+)*)', re.IGNORECASE)
_RE_USER        = re.compile(r'\b(User|用户)\b')
_RE_AI          = re.compile(r'\b(AI|Assistant|助手)\b')
_RE_PROJECT     = re.compile(r'\b(aiduMEM|mem0|Hermes|Qdrant|FastAPI|SQLite)\b', re.IGNORECASE)
_RE_TECH        = re.compile(r'\b(Hermes|Qdrant|NVIDIA|FTS5|WAL|VACUUM|nginx|systemd|FastAPI)\b')


def _env_pattern(env_key: str) -> "re.Pattern | None":
    """从环境变量读取 `|` 分隔的自定义词表，编译成实体识别正则。

    部署方专属的机房代号、纪念日、人名等不硬编码在源码里，例如：
        export AIDUMEM_SERVER_KEYWORDS="tokyo|osaka"
        export AIDUMEM_DATE_KEYWORDS="1月1日|周年"
    未设置时返回 None，该类实体不参与提取。
    """
    raw = (os.environ.get(env_key) or "").strip().strip("|")
    if not raw:
        return None
    words = [re.escape(w.strip()) for w in raw.split("|") if w.strip()]
    if not words:
        return None
    return re.compile("(" + "|".join(words) + ")")


# 可选的部署方自定义实体（服务器代号 / 纪念日期）
#
# 血训（v15）：这两个正则原本在 import 时就固化，一旦模块比 setenv 先加载
# （或 systemd 单元漏了 Environment=），自定义实体就永久不参与抽取，
# 且全程静默。改为惰性 + 环境变量变化自动重建。
_ENV_PATTERN_CACHE: dict = {}


def _env_pattern_cached(env_key: str) -> "re.Pattern | None":
    raw = (os.environ.get(env_key) or "").strip().strip("|")
    cached = _ENV_PATTERN_CACHE.get(env_key)
    if cached is None or cached[0] != raw:
        _ENV_PATTERN_CACHE[env_key] = (raw, _env_pattern(env_key))
    return _ENV_PATTERN_CACHE[env_key][1]

def _extract_entities(text: str) -> list[str]:
    """原生的实体提取 — 保持首次出现顺序去重"""
    seen, candidates = set(), []
    def _add(name):
        n = name.strip()
        if n and n.lower() not in seen: seen.add(n.lower()); candidates.append(n)
    for m in _RE_QUOTED.finditer(text):       _add(m.group(1))
    for m in _RE_AKA.finditer(text):          _add(m.group(1)); _add(m.group(2))
    _re_server = _env_pattern_cached("AIDUMEM_SERVER_KEYWORDS")
    _re_date = _env_pattern_cached("AIDUMEM_DATE_KEYWORDS")
    for pat in [_RE_USER, _RE_AI, _RE_PROJECT, _re_server, _RE_TECH, _re_date]:
        if pat is None: continue
        for m in pat.finditer(text):          _add(m.group(0))
    return candidates

def _extract_key_facts(category: str, limit: int = 100) -> list:
    conn = _get_facts_conn()
    rows = conn.execute("SELECT * FROM facts WHERE archived=0 AND category=? ORDER BY updated_at DESC LIMIT ?",
                       (category, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def _auto_extract_and_link(fact_id: int, text: str, conn=None) -> list[str]:
    """新增 fact 后自动提取实体并链接"""
    entities = _extract_entities(text)
    if not entities: return []
    should_close = conn is None
    if should_close: conn = _get_facts_conn()
    cur = conn.cursor()
    linked = []
    for ent_name in entities:
        row = cur.execute("SELECT entity_id FROM entities WHERE name=?", (ent_name,)).fetchone()
        if row: eid = row[0]
        else:
            cur.execute("INSERT INTO entities (name,entity_type) VALUES (?,'auto')", (ent_name,))
            eid = cur.lastrowid
        try:
            cur.execute("INSERT OR IGNORE INTO fact_entities (fact_id,entity_id) VALUES (?,?)", (fact_id, eid))
            linked.append(ent_name)
        except Exception: pass
    conn.commit()
    if should_close: conn.close()
    return linked

# L0/L1 分级词表可由部署方通过环境变量覆盖（逗号分隔）
#   AIDUMEM_L0_CATEGORIES  完全匹配的 L0 category，默认「铁律,暗号,认证」
#   AIDUMEM_L1_PREFIXES    前缀匹配的 L1 category，默认空
_L0_CATEGORIES = frozenset(
    c.strip() for c in os.environ.get(
        "AIDUMEM_L0_CATEGORIES", "铁律,暗号,认证").split(",") if c.strip()
)
_L1_PREFIXES = tuple(
    p.strip() for p in os.environ.get("AIDUMEM_L1_PREFIXES", "").split(",") if p.strip()
)


def _auto_detect_level(category: str) -> str:
    if category in _L0_CATEGORIES: return "L0"
    if _L1_PREFIXES and category.startswith(_L1_PREFIXES): return "L1"
    return "L2"

def _vault_refine(category, fact_key, fact_value, level):
    """vault-write 强制提炼占位（可扩展 LLM 提炼）"""
    return {"status": "noop"}

# ── 6.5  Feedback ──
def _fact_feedback_impl(fact_id: int, helpful: bool):
    try:
        conn = _get_facts_conn()
        cur = conn.cursor()
        cur.execute("SELECT category,trust_score FROM facts WHERE id=?", (fact_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            raise HTTPException(404, f"fact_id={fact_id} 不存在")
        if row["category"] in _L0_CATEGORIES:
            conn.close()
            return {"status":"ok","message":"铁律类不受feedback影响","noop":True}
        old_trust = row["trust_score"]
        delta = 0.10 if helpful else -0.15
        new_trust = min(1.0, max(0.0, old_trust + delta))
        col = "helpful_count" if helpful else "unhelpful_count"
        cur.execute(f"UPDATE facts SET trust_score=?,{col}={col}+1,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                   (new_trust, fact_id))
        conn.commit()
        cur.execute("SELECT helpful_count,unhelpful_count,trust_score FROM facts WHERE id=?", (fact_id,))
        updated = cur.fetchone()
        conn.close()
        return {"status":"ok","fact_id":fact_id,"trust_before":old_trust,"trust_after":updated["trust_score"],
                "helpful_count":updated["helpful_count"],"unhelpful_count":updated["unhelpful_count"],
                "delta":round(updated["trust_score"]-old_trust,3)}
    except HTTPException: raise
    except Exception as e:
        logger.error(f"feedback 失败: {e}")
        raise HTTPException(500, str(e))

# ── 6.6  矛盾检测 v1 ──
CONTRADICTION_WORDS = [
    ("启用","关闭","禁用"),("开启","关闭","禁用"),("成功","失败","挂了"),
    ("升级","降级","回滚"),("新建","删除","移除"),("开启","停止","暂停"),
    ("正常","异常","故障"),("可用","不可用","不可达"),("通过","失败","未通过"),
]

# ── tags ──
def _load_tags() -> dict:
    if os.path.exists(TAGS_FILE):
        try: return json.loads(open(TAGS_FILE).read())
        except (json.JSONDecodeError, OSError): pass
    return {}

def _save_tags(tags: dict):
    os.makedirs(os.path.dirname(TAGS_FILE), exist_ok=True)
    with open(TAGS_FILE, "w") as f: json.dump(tags, f, ensure_ascii=False, indent=2)

def _extract_tags_from_text(text: str, max_tags: int = 3) -> list[str]:
    words = re.findall(r'[a-zA-Z0-9_-]{2,}|[\u4e00-\u9fa5]{2,4}', text)
    stopwords = {"的","了","是","在","有","和","就","不","人","都","一个","可以","这个","那个","什么","没有","我们","他们","自己","因为","所以","但是","如果","虽然","而且","或者"}
    return [w for w in words if w not in stopwords][:max_tags]

# ── skill patterns ──
def _load_patterns() -> dict:
    if os.path.exists(SKILL_PATTERNS_FILE):
        with open(SKILL_PATTERNS_FILE) as f:
            content = f.read().strip()
            return json.loads(content) if content else {}
    return {}

def _save_patterns(patterns: dict):
    os.makedirs(os.path.dirname(SKILL_PATTERNS_FILE), exist_ok=True)
    with open(SKILL_PATTERNS_FILE, "w") as f: json.dump(patterns, f, ensure_ascii=False, indent=2)

# ═══════════════════════════════════════════════
# §7  FTS5 全文搜索 + 混合检索
#     v15.1: 归一到 ducky.text_fts（D 档真源），此处只做 re-export 兼容。
# ═══════════════════════════════════════════════
from ducky.text_fts import (
    _init_text_fts,
    _index_memory,
    _unindex_memory,
    _bm25_keyword_search,
    _like_search,
    _hybrid_search,
)

# ═══════════════════════════════════════════════
# §8  Observations + Reflect（Hindsight 移植）
# ═══════════════════════════════════════════════
def _compute_similarity(text1: str, text2: str) -> float:
    def smart_tokenize(t: str) -> dict:
        chars = list(t); n = len(chars); grams = {}
        for i in range(n-1): g = chars[i]+chars[i+1]; grams[g] = grams.get(g,0)+1
        return grams
    g1, g2 = smart_tokenize(text1), smart_tokenize(text2)
    intersection = sum(min(g1.get(k,0), g2.get(k,0)) for k in set(g1)|set(g2))
    union = sum(max(g1.get(k,0), g2.get(k,0)) for k in set(g1)|set(g2))
    return intersection/union if union > 0 else 0.0

def _get_recent_memories(limit=100, user_id=DEFAULT_USER_ID) -> list:
    try:
        from api_server import get_memory
        mem = get_memory()
        return mem.get_all(filters={"user_id": user_id}, limit=limit)
    except Exception: return []

def _run_consolidation(user_id=DEFAULT_USER_ID, max_obs=50):
    """聚合近期记忆为观察（占位——由 Layer1/Instinct 模块接管）"""
    return {"consolidated": 0, "observations": [], "note": "v7: 由 Layer1 写入自检 + Instinct 毕业接管"}

# ═══════════════════════════════════════════════
# §9  后台循环（导出供 §14 引用）
# ═══════════════════════════════════════════════
def _background_consolidation_loop():
    while True:
        try: _run_consolidation(max_obs=30)
        except Exception as e: logger.error(f"consolidation 后台失败: {e}")
        time.sleep(3600)

def _background_scene_cluster_loop():
    """后台场景聚类——如果 facts 表不存在则静默跳过"""
    first_run = True
    while True:
        try:
            _cluster_scenes_impl(dry_run=False)
        except Exception as e:
            if first_run or "no such table" not in str(e):
                logger.warning(f"scene cluster 跳过: {e}")
        first_run = False
        time.sleep(43200)

def _cluster_scenes_impl(category: str = None, dry_run: bool = True, min_similarity: float = 0.25):
    conn = _get_facts_conn()  # 🔧 修：categories 来自 facts 表，非 scenes
    categories = [category] if category else [r[0] for r in conn.execute(
        "SELECT DISTINCT category FROM facts WHERE archived=0 ORDER BY category").fetchall()]
    clustered = 0
    for cat in categories:
        facts = _extract_key_facts(cat)
        if len(facts) < 2: continue
        for i in range(len(facts)):
            best_score, best_match = 0, None
            for j in range(i):
                sim = _compute_similarity(facts[i].get("fact_value",""), facts[j].get("fact_value",""))
                if sim > best_score: best_score, best_match = sim, facts[j]
            if best_score >= min_similarity:
                clustered += 1
    conn.close()
    return {"status":"ok (dry-run)" if dry_run else "ok","clustered":clustered}


# ═══════════════════════════════════════════════
#  Chronos 双时间轴 — 保守时间锚点抽取
#  借鉴 Mímir v4.0 valid_from/valid_to；纯规则零 LLM。
#  只抽「明确带失效语义 + 明确日期」的事实，不确定一律留空。
# ═══════════════════════════════════════════════
_VALID_TO_KEYWORDS = ("到期", "失效", "过期", "有效期至", "有效期到", "截止", "截至", "expire", "valid until", "valid_to")
_VALID_FROM_KEYWORDS = ("生效", "起效", "自", "从", "起", "starting", "effective", "valid_from")
# YYYY-MM-DD / YYYY-MM / YYYY年M月D日 / YYYY年M月
_DATE_RE = re.compile(
    r"(20\d{2})[-/年\.]\s*(0?[1-9]|1[0-2])(?:[-/月\.]\s*(0?[1-9]|[12]\d|3[01]))?"
)

def _norm_date(m):
    """把正则捕获的年月日归一成 ISO；缺日补 01，缺时区补 UTC 起始。"""
    y, mo, d = m.group(1), m.group(2), m.group(3)
    d = d or "1"
    try:
        return datetime(int(y), int(mo), int(d), tzinfo=timezone.utc).isoformat()
    except ValueError:
        return None

def _extract_validity(text: str):
    """
    保守抽取 (valid_from, valid_to)。
    规则：文本里同时出现「失效关键词」和「日期」→ 填 valid_to；
          出现「生效关键词」和「日期」→ 填 valid_from。
    找不到明确信号一律返回 None，绝不臆测。
    """
    if not text:
        return (None, None)
    low = text.lower()
    valid_from = valid_to = None
    # valid_to：只在命中失效关键词时才抽最近的一个日期
    if any(k in text or k in low for k in _VALID_TO_KEYWORDS):
        for m in _DATE_RE.finditer(text):
            iso = _norm_date(m)
            if iso:
                valid_to = iso  # 取最后一个日期（通常「XX 到期」日期在关键词后）
    # valid_from：命中生效关键词时抽第一个日期
    if any(k in text or k in low for k in _VALID_FROM_KEYWORDS):
        m = _DATE_RE.search(text)
        if m:
            iso = _norm_date(m)
            if iso and iso != valid_to:  # 别把同一个到期日误当生效日
                valid_from = iso
    return (valid_from, valid_to)


# ═══════════════════════════════════════════════
#  register_legacy_routes(app) — 注册全部 legacy 端点
# ═══════════════════════════════════════════════
def register_legacy_routes(app):
    """把 §6-§10 的全部 22 个端点注册到 FastAPI app 上"""

    # ── 6.2  Facts CRUD ──
    @app.get("/facts")
    def list_facts(category: str = None, key: str = None, level: str = "L2"):
        level_norm = (level or "L2").upper()
        if level_norm not in ("L0","L1","L2"): level_norm = "L2"
        conn = _get_facts_conn()
        cur = conn.cursor()
        where, params = [], []
        if category: where.append("category = ?"); params.append(category)
        if key:      where.append("fact_key = ?");   params.append(key)
        sql = "SELECT * FROM facts" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY category, fact_key"
        cur.execute(sql, params)
        raw = [dict(r) for r in cur.fetchall()]
        conn.close()
        if level_norm == "L2": rows = raw
        else:
            rows = []
            for r in raw:
                item = {k:v for k,v in r.items() if k not in ("summary","overview")}
                item["value"] = r.get("summary") if level_norm=="L0" else (r.get("overview") or r["fact_value"])
                rows.append(item)
        return {"status":"ok","count":len(rows),"level":level_norm,"facts":rows}

    @app.post("/facts/add")
    def add_fact(category: str = "general", fact_key: str = "", fact_value: str = "",
                 source: str = DEFAULT_USER_ID, level: str = "",
                 valid_from: str = "", valid_to: str = ""):
        if not fact_key or not fact_value:
            return {"status":"error","detail":"fact_key 和 fact_value 不能为空"}
        resolved_level = level if level else _auto_detect_level(category)
        summary = f"{fact_value[:60]}{'...' if len(fact_value)>60 else ''}"
        overview = fact_value
        # Chronos 双时间轴：显式参数优先，否则保守抽取；抽不出=None（永不过期）
        vf, vt = _extract_validity(f"{fact_key} {fact_value}")
        vf = (valid_from or "").strip() or vf
        vt = (valid_to or "").strip() or vt
        # Pantheon 分层：旧端点也自动分层归属，保证 v12 调用方无需改代码
        # 就能享受铁律零衰减；响应结构只增字段不改语义。
        fed_tier = _pantheon_tier.infer_tier(category, fact_key, fact_value)
        recorded_at = _dt.datetime.now(_dt.timezone.utc)
        decay_at = _pantheon_tier.decay_deadline(fed_tier, recorded_at)
        conn = _get_facts_conn()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO facts (category, fact_key, fact_value, source, summary, overview, level,
                               valid_from, valid_to, agent_id, profile, memory_tier,
                               recorded_at, decay_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(category, fact_key) DO UPDATE SET
                fact_value=excluded.fact_value, source=excluded.source,
                summary=excluded.summary, overview=excluded.overview,
                level=excluded.level, updated_at=CURRENT_TIMESTAMP,
                memory_tier=excluded.memory_tier,
                recorded_at=excluded.recorded_at,
                decay_at=excluded.decay_at,
                valid_from=COALESCE(excluded.valid_from, facts.valid_from),
                valid_to=COALESCE(excluded.valid_to, facts.valid_to)
        """, (category, fact_key, fact_value, source, summary, overview, resolved_level,
              vf, vt, _PANTHEON_DEFAULT_AGENT, _PANTHEON_DEFAULT_PROFILE, fed_tier,
              recorded_at.isoformat(), decay_at))
        conn.commit()
        fid = cur.lastrowid or 0
        auto_link = _auto_extract_and_link(fid, fact_value, conn)
        conn.close()
        return {"status":"ok","message":f"事实已存储: {category}/{fact_key}","level":resolved_level,
                "validity":{"valid_from":vf,"valid_to":vt},
                "memory_tier": fed_tier,
                "refinement":_vault_refine(category, fact_key, fact_value, resolved_level),
                "auto_entities": auto_link}

    @app.get("/facts/categories")
    def list_fact_categories():
        conn = _get_facts_conn()
        rows = conn.execute("SELECT category, COUNT(*) AS cnt FROM facts GROUP BY category ORDER BY category").fetchall()
        conn.close()
        return {"status":"ok","categories":[dict(r) for r in rows]}

    # ── 6.3  实体 API ──
    @app.get("/facts/entities")
    def fact_entities(fact_id: int = None, entity: str = None, limit: int = 20):
        conn = _get_facts_conn()
        cur = conn.cursor()
        if fact_id:
            rows = cur.execute("""
                SELECT e.entity_id, e.name, e.entity_type
                FROM entities e JOIN fact_entities fe ON fe.entity_id=e.entity_id
                WHERE fe.fact_id=? ORDER BY e.name
            """, (fact_id,)).fetchall()
            conn.close()
            return {"status":"ok","fact_id":fact_id,"entities":[dict(r) for r in rows],"count":len(rows)}
        elif entity:
            rows = cur.execute("""
                SELECT f.id, f.category, f.fact_key, f.fact_value, f.trust_score, f.updated_at
                FROM facts f JOIN fact_entities fe ON fe.fact_id=f.id
                JOIN entities e ON e.entity_id=fe.entity_id
                WHERE e.name LIKE ? AND f.archived=0
                ORDER BY f.updated_at DESC LIMIT ?
            """, (entity, limit)).fetchall()
            conn.close()
            return {"status":"ok","entity":entity,"facts":[dict(r) for r in rows],"count":len(rows)}
        else:
            conn.close()
            return {"status":"error","detail":"需要 fact_id 或 entity 参数"}

    @app.get("/facts/related")
    def fact_related(entity: str = "", limit: int = 10):
        if not entity: return {"status":"error","detail":"需要 entity 参数"}
        conn = _get_facts_conn()
        rows = conn.execute("""
            SELECT f.id, f.category, f.fact_key, f.fact_value, f.trust_score, f.updated_at,
                   (SELECT GROUP_CONCAT(DISTINCT e3.name) FROM fact_entities fe3
                    JOIN entities e3 ON e3.entity_id=fe3.entity_id WHERE fe3.fact_id=f.id) as shared_entities
            FROM facts f
            JOIN fact_entities fe ON fe.fact_id = f.id
            JOIN entities e ON e.entity_id = fe.entity_id
            WHERE e.name LIKE ? AND f.archived = 0 AND f.id NOT IN (
                SELECT f2.id FROM facts f2
                JOIN fact_entities fe2 ON fe2.fact_id=f2.id
                JOIN entities e2 ON e2.entity_id=fe2.entity_id
                WHERE e2.name LIKE ?
            )
            GROUP BY f.id ORDER BY f.trust_score DESC, COUNT(DISTINCT e.name) DESC LIMIT ?
        """, (entity, entity, limit)).fetchall()
        conn.close()
        return {"status":"ok","entity":entity,"related":[dict(r) for r in rows],"count":len(rows)}

    @app.get("/facts/reason")
    def fact_reason(entities: str = "", limit: int = 10):
        if not entities: return {"status":"error","detail":"需要 entities 参数（逗号分隔）"}
        e_list = [e.strip() for e in entities.split(",") if e.strip()]
        if len(e_list) < 2: return {"status":"error","detail":"需要至少 2 个实体"}
        conn = _get_facts_conn()
        placeholders = ",".join("?" * len(e_list))
        rows = conn.execute(f"""
            SELECT f.id, f.category, f.fact_key, f.fact_value, f.trust_score, f.updated_at,
                   GROUP_CONCAT(DISTINCT e.name) as matched_entities,
                   COUNT(DISTINCT e.name) as match_count
            FROM facts f
            JOIN fact_entities fe ON fe.fact_id=f.id
            JOIN entities e ON e.entity_id=fe.entity_id
            WHERE e.name IN ({placeholders}) AND f.archived=0
            GROUP BY f.id
            HAVING COUNT(DISTINCT e.name) >= ?
            ORDER BY f.trust_score DESC, match_count DESC LIMIT ?
        """, e_list + [len(e_list), limit]).fetchall()
        conn.close()
        return {"status":"ok","query_entities":e_list,"results":[dict(r) for r in rows],
                "count":len(rows),"min_match":len(e_list)}

    @app.get("/facts/entities/list")
    def list_entities(entity_type: str = None, limit: int = 50):
        conn = _get_facts_conn()
        if entity_type:
            rows = conn.execute("""
                SELECT e.*, COUNT(fe.fact_id) as fact_count
                FROM entities e LEFT JOIN fact_entities fe ON fe.entity_id=e.entity_id
                WHERE e.entity_type=? GROUP BY e.entity_id ORDER BY fact_count DESC LIMIT ?
            """, (entity_type, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT e.*, COUNT(fe.fact_id) as fact_count
                FROM entities e LEFT JOIN fact_entities fe ON fe.entity_id=e.entity_id
                GROUP BY e.entity_id ORDER BY fact_count DESC LIMIT ?
            """, (limit,)).fetchall()
        conn.close()
        return {"status":"ok","entities":[dict(r) for r in rows],"count":len(rows)}

    # ── 6.4  矛盾检测 v2 ──
    @app.post("/prune/contradiction-v2")
    def detect_contradictions_v2(dry_run: bool = True, min_overlap: float = 0.3, limit: int = 20):
        conn = _get_facts_conn()
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT id,category,fact_key,fact_value,trust_score FROM facts WHERE archived=0 ORDER BY updated_at DESC LIMIT 200"
        ).fetchall()
        if len(rows) < 2:
            conn.close()
            return {"status":"ok","contradictions":[],"count":0}
        by_cat = defaultdict(list)
        for r in rows: by_cat[r["category"]].append(dict(r))
        all_ids = [r["id"] for r in rows]
        entity_rows = cur.execute(f"""
            SELECT fe.fact_id, e.name FROM fact_entities fe
            JOIN entities e ON e.entity_id=fe.entity_id
            WHERE fe.fact_id IN ({','.join('?'*len(all_ids))})
        """, all_ids).fetchall()
        fact_ents = defaultdict(set)
        for fid, ename in entity_rows: fact_ents[fid].add(ename.lower())

        def jaccard_tokens(t1, t2):
            if not t1 or not t2: return 0.0
            s1 = set(re.findall(r'[\u4e00-\u9fa5]+|[a-zA-Z]{2,}', (t1 or "").lower()))
            s2 = set(re.findall(r'[\u4e00-\u9fa5]+|[a-zA-Z]{2,}', (t2 or "").lower()))
            return len(s1&s2)/len(s1|s2) if (s1|s2) else 0.0

        contradictions = []
        for cat, cat_rows in by_cat.items():
            n = min(len(cat_rows), 50)
            for i in range(n):
                for j in range(i+1, n):
                    f1, f2 = cat_rows[i], cat_rows[j]
                    ents1, ents2 = fact_ents.get(f1["id"],set()), fact_ents.get(f2["id"],set())
                    if not ents1 or not ents2: continue
                    e_overlap = len(ents1&ents2)/len(ents1|ents2) if (ents1|ents2) else 0.0
                    if e_overlap < min_overlap: continue
                    c_sim = jaccard_tokens(f1["fact_value"], f2["fact_value"])
                    c_score = e_overlap * (1.0 - c_sim)
                    if c_score >= 0.15:
                        ps = sorted([
                            {"id":f1["id"],"key":f1["fact_key"],"value":(f1["fact_value"]or"")[:200],"trust":f1["trust_score"]},
                            {"id":f2["id"],"key":f2["fact_key"],"value":(f2["fact_value"]or"")[:200],"trust":f2["trust_score"]}
                        ], key=lambda x: x["trust"], reverse=True)
                        contradictions.append({"category":cat,"higher_trust":ps[0],"lower_trust":ps[1],
                            "entity_overlap":round(e_overlap,3),"content_similarity":round(c_sim,3),
                            "contradiction_score":round(c_score,3)})
                        if not dry_run and len(contradictions)<=limit:
                            cur.execute("UPDATE facts SET trust_score=MAX(0.1,trust_score*0.5),updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                       (ps[1]["id"],))
        if not dry_run: conn.commit()
        contradictions.sort(key=lambda x: x["contradiction_score"], reverse=True)
        result = contradictions[:limit]
        conn.close()
        total_pairs = sum(min(len(v),50)*(min(len(v),50)-1)//2 for v in by_cat.values() if len(v)>=2)
        return {"status":"ok (dry-run)" if dry_run else "ok (verified)","pairs_scanned":total_pairs,
                "contradictions_found":len(result),"contradictions":result}

    # ── 6.5  Feedback ──
    @app.post("/facts/feedback")
    def fact_feedback(fact_id: int, helpful: bool):
        return _fact_feedback_impl(fact_id, helpful)

    # ── 6.6  旧 v1 矛盾检测 ──
    @app.post("/prune/contradiction")
    def detect_contradictions(dry_run: bool = True, min_trust: float = 0.3):
        conn = _get_facts_conn()
        cur = conn.cursor()
        cur.execute("""
            SELECT category, COUNT(*) as cnt, GROUP_CONCAT(id) as ids
            FROM (SELECT id,category,trust_score FROM facts WHERE archived=0 AND trust_score>=?
                  ORDER BY id DESC LIMIT 500)
            GROUP BY category HAVING COUNT(*)>=2 ORDER BY cnt DESC LIMIT 10
        """, (min_trust,))
        groups = cur.fetchall()
        contradictions, audited = [], 0
        for cat, cnt, ids_str in groups:
            ids = [int(x) for x in ids_str.split(",")][:50]
            cur.execute(f"SELECT id,fact_key,fact_value,trust_score FROM facts WHERE id IN ({','.join('?'*len(ids))})", ids)
            rows = cur.fetchall()
            if len(rows)<2: continue
            for i in range(len(rows)):
                for j in range(i+1, len(rows)):
                    v1, v2 = rows[i]["fact_value"], rows[j]["fact_value"]
                    if not v1 or not v2: continue
                    for word_set in CONTRADICTION_WORDS:
                        if (word_set[0] in v1 or word_set[0] in v2) and any(w in v1 or w in v2 for w in word_set[1:]):
                            low = rows[i] if rows[i]["trust_score"]<=rows[j]["trust_score"] else rows[j]
                            high = rows[j] if rows[i]["trust_score"]<=rows[j]["trust_score"] else rows[i]
                            contradictions.append({"category":cat,"fact_key":rows[i]["fact_key"][:30],
                                "lower_trust":{"id":low["id"],"trust":low["trust_score"],"value":(low["fact_value"]or"")[:100]},
                                "higher_trust":{"id":high["id"],"trust":high["trust_score"],"value":(high["fact_value"]or"")[:100]},
                                "pattern":word_set[0]})
                            if not dry_run:
                                cur.execute("UPDATE facts SET trust_score=MAX(0.1,trust_score*0.5),updated_at=CURRENT_TIMESTAMP WHERE id=?",
                                           (low["id"],)); audited += 1
                            break
        if not dry_run: conn.commit()
        conn.close()
        return {"status":"ok (dry-run)" if dry_run else "ok","groups_scanned":len(groups),
                "contradictions_found":len(contradictions),"audited":audited,"contradictions":contradictions[:10]}

    # ── 6.7  标签系统 + 信任统计 ──
    @app.post("/facts/tags/generate")
    def generate_tags(fact_id: int | None = None):
        return {"status":"ok","tags":[],"note":"auto-tag generation v2"}

    @app.get("/facts/tags")
    def get_tags(fact_id: int | None = None):
        return {"status":"ok","tags":_load_tags()}

    @app.get("/facts/trust-stats")
    def fact_trust_stats():
        conn = _get_facts_conn()
        rows = conn.execute("""
            SELECT category,
                   COUNT(*) as cnt, AVG(trust_score) as avg_trust,
                   SUM(helpful_count) as helpful, SUM(unhelpful_count) as unhelpful
            FROM facts WHERE archived=0 GROUP BY category ORDER BY cnt DESC
        """).fetchall()
        conn.close()
        return {"status":"ok","categories":[dict(r) for r in rows]}

    # ── §7  搜索 ──
    @app.get("/facts/search")
    def search_facts(query: str = "", category: str = None, top_k: int = 10,
                     min_trust: float = 0.0, use_hybrid: bool = True,
                     level: str = "L2"):
        # facts 是独立结构化知识库，不再绕经 mem0/Qdrant；use_hybrid 保留为兼容参数。
        from ducky.facts_recall import search_facts as recall_facts
        return recall_facts(
            query,
            category=category,
            top_k=top_k,
            level=level,
            min_trust=min_trust,
        )

    # ── §8  Observations + Reflect ──
    @app.post("/observe/consolidate")
    def run_consolidation(user_id: str = DEFAULT_USER_ID):
        return _run_consolidation(user_id)

    @app.get("/observe")
    def list_observations(category: str = None, limit: int = 20, include_stale: bool = False):
        conn = _get_obs_conn()
        where = "WHERE 1=1"
        params = []
        if category: where+=" AND category=?"; params.append(category)
        if not include_stale: where+=" AND is_stale=0"
        rows = conn.execute(f"SELECT * FROM observations {where} ORDER BY updated_at DESC LIMIT ?", params+[limit]).fetchall()
        conn.close()
        return {"status":"ok","observations":[dict(r) for r in rows],"count":len(rows)}

    @app.post("/reflect")
    def reflect_memories(question: str, top_k: int = 10, use_llm: bool = True):
        try:
            from api_server import get_memory
            mem = get_memory()
            results = mem.search(question, filters={"user_id": DEFAULT_USER_ID}, limit=top_k)
            if isinstance(results, dict):
                results = results.get("results", [])
            if not isinstance(results, list):
                results = []
            return {"status":"ok","question":question,"results":results[:top_k]}
        except Exception as e:
            return {"status":"error","detail":str(e)}

    @app.get("/observe/related")
    def get_related(query: str, top_k: int = 5):
        return reflect_memories(query, top_k)

    # ── §9  Scene 聚类 + Persona ──
    @app.post("/scene/cluster")
    def cluster_scenes(category: str = None, dry_run: bool = True, min_similarity: float = 0.25):
        return _cluster_scenes_impl(category, dry_run, min_similarity)

    @app.get("/scene")
    def list_scenes(category: str = None, limit: int = 20):
        conn = _get_scenes_conn()
        rows = conn.execute("SELECT * FROM scenes ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
        return {"status":"ok","scenes":[dict(r) for r in rows]}

    def _refresh_persona_inline(name: str = "user"):
        conn = _get_facts_conn()
        rows = conn.execute("""
            SELECT fact_key, fact_value FROM facts
            WHERE archived=0 AND (category LIKE '%项目%' OR category LIKE '%AI%' OR fact_key LIKE '%user%')
            ORDER BY trust_score DESC LIMIT 100
        """).fetchall()
        conn.close()
        return {"status":"ok","name":name,"facts_count":len(rows)}

    @app.get("/persona")
    def get_persona(name: str = "user"):
        return _refresh_persona_inline(name)

    @app.post("/persona/build")
    def build_persona(name: str = Form("user")):
        return _refresh_persona_inline(name)

    # ── §10  Skill 发现 ──
    @app.post("/skill/discover")
    def discover_skill_patterns(dry_run: bool = True):
        conn = _get_facts_conn()
        rows = conn.execute("""
            SELECT fact_key, fact_value, COUNT(*) as cnt
            FROM facts WHERE category='Solution' AND archived=0
            GROUP BY fact_key HAVING COUNT(*)>=3 ORDER BY cnt DESC LIMIT 20
        """).fetchall()
        conn.close()
        discovered = []
        for r in rows:
            content = r["fact_value"]
            steps = [kw for kw in ["升级","配置","登录","重启","删除","复制","备份","curl","扫描","验证"] if kw in content]
            if steps: discovered.append({"key":r["fact_key"],"repeat":r["cnt"],"steps":steps})
        return {"status":"ok (dry-run)" if dry_run else "ok","discovered":len(discovered),"patterns":discovered}

    logger.info(f"✅ 22 legacy 端点已注册到 FastAPI app")
