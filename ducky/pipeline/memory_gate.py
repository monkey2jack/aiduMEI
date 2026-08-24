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


#: `.env` 里这个键的声明值 —— 唯一真相源（v20 P0-2 把它从 systemd drop-in 搬进来）
_ENV_FILE_NAME = ".env"


def _declared_in_env_file() -> tuple[str | None, str]:
    """读 `.env` 里声明的实体词表值，返回 (值 或 None, 状态)。

    状态取值：`present` / `absent`（文件在但没这一行）/ `no_file` / `unreadable`。
    只读不写，任何异常都降级成一个状态字符串，绝不让 /health 因为读配置而挂掉。
    """
    from ducky.utils import BASE_DIR
    path = os.path.join(BASE_DIR, _ENV_FILE_NAME)
    if not os.path.exists(path):
        return None, "no_file"
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for ln in f:
                ln = ln.strip()
                if ln.startswith("#") or "=" not in ln:
                    continue
                k, v = ln.split("=", 1)
                if k.strip() == "AIDUMEM_ENTITY_KEYWORDS":
                    return v.strip().strip('"').strip("'"), "present"
    except OSError:
        return None, "unreadable"
    return None, "absent"


def entity_keywords_source() -> str:
    """活值是不是来自那个唯一真相源 —— 「配置写了不等于配置生效」的探针（铁律 13）。

    v20 P0-2 之前，这个键住在 systemd drop-in `entity-keywords.conf` 里。而合并后的
    unit 中 drop-in 的 `Environment=` 排在 `EnvironmentFile=` **之后** —— 也就是说
    drop-in 永远压过 `.env`。迁移时如果只往 `.env` 加一行而不删 drop-in，`.env` 就
    成了纯装饰：改它没有任何效果，而且**没有任何东西会因此变红**。

    进程侧看不见值来自哪一个 systemd 层（环境变量只有值，没有出身）。但看得见一件
    等价有用的事：**活值和 `.env` 声明的那一份一致吗**。不一致就说明有别的东西在
    覆盖它，那正是要报出来的形态。

    返回值：
      · `env_file`      —— 活值 == `.env` 声明值（正常）
      · `overridden`    —— 两边都有值但**不相等**：有东西在压 `.env`（🔴 静默失配）
      · `outside_env_file` —— 活值有、`.env` 没声明：来源不在唯一真相源里
      · `declared_not_effective` —— `.env` 声明了但活值为空：声明没生效
      · `unset`         —— 两边都没有
      · `no_env_file` / `env_file_unreadable` —— 读不到那份文件，无法判定
    """
    live = _entity_keywords()
    declared, state = _declared_in_env_file()
    if state in ("no_file", "unreadable"):
        return "no_env_file" if state == "no_file" else "env_file_unreadable"
    declared_norm = (declared or "").strip().strip("|")
    if live and declared_norm:
        return "env_file" if live == declared_norm else "overridden"
    if live and not declared_norm:
        return "outside_env_file"
    if declared_norm and not live:
        return "declared_not_effective"
    return "unset"


def entity_keywords_status() -> dict:
    """供 /health 与启动自检使用的实体词表状态。"""
    key = _entity_keywords()
    words = [w.strip() for w in key.split("|") if w.strip()] if key else []
    return {
        "configured": bool(words),
        "count": len(words),
        "env_var": "AIDUMEM_ENTITY_KEYWORDS",
        # v20 P0-3：光报「配了几个词」不够 —— 22 个词可能来自一个我们以为已经
        # 删掉的 drop-in。这个字段回答的是「它来自唯一真相源吗」。
        "source": entity_keywords_source(),
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
