"""
ducky.pattern_extract — 确定性抽取层（v20.1 WP-A）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

为什么这一层必须存在
────────────────────
写入链的结构化事实此前只有 LLM 一个来源。LLM 是远程服务，会限流、会超时、
会在推理预算耗尽时返回空串——而 mem0 对空抽取的处理是静默丢弃。v20.0 把
「丢了」变成了可观测（``mem0_patches`` 的 ``empty_extraction`` 计数），但
观测不等于止损：计数器 +1 的那一刻，那条写入里的日期、版本号、指令、偏好
已经没了。

这一层是 LLM 之外的**第二事实来源**：纯规则（正则 + 词法），零模型、零网络、
零 token。它不替代 LLM 抽取——LLM 管语义归纳，这里只管「硬事实」：模式
明确、正则抓得住、丢了最心疼的那几类。LLM 全程健康时它是冗余，LLM 哑火时
它是底线。

三条设计底线
────────────
1. **确定性**：同一输入永远产出同一结果。全模块禁止 ``now()`` / 随机数；
   相对日期（今天/明天…）只在调用方传入 ``recorded_at`` 锚点时换算，
   锚点缺失就不产出，绝不悄悄用当前时间补。
2. **可逆**：所有产物经 ``federation.writer.write_fact`` 落 facts 层，
   ``source='pattern_extract'``、category 一律带 ``pattern_`` 前缀。
   回滚 = 按 source 精确清除，一条不多删。
3. **失败可观测**：单条落库失败计数 + 日志后继续其余条目；整层异常由
   调用方（``hot/add.py``）记入 failure_ledger。任何一处都不许静默吞掉
   ——「每写一个 except 都要能回答：真失败了谁会知道」。

噪音控制（写少不写错）
──────────────────────
规则抽取的天敌是误报污染 facts 层。这里的取舍是**保守优先**：
键值断言带连词后缀护栏（「但是/还是/就是…」不当键）与代词停用集；
版本号要求 v 前缀、≥2 个点、或上下文出现版本类词；数字必须带白名单单位；
日期/版本的匹配区间会屏蔽数字类的重复抽取。宁可漏，不可脏——漏掉的
LLM 还有机会抽到，写脏的会进召回污染每一次检索。
每次写入最多落 ``MAX_FACTS_PER_ADD`` 条，超出**必须**打日志并计数
（沉默截断会把「覆盖了」伪装成「全覆盖」）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta

logger = logging.getLogger("aiduMEM.PatternExtract")

#: 产物在 facts 层的 source 标记 —— 回滚与审计的唯一锚点。
PATTERN_EXTRACT_SOURCE = "pattern_extract"

#: 环境变量开关（显式压过配置文件；无效值报错点名，绝不静默回退）。
#: 前缀用 AIDUMEI_ —— AIDUMEM_ 是为兼容既有部署冻结的旧前缀，新变量禁用
#:（tests/test_v20_brand_policy.py 的冻结集守卫会拦）。
ENV_FLAG = "AIDUMEI_PATTERN_EXTRACT"

#: 单次写入最多落库条数 —— 截断必须可观测。
MAX_FACTS_PER_ADD = 20

#: 单条 fact_value / fact_key 的长度上限。
MAX_VALUE_LEN = 200
MAX_KEY_LEN = 64

# ───────────────────────── 计数器（/health 暴露） ─────────────────────────

_STATS_LOCK = threading.Lock()
_STATS: dict[str, int] = {
    "attempted": 0,        # extract_and_store 被调用次数（开关开着）
    "extracted": 0,        # 抽出的事实条数（落库前）
    "stored": 0,           # 成功落库条数（insert/update/merge 都算）
    "store_failed": 0,     # 单条落库失败条数
    "truncated": 0,        # 因 MAX_FACTS_PER_ADD 截断的次数
    "disabled_skips": 0,   # 开关关闭而跳过的次数
}


def _bump(key: str, n: int = 1) -> None:
    with _STATS_LOCK:
        _STATS[key] = _STATS.get(key, 0) + n


def stats() -> dict[str, int]:
    """返回计数快照（拷贝，调用方改不坏内部状态）。"""
    with _STATS_LOCK:
        return dict(_STATS)


def reset_stats() -> None:
    """仅供测试：清零计数。生产路径没有任何调用方。"""
    with _STATS_LOCK:
        for k in _STATS:
            _STATS[k] = 0


# ───────────────────────── 开关 ─────────────────────────

def is_pattern_extract_enabled() -> bool:
    """三态开关：env 显式 > 配置 ``_features.pattern_extract`` > 默认 True。

    env 值无效时**抛错点名**而不是回退默认——「用户指了 A、实际跑的是 B、
    还是绿的」是 SOP 里付过学费的形态。抛出的异常由 add 链路的 wrapper
    记入 failure_ledger，可观测、不阻断主链。
    """
    raw = os.environ.get(ENV_FLAG)
    if raw is not None:
        v = raw.strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off"):
            return False
        raise ValueError(
            f"{ENV_FLAG} 值无效: {raw!r}（接受 1/0/true/false/yes/no/on/off）"
            "——显式配置无效必须报错点名，不静默回退"
        )
    try:
        from ducky.mem0_runtime import MEM0_CONFIG
        if os.path.exists(MEM0_CONFIG):
            with open(MEM0_CONFIG) as f:
                cfg = json.loads(f.read())
            return bool(cfg.get("_features", {}).get("pattern_extract", True))
    except Exception as e:  # 配置读不动按默认开——与 vision/obsidian 同一惯例
        logger.warning("读取 _features.pattern_extract 失败，按默认开启: %s", e)
    return True


# ───────────────────────── 词表与正则 ─────────────────────────

#: 句子切分：中英文句末标点 + 换行。分号也切——键值断言极少跨分号。
_SENT_SPLIT = re.compile(r"[。！？!?；;\n]+")

#: 指令句标记词 —— 命中即整句入库（这类句子丢一条都可能挨骂）。
_INSTRUCTION_RE = re.compile(
    r"必须|禁止|铁律|别再|永不|不许|严禁|一律|不得|绝不|杜绝|不准|勿"
)

#: 偏好句标记词。
_PREFERENCE_RE = re.compile(r"不?喜欢|讨厌|偏好|偏爱|最爱|习惯|反感")

#: 绝对日期。顺序有意从长到短：先占住带年份的长形态，
#: 屏蔽区间再挡住短形态在同一位置的重复命中。
_DATE_RES = (
    re.compile(r"\d{4}-\d{1,2}-\d{1,2}"),
    re.compile(r"\d{4}年\d{1,2}月\d{1,2}日?"),
    re.compile(r"\d{1,2}月\d{1,2}日"),
)

#: 相对日期 → 相对 recorded_at 的天数偏移。
_REL_DAYS = {"今天": 0, "明天": 1, "后天": 2, "昨天": -1, "前天": -2}
_REL_RE = re.compile("|".join(_REL_DAYS))

#: 版本号候选（是否采信看 _version_accept）。
_VERSION_RE = re.compile(
    r"\bv?\d+(?:\.\d+){1,3}(?:[-.]?(?:pre|dev|rc|alpha|beta)\.?\d*)?\b",
    re.IGNORECASE,
)
_VERSION_CONTEXT_RE = re.compile(r"版本|version|升级|发布|装|install|upgrade|release", re.IGNORECASE)

#: 数字 + 白名单单位。英文单位后不许紧跟字母（避免 "10 mst" 里的 ms）。
#: 复合量词（个小时/个月/个星期）排在裸「个」之前 —— 否则「3.5 个小时」
#: 会被截成「3.5个」，单位被孤儿化（v20.1 整改轮 R-04 · 外审 z P2-01 实例②）。
_METRIC_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:TB|GB|MB|KB|ms|毫秒|秒|分钟|小时|天|周|个小时|个星期|个月|年|条|次|个|行|张|页|%|％|元|美元|倍)(?![A-Za-z])"
)

#: URL 与绝对路径。路径要求 ≥2 段，只认 Unix 形态（生产环境没有别的）。
_URL_RE = re.compile(r"https?://[^\s\"'<>（）()【】\[\]、，。；！？]+")
_PATH_RE = re.compile(r"(?:^|(?<=\s))(~?/[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+)")

#: 键值断言。三种形态按可信度排：行首冒号定义 > X=Y > X是/为Y。
_KV_EQ_RE = re.compile(r"([A-Za-z0-9_一-鿿.\-]{2,24})\s*[=＝]\s*([^\s，。！？；]{1,120})")
_KV_COLON_RE = re.compile(r"^([A-Za-z0-9_一-鿿.\-]{2,24})[：:]\s*(\S[^\n]{0,119})$")
_KV_SHI_RE = re.compile(r"([A-Za-z0-9_一-鿿.\-]{2,24})\s*(?:是|为)\s*([^，。！？；\n]{2,120})")

#: 「X是Y」的键后缀护栏：键以这些字结尾时，那个「是/为」大概率是连词或
#: 动词复合（但是/还是/就是/认为/以为/成为…），不是系动词。宁可漏。
_BAD_KEY_SUFFIX = set("但还就只算而不倒像真也都才即便或若总老凡认以成作称视评变")

#: 连词**整词**护栏（v20.1 整改轮 R-04 · 外审 z P2-01 实例①）：
#: 「X但是Y是Z」形句式里，键候选是「X但是Y」——尾字是 Y 的尾字，
#: 尾字护栏够不着中间的「但是」。整词出现在键内任意位置即拒。
_BAD_KEY_SUBSTRINGS = (
    "但是", "还是", "就是", "凡是", "总是", "老是", "算是", "倒是",
    "像是", "真是", "也是", "都是", "才是", "而是", "或是", "即是", "便是",
)

#: 纯代词/指示词不当键。
_STOP_KEYS = {
    "我", "你", "他", "她", "它", "我们", "你们", "他们", "她们", "它们",
    "大家", "自己", "这个", "那个", "这些", "那些", "什么", "哪个", "谁",
    "这里", "那里", "现在", "刚才", "以前", "以后",
}


def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text or "") if s.strip()]


def _clip(s: str, limit: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= limit else s[:limit]


def _key_ok(key: str) -> bool:
    key = key.strip()
    if len(key) < 2 or key in _STOP_KEYS:
        return False
    if key[-1] in _BAD_KEY_SUFFIX:
        return False
    if any(w in key for w in _BAD_KEY_SUBSTRINGS):
        return False
    return True


def _version_accept(token: str, sentence: str) -> bool:
    """版本号采信三选一：v 前缀 / ≥2 个点 / 上下文有版本类词。"""
    if token[:1].lower() == "v":
        return True
    if token.count(".") >= 2:
        return True
    return bool(_VERSION_CONTEXT_RE.search(sentence))


def _resolve_relative(word: str, recorded_at: str | None) -> str | None:
    """相对日期 → ISO 日期。锚点缺失/不可解析返回 None（绝不用 now() 补）。"""
    if not recorded_at:
        return None
    try:
        base = datetime.fromisoformat(str(recorded_at).replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        return None
    return (base + timedelta(days=_REL_DAYS[word])).isoformat()


def _normalize_date(token: str) -> str:
    """带年份的中文日期归一成 ISO；无年份形态原样保留（不编造年份）。"""
    m = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日?", token)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", token)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return token


# ───────────────────────── 抽取核心（纯函数） ─────────────────────────

def extract_patterns(text: str, *, recorded_at: str | None = None) -> list[dict]:
    """从文本中确定性地抽取七类硬事实。

    返回 ``[{"kind", "category", "fact_key", "fact_value"}, ...]``，
    顺序稳定（按句序 × 类别序），同一 (category, key, value) 只出一次。
    纯函数：不读环境、不落库、不看钟。
    """
    out: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    def emit(kind: str, key: str, value: str) -> None:
        key = _clip(key, MAX_KEY_LEN)
        value = _clip(value, MAX_VALUE_LEN)
        if not key or not value:
            return
        category = f"pattern_{kind}"
        sig = (category, key, value)
        if sig in seen:
            return
        seen.add(sig)
        out.append({"kind": kind, "category": category,
                    "fact_key": key, "fact_value": value})

    for sentence in _split_sentences(text):
        # 数字类屏蔽区间：日期与版本占住的位置，metric 不许重复抽。
        claimed: list[tuple[int, int]] = []

        def overlaps(start: int, end: int) -> bool:
            return any(not (end <= s or start >= e) for s, e in claimed)

        # ① 绝对日期
        for date_re in _DATE_RES:
            for m in date_re.finditer(sentence):
                if overlaps(m.start(), m.end()):
                    continue
                claimed.append((m.start(), m.end()))
                emit("datetime", _normalize_date(m.group()), sentence)

        # ① 相对日期（仅在锚点可解析时产出）
        for m in _REL_RE.finditer(sentence):
            resolved = _resolve_relative(m.group(), recorded_at)
            if resolved:
                emit("datetime", resolved, sentence)

        # ② 版本号
        for m in _VERSION_RE.finditer(sentence):
            if overlaps(m.start(), m.end()):
                continue
            if _version_accept(m.group(), sentence):
                claimed.append((m.start(), m.end()))
                emit("version", m.group(), sentence)

        # ③ 数字 + 单位（避开日期/版本已占区间）
        for m in _METRIC_RE.finditer(sentence):
            if overlaps(m.start(), m.end()):
                continue
            emit("metric", re.sub(r"\s+", "", m.group()), sentence)

        # ④ URL 与路径（路径避开 URL 区间）
        url_spans: list[tuple[int, int]] = []
        for m in _URL_RE.finditer(sentence):
            url_spans.append((m.start(), m.end()))
            emit("link", m.group().rstrip(".,;:"), sentence)
        for m in _PATH_RE.finditer(sentence):
            if any(not (m.end(1) <= s or m.start(1) >= e) for s, e in url_spans):
                continue
            emit("link", m.group(1), sentence)

        # ⑤ 键值断言（= / 行首冒号 / 是·为）。
        # URL 占住的区间对 KV 规则做 span 屏蔽（v20.1 整改轮 R-04 ·
        # 外审 z P2-01 实例③）：行首 URL 的「:」会被行首冒号规则当成
        # 键值定义，抽出 kv(https → //…) 的噪音。
        def _in_url(start: int, end: int) -> bool:
            return any(not (end <= s or start >= e) for s, e in url_spans)

        for m in _KV_EQ_RE.finditer(sentence):
            if _in_url(m.start(), m.end()):
                continue
            if _key_ok(m.group(1)):
                emit("kv", m.group(1), m.group(2))
        cm = _KV_COLON_RE.match(sentence)
        if cm and not _in_url(cm.start(1), cm.end(1)) and _key_ok(cm.group(1)):
            emit("kv", cm.group(1), cm.group(2))
        for m in _KV_SHI_RE.finditer(sentence):
            if _in_url(m.start(), m.end()):
                continue
            if _key_ok(m.group(1)):
                emit("kv", m.group(1), m.group(2).strip())

        # ⑥ 指令句 / ⑦ 偏好句 —— 整句入库，键取句子头部保证稳定。
        head = _clip(re.sub(r"\s+", " ", sentence), 32)
        if _INSTRUCTION_RE.search(sentence):
            emit("instruction", head, sentence)
        if _PREFERENCE_RE.search(sentence):
            emit("preference", head, sentence)

    return out


# ───────────────────────── 落库 ─────────────────────────

def extract_and_store(
    text: str,
    *,
    user_id: str,
    bank_id: str,
    recorded_at: str | None = None,
) -> dict:
    """抽取并经 ``write_fact`` 落 facts 层（source=pattern_extract，盖域戳）。

    单条失败：计数 + 日志，继续其余条目——一条坏事实不该拖死一批好事实。
    整层失败（import 崩、开关值非法…）：向上抛，由 add 链路记 failure_ledger。
    """
    if not is_pattern_extract_enabled():
        _bump("disabled_skips")
        return {"status": "disabled", "extracted": 0, "stored": 0, "failed": 0}

    _bump("attempted")
    items = extract_patterns(text, recorded_at=recorded_at)
    _bump("extracted", len(items))

    if len(items) > MAX_FACTS_PER_ADD:
        # 沉默截断 = 把「覆盖了一部分」伪装成「全覆盖」，必须出声。
        # v20.1 整改轮（R-05 · 外审 x REC-01 方向）：截断不再按抽取顺序
        # 粗暴切片 —— 句内顺序是日期在前、指令/偏好在后，长文本里 20 个
        # 日期会把 1 条关键指令挤出局。改按信息价值稳定排序后再切：
        # 指令/偏好（丢了最心疼）> 键值 > 日期/版本 > 数量/链接。
        # 稳定排序保证同输入同输出，确定性不破。
        _KIND_PRIORITY = {"instruction": 0, "preference": 0, "kv": 1,
                          "datetime": 2, "version": 2, "metric": 3, "link": 3}
        items.sort(key=lambda it: _KIND_PRIORITY.get(it["kind"], 4))
        dropped_kinds: dict[str, int] = {}
        for it in items[MAX_FACTS_PER_ADD:]:
            dropped_kinds[it["kind"]] = dropped_kinds.get(it["kind"], 0) + 1
        logger.warning(
            "🧩 [PatternExtract] 单次抽取 %d 条超上限，按重要性截断至 %d，"
            "被丢分布=%s（user=%s bank=%s）",
            len(items), MAX_FACTS_PER_ADD, dropped_kinds, user_id, bank_id,
        )
        _bump("truncated")
        items = items[:MAX_FACTS_PER_ADD]

    from ducky.federation.writer import write_fact

    stored = failed = 0
    for it in items:
        try:
            res = write_fact(
                it["category"], it["fact_key"], it["fact_value"],
                source=PATTERN_EXTRACT_SOURCE,
                user_id=user_id, bank_id=bank_id,
            )
            if isinstance(res, dict) and res.get("status") == "error":
                failed += 1
                logger.warning("🧩 [PatternExtract] 落库被拒: %s (key=%s)",
                               res.get("detail"), it["fact_key"])
            else:
                stored += 1
        except Exception as e:
            failed += 1
            logger.warning("🧩 [PatternExtract] 单条落库异常: %s (key=%s)",
                           e, it["fact_key"])

    _bump("stored", stored)
    _bump("store_failed", failed)
    return {"status": "ok", "extracted": len(items), "stored": stored, "failed": failed}
