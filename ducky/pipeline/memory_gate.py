#!/usr/bin/env python3
"""
aiduMEM Relevance Gate — 相关性闸门
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Osaurus Memory v2 读路径启发：
- 先判断「这条查询需要记忆吗？」再检索
- 不需要 → 零注入，不浪费 token
- 需要 → 取对应 scope，拼 ≤800 token 上下文块

启发式闸门（heuristic gate）：
1. 代词/指代 → 一定需要记忆
2. 明确回忆请求 → 需要
3. 实体命中 → 需要
4. 其他 → 默认不注入
"""

import os
import re, logging, time

logger = logging.getLogger("aiduMEM.gate")

# ── 闸门规则 ──
REFERENCE_PATTERNS = re.compile(
    r'上次|之前|以前|前面|刚才|刚刚|过去|曾经|还记得|'
    r'上次说的|上回|那.*(事|问题|话题|项目|任务)|'
    r'上次.*(聊|说|讲|提到|讨论)|'
    r'继续|接着|再.*(说|讲|聊)|'
    r'我们.*(决定|说过|定|约)|'
    r'last time|previously|before|earlier|'
    r'remember|recall|what.*(we|I).*said|'
    r'continue|go on|pick up',
    re.IGNORECASE
)

EXPLICIT_RECALL = re.compile(
    r'记得|忘记|忘了|记不|想起来|想不起|回忆|'
    r'查.*记忆|查.*历史|搜索.*记忆|'
    r'remember|forgot|forget|recall|search.*memory',
    re.IGNORECASE
)

# 不需要记忆的查询（直接跳过）
NO_MEMORY_PATTERNS = re.compile(
    r'^(ok|好|嗯|哦|行|可以|是的|对|收到|了解|明白|知道了|再见|拜拜|谢谢|'
    r'yes|no|yep|nope|k|kk|okay|thanks|bye|got it|sure|alright|'
    r'hello|hi|hey|早上好|晚上好|晚安)[!！。.]{0,3}$',
    re.IGNORECASE
)

# 纠错/纠偏关键词匹配
CORRECTION_PATTERNS = re.compile(
    r'不对|不是这|你记错|错了|no, |wrong|actually|not really|记错了|你说错',
    re.IGNORECASE
)

# 实体匹配 — 通用自指模式 + 可选的部署方自定义关键词
#
# 自定义实体（人名、昵称、项目代号、作品名等）不硬编码在源码里，
# 通过环境变量注入，用 `|` 分隔，例如：
#     export AIDUMEM_ENTITY_KEYWORDS="Alice|Bob|ProjectX"
# 未设置时只使用下面的通用模式。
_BASE_SELF_REFERENCE = (
    r'我的|我是|我叫|我.*(名字|生日|年龄|地址|电话|邮箱)|'
    r'assistant|agent|user|用户'
)


def _build_self_reference(extra: str = "") -> re.Pattern:
    pattern = _BASE_SELF_REFERENCE
    extra = (extra or "").strip().strip("|")
    if extra:
        # 每个自定义词单独转义，避免部署方误输入的元字符破坏整条正则
        safe = "|".join(re.escape(w.strip()) for w in extra.split("|") if w.strip())
        if safe:
            pattern = f"{pattern}|{safe}"
    return re.compile(pattern, re.IGNORECASE)


# 实体词表在「首次用到时」构建，并跟随环境变量热更新。
#
# 血训（v15）：早期版本在 import 时就把 SELF_REFERENCE 定死，
# 于是「先 import ducky、后 setenv」或「systemd 漏配 Environment=」
# 都会让实体词永久为空 —— 闸门对部署方自己的核心词全判 no_signal，
# 检索静默返回 0 结果，且不报任何错。必须惰性构建 + 缓存键校验。
_SELF_REF_CACHE: dict = {"key": None, "pattern": None}
_ENTITY_WARNED = False


def _entity_keywords() -> str:
    return (os.environ.get("AIDUMEM_ENTITY_KEYWORDS") or "").strip().strip("|")


def get_self_reference() -> re.Pattern:
    """取当前实体词正则；环境变量变化时自动重建。"""
    global _ENTITY_WARNED
    key = _entity_keywords()
    if _SELF_REF_CACHE["key"] != key or _SELF_REF_CACHE["pattern"] is None:
        _SELF_REF_CACHE["key"] = key
        _SELF_REF_CACHE["pattern"] = _build_self_reference(key)
        if key:
            logger.info(
                "闸门实体词已加载：%d 个自定义词", len([w for w in key.split("|") if w.strip()])
            )
    if not key and not _ENTITY_WARNED:
        _ENTITY_WARNED = True
        logger.warning(
            "⚠️ AIDUMEM_ENTITY_KEYWORDS 未设置 —— 相关性闸门只认通用自指模式，"
            "涉及你自己的人名/项目代号的查询会被判 no_signal 而不召回记忆。"
            "请参考 .env.example 配置后重启服务。"
        )
    return _SELF_REF_CACHE["pattern"]


def entity_keywords_status() -> dict:
    """供 /health 与启动自检使用的实体词表状态。"""
    key = _entity_keywords()
    words = [w.strip() for w in key.split("|") if w.strip()] if key else []
    return {
        "configured": bool(words),
        "count": len(words),
        "env_var": "AIDUMEM_ENTITY_KEYWORDS",
    }


def __getattr__(name):
    # 兼容老代码 `from ducky.pipeline.memory_gate import SELF_REFERENCE`
    if name == "SELF_REFERENCE":
        return get_self_reference()
    raise AttributeError(name)

# ── v9 优化：近几轮会话上下文门控缓存 ──
_LAST_GATE_DECISION = {"time": 0.0, "query": "", "needs_memory": False}
_GATE_CACHE_TTL = 15.0  # 15秒缓存过期


def reset_gate_cache() -> None:
    """清空门控热缓存。

    热缓存会让「上一轮判了要记忆 + 本轮是 <12 字追问」直接沿用上轮结论，
    这在真实会话里是对的，但会掩盖单条查询的真实判定。测试与诊断脚本
    需要逐条独立判定时先调这个。
    """
    global _LAST_GATE_DECISION
    _LAST_GATE_DECISION = {"time": 0.0, "query": "", "needs_memory": False}


def relevance_check(query: str) -> dict:
    """
    判断查询是否需要记忆上下文。
    返回 {"needs_memory": bool, "reason": str, "scope": str}
    """
    if not query or len(query.strip()) < 3:
        return {"needs_memory": False, "reason": "query_too_short", "scope": None}

    q = query.strip()
    global _LAST_GATE_DECISION
    now = time.time() if hasattr(time, "time") else 0.0

    # 0. 优先检测纠偏/纠错信号 (Lethe v9.2.0)
    if CORRECTION_PATTERNS.search(q):
        res = {"needs_memory": True, "reason": "correction_detected", "scope": "episode"}
        _LAST_GATE_DECISION = {"time": now, "query": q, "needs_memory": True}
        return res

    # 1. 纯社交结束语 → 不需要
    if NO_MEMORY_PATTERNS.match(q):
        return {"needs_memory": False, "reason": "social_closer", "scope": None}

    # 2. 检查缓存状态（如果user追问如“为什么”或短句，沿用上一轮门控判定）
    now = time.time() if hasattr(time, "time") else 0.0
    if now > 0 and (now - _LAST_GATE_DECISION["time"]) < _GATE_CACHE_TTL:
        # 如果上一轮开启了记忆，且当前是追问（短句），热激活沿用
        if _LAST_GATE_DECISION["needs_memory"] and len(q) < 12:
            logger.debug(f"闸门命中热缓存: 沿用 needs_memory=True")
            return {"needs_memory": True, "reason": "session_followup_hot", "scope": "episode"}

    # 3. 自我/身份指代 & 实体命中 → Identity scope
    if get_self_reference().search(q):
        res = {"needs_memory": True, "reason": "self_reference", "scope": "identity"}
        _LAST_GATE_DECISION = {"time": now, "query": q, "needs_memory": True}
        return res

    # 4. 明确回忆请求 → Episode scope
    if EXPLICIT_RECALL.search(q):
        res = {"needs_memory": True, "reason": "explicit_recall", "scope": "episode"}
        _LAST_GATE_DECISION = {"time": now, "query": q, "needs_memory": True}
        return res

    # 5. 指代/延续 → Episode scope
    if REFERENCE_PATTERNS.search(q):
        res = {"needs_memory": True, "reason": "reference", "scope": "episode"}
        _LAST_GATE_DECISION = {"time": now, "query": q, "needs_memory": True}
        return res

    # 6. 有实质内容（含实词）→ Pinned facts
    if len(q) > 15 and _has_content_words(q):
        res = {"needs_memory": True, "reason": "content_query", "scope": "pinned"}
        _LAST_GATE_DECISION = {"time": now, "query": q, "needs_memory": True}
        return res

    # 7. 默认不需要
    res = {"needs_memory": False, "reason": "no_signal", "scope": None}
    _LAST_GATE_DECISION = {"time": now, "query": q, "needs_memory": False}
    return res


def _has_content_words(text: str) -> bool:
    """判断文本是否含实质内容（非纯功能词）"""
    # 中文实词特征：含汉字且超过 5 个汉字
    chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
    if chinese_chars >= 5:
        return True
    # 英文实词
    content_patterns = [
        r'\b(what|how|why|when|where|who|which|explain|describe|'
        r'analyze|compare|create|build|fix|debug|deploy|install|'
        r'config|setup|migrate|upgrade|error|fail|bug|issue|'
        r'方案|怎么|如何|为什么|帮我|需要|应该|建议|推荐)\b',
    ]
    for pat in content_patterns:
        if re.search(pat, text, re.IGNORECASE):
            return True
    return False
