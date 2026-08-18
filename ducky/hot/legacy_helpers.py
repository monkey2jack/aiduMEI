"""ducky.hot.legacy_helpers — SQLite Legacy 辅助函数与连接池"""
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
_RE_TECH        = re.compile(r'\b(Hermes|DeepSeek|Qdrant|NVIDIA|FTS5|WAL|VACUUM|nginx|systemd|FastAPI)\b')


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
    # 🔴10：合并间隔改为读 manifest/env 可配置项（consolidation_interval_hours，默认 24h）。
    import os as _os
    try:
        interval_h = float(_os.getenv("AIDUMEM_CONSOLIDATION_INTERVAL_HOURS", "0")) or None
    except ValueError:
        interval_h = None
    if interval_h is None:
        try:
            import json as _json
            _mp = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.dirname(__file__))), "manifest.json")
            _cfg = (_json.load(open(_mp, encoding="utf-8")).get("capabilities", {}) or {}).get("config", {})
            interval_h = float(_cfg.get("consolidation_interval_hours", {}).get("default", 24))
        except Exception:
            interval_h = 24
    interval_s = max(60, int(interval_h * 3600))
    while True:
        try: _run_consolidation(max_obs=30)
        except Exception as e: logger.error(f"consolidation 后台失败: {e}")
        time.sleep(interval_s)

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

def _ensure_observations_table(conn):
    """幂等建 observations 表。

    v19.4.1（P1-3）：该表自 v7 起只有读取方（GET /observe），
    写入方 `_run_consolidation` 早已退化为占位（由 Layer1 自检 +
    Instinct 毕业接管），全仓从未有过 DDL —— 全新部署调 /observe
    直接 `no such table: observations` 500。此处按 scenes 表同一
    模式幂等建表，并预留 user_id 租户列（与 v19.4.1 租户贯通对齐）。

    只 CREATE IF NOT EXISTS，绝不 DROP / 改类型 / 删数据。
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS observations (
            obs_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     TEXT DEFAULT '',
            category    TEXT DEFAULT '',
            content     TEXT DEFAULT '',
            evidence    TEXT DEFAULT '',
            confidence  REAL DEFAULT 0.5,
            is_stale    INTEGER DEFAULT 0,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_obs_user ON observations(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_obs_category ON observations(category)",
    ):
        try:
            conn.execute(stmt)
        except Exception as exc:
            logger.debug("observations 索引跳过: %s", exc)
    conn.commit()

def _ensure_scenes_table(conn):
    """🔴6：scenes 表此前从未建，导致 /scene 开箱 500。此处幂等建表。

    member_keys 加 UNIQUE：后台聚类每 12h 跑一次，配合下方 INSERT OR IGNORE
    防止同一场景重复累积、表无限膨胀（自审 A ⚠️）。
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scenes (
            scene_id    INTEGER PRIMARY KEY AUTOINCREMENT,
            category    TEXT DEFAULT '',
            summary     TEXT DEFAULT '',
            member_keys TEXT DEFAULT '' UNIQUE,
            member_count INTEGER DEFAULT 0,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # 迁移：早期版本建过无 UNIQUE 的 scenes 表；scenes 是聚类衍生数据，可安全重建。
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='scenes'"
    ).fetchone()
    if row and row[0] and "UNIQUE" not in row[0]:
        conn.executescript("""
            DROP TABLE IF EXISTS scenes;
            CREATE TABLE scenes (
                scene_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                category    TEXT DEFAULT '',
                summary     TEXT DEFAULT '',
                member_keys TEXT DEFAULT '' UNIQUE,
                member_count INTEGER DEFAULT 0,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
    conn.commit()


def _cluster_scenes_impl(category: str = None, dry_run: bool = True, min_similarity: float = 0.25):
    conn = _get_facts_conn()  # 🔧 修：categories 来自 facts 表，非 scenes
    categories = [category] if category else [r[0] for r in conn.execute(
        "SELECT DISTINCT category FROM facts WHERE archived=0 ORDER BY category").fetchall()]
    clustered = 0
    scenes_out = []
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
                if best_match is not None:
                    scenes_out.append({
                        "category": cat,
                        "summary": (facts[i].get("fact_value","") or "")[:120],
                        "member_keys": f"{facts[i].get('fact_key','')}|{best_match.get('fact_key','')}",
                    })
    conn.close()
    # 🔴6：非 dry-run 时把聚类结果落库（此前只算 clustered 计数、从不写库）
    if not dry_run and scenes_out:
        sconn = _get_scenes_conn()
        try:
            _ensure_scenes_table(sconn)
            for s in scenes_out:
                sconn.execute(
                    "INSERT OR IGNORE INTO scenes (category, summary, member_keys, member_count) VALUES (?,?,?,2)",
                    (s["category"], s["summary"], s["member_keys"]),
                )
            sconn.commit()
        finally:
            sconn.close()
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
