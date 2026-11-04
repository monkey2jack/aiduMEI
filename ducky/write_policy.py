"""ducky.write_policy — f0.7+ "what is worth remembering" (nine principles).

Every write already lands verbatim; this module decides, per stored text,
whether it was worth keeping and how it should be read later.  It is the
single source of truth for the write-time judgement and the recall-time
labels, so the regression set, the replay script and the HTTP layer all
call the same functions.

Principle → code (P7 and the deletion half of P8 act; everything else is
shadow, i.e. it only stamps metadata and writes a verdict row):

  P1 worth score .............. classify() → verdict keep|daily|reject (shadow)
  P2 condition ................ extract_condition(); distill prompt rules
  P3 direct vs inferred ....... source_of() → metadata source_type/basis;
                                annotate_results() marks inferred recall rows
  P4 supersede ................ supersede_candidates() → "would supersede" (shadow)
  P5 conflict resolution ...... resolve_conflict(): direct>inferred, newer>older,
                                tie → pending_confirm (shadow)
  P6 layers ................... metadata layer=profile|topic|daily; recall filter
                                behind AIDUMEI_RECALL_EXCLUDE_DAILY (default off)
  P7 never store .............. redact_forbidden() / hard_block_reason() — ACTIVE
  P8 forget ................... purge_memory()/purge_scope() (real delete of
                                our rows); forget rules checked at write (shadow)
  P9 precise recall ........... precise_filter() behind AIDUMEI_RECALL_PRECISE
                                (default off)

Shadow log: ``<DATA_DIR>/write_policy.db`` (verdicts, forget_rules,
key_events).  Rows carry ids, hashes, flags and reason codes — never the
memory text — so a forgotten memory leaves nothing readable behind here.
Every hook is fail-open for the write path: a policy failure is logged and
the write proceeds exactly as before (P7 excepted, which fails closed).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Iterable

logger = logging.getLogger("aiduMEM.write_policy")

LAYERS = ("profile", "topic", "daily")
VERDICTS = ("keep", "daily", "reject")


# ── switches ─────────────────────────────────────────────────────────────
def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def policy_mode() -> str:
    """AIDUMEI_WRITE_POLICY: shadow (default) | off.  Shadow never blocks."""
    val = os.environ.get("AIDUMEI_WRITE_POLICY", "shadow").strip().lower()
    return "off" if val in ("off", "0", "false", "no") else "shadow"


def secret_guard_on() -> bool:
    """AIDUMEI_SECRET_GUARD: on (default).  P7 is a hard rule; off only as an
    explicit escape hatch for a deployment that stores credentials on purpose."""
    return os.environ.get("AIDUMEI_SECRET_GUARD", "on").strip().lower() not in ("off", "0", "false", "no")


def max_raw_chars() -> int:
    from ducky.env_config import int_env
    return int_env("AIDUMEI_WRITE_MAX_RAW_CHARS", 20000, minimum=1000)


# ══ P7 — never store secrets, ID documents, signatures, raw dumps ════════
REDACT_FMT = "[redacted:{}]"

# Specific, prefixed credential shapes: near-zero false positives.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("private_key", re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)")),
    ("api_key", re.compile(r"\bsk-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_\-]{20,}")),
    ("api_key", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{30,}\b")),
    ("api_key", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}\b")),
    ("api_key", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b")),
    ("api_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("api_key", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}\b")),
    ("api_key", re.compile(r"\btskey-[a-z]+-[A-Za-z0-9\-]{10,}\b")),
    ("api_key", re.compile(r"\bibk_[A-Za-z0-9]{16,}\b")),
    ("api_key", re.compile(r"\bcf[a-z]{2}_[A-Za-z0-9]{30,}\b")),
    ("api_key", re.compile(r"\bpypi-[A-Za-z0-9_\-]{50,}")),
    ("api_key", re.compile(r"\bnpm_[A-Za-z0-9]{30,}\b")),
    ("api_key", re.compile(r"\b(?:hf|r8|pk_live|sk_live|rk_live)_[A-Za-z0-9]{20,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+(?=[A-Za-z0-9._~+/\-]*\d)[A-Za-z0-9._~+/\-]{20,}=*")),
    ("embedded_image", re.compile(r"data:image/[A-Za-z0-9.+\-]+;base64,[A-Za-z0-9+/=\s]{200,}")),
    ("url_credentials", re.compile(r"(?<=://)[^\s/:@]{1,64}:[^\s/@]{3,128}(?=@)")),
)

# key = value assignments: only the value goes, the sentence stays.
_ASSIGN_RE = re.compile(
    # f0.7.5（外审 sesame F3 / muse H-07）：左边界从 \b 改为「前面不是字母数字」——
    # 下划线是 \w，`DB_PASSWORD=` / `DEEPSEEK_API_KEY=` / `aws_secret_access_key =`
    # 这类 .env 标准写法此前整句漏打码。右边界仍是 \b（`password_hint:` 不算）。
    r"(?i)(?P<k>(?<![A-Za-z0-9])(?:api[_\- ]?key|access[_\- ]?key|secret(?:[_\- ]?(?:access[_\- ]?)?key)?|"
    r"client[_\- ]?secret|app[_\- ]?secret|auth[_\- ]?token|access[_\- ]?token|refresh[_\- ]?token|token|"
    r"password|passwd|pwd|passphrase)\b|密码|口令|密钥|秘钥|令牌)"
    # 分隔符：允许 JSON 键名后的引号（`"api_key": "…"`），以及「是：」「为：」「is:」复合写法。
    r"(?P<sep>[*_`\"']{0,3}\s*(?:(?:is|是|为)\s*[:=：]?|[:=：])\s*[*_]{0,3}\s*)[\"'`]?"
    r"(?P<v>[A-Za-z0-9_\-+/=.!@#$%^&*~?]{6,200})")
_PLACEHOLDER_RE = re.compile(r"^(?:[A-Z][A-Z_]+|(?i:x{3,}|\*{3,}|\.{3,}|<.*>|\$\{?\w+\}?|changeme|"
                             r"redacted.*|your[_\-].*|example.*|placeholder.*|none|null|true|false)|"
                             r"[a-z]+(?:_[a-z]+)+)$")
# 口令类键名后、不带数字/大小写/符号的值：只在明确赋值（: = ：）时算口令，
# 且排除描述状态的常见词（"password: required"、"token: expired"）。
_PASSWORD_KEY_RE = re.compile(r"(?i)(?:password|passwd|pwd|passphrase|secret|密码|口令|密钥|秘钥)")
_STATE_WORDS = frozenset({
    "required", "optional", "invalid", "expired", "missing", "empty", "wrong", "correct",
    "incorrect", "changed", "reset", "needed", "protected", "hidden", "unknown", "default",
    "enabled", "disabled", "masked", "omitted", "removed", "revoked", "rotated", "updated",
    "provided", "supplied", "redacted", "secret", "password", "token", "notset", "unset",
})
# 不带信息量、只用于拆分识别的格式字符（零宽、软连字符、BOM、词连接符等）。
_INVISIBLE_RE = re.compile("[\u00ad\u180e\u200b-\u200f\u2060-\u2064\ufeff]")
# 连续 ≥6 个全角 ASCII 字母数字/连字符/下划线/等号：按半角重扫一遍。
_FULLWIDTH_RUN_RE = re.compile("[\uff10-\uff19\uff21-\uff3a\uff41-\uff5a\uff0d\uff3f\uff1d]{6,}")

# ID documents: a valid PRC resident ID (region + birth date + checksum).
_CN_ID_RE = re.compile(r"(?<![0-9A-Za-z])([1-6]\d{5})((?:19|20)\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(\d{3}[0-9Xx])(?![0-9A-Za-z])")
_CN_ID_W = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_CN_ID_C = "10X98765432"
_DOC_CONTEXT_RE = re.compile(
    r"(?i)(?P<k>护照(?:号码?)?|passport(?:\s*(?:no\.?|number|#))?|驾驶证号?|驾照号?|"
    r"社保号|social\s*security(?:\s*number)?|\bssn\b)\s*[:：#]?\s*(?P<v>[A-Z0-9][A-Z0-9\-]{5,17})")
_CARD_CONTEXT_RE = re.compile(
    r"(?i)(?P<k>卡号|银行卡号?|信用卡号?|借记卡号?|card\s*(?:no\.?|number|#))\s*[:：#]?\s*(?P<v>\d(?:[ \-]?\d){12,18})")
_SIGNATURE_RE = re.compile(r"(?i)(?:签名|signature)[^\n]{0,20}data:image/[A-Za-z0-9.+\-]+;base64,[A-Za-z0-9+/=\s]{40,}")

# whole-copy shapes: rejected outright (record what it said, not the copy).
_EMAIL_HEADER_RE = re.compile(
    r"(?im)^(?:from|to|cc|bcc|subject|date|message-id|received|return-path|mime-version|"
    r"content-type|reply-to|dkim-signature|x-[a-z\-]+):\s")


def _cn_id_valid(m: re.Match) -> bool:
    digits = "".join(m.groups())
    try:
        datetime.strptime(digits[6:14], "%Y%m%d")
    except ValueError:
        return False
    total = sum(int(d) * w for d, w in zip(digits[:17], _CN_ID_W))
    return _CN_ID_C[total % 11] == digits[17].upper()


def _luhn(num: str) -> bool:
    ds = [int(c) for c in num if c.isdigit()]
    if not 13 <= len(ds) <= 19:
        return False
    s = 0
    for i, d in enumerate(reversed(ds)):
        if i % 2:
            d *= 2
            d = d - 9 if d > 9 else d
        s += d
    return s % 10 == 0


def _assignment_is_secret(value: str, key: str = "", sep: str = "") -> bool:
    v = value.strip().strip("\"'`")
    if _PLACEHOLDER_RE.match(v) or v.startswith("[redacted"):
        return False
    # a path or a URL after "pwd:" / "token=" is a location, not the secret itself
    # (ops chat is full of `pwd: /srv/...`); embedded URL credentials have their own pattern.
    if v.startswith(("/", "~/", "./", "../", "http://", "https://")):
        return False
    is_password_key = bool(_PASSWORD_KEY_RE.search(key or ""))
    # `max_token=4096` 是参数不是凭据；口令类键名下的纯数字（PIN）照打。
    if v.isdigit() and not is_password_key:
        return False
    has_digit = any(c.isdigit() for c in v)
    has_mixed = any(c.isupper() for c in v) and any(c.islower() for c in v)
    has_symbol = any(c in "!@#$%^&*+/=~?" for c in v)
    if has_digit or has_mixed or has_symbol:
        return True
    # f0.7.5（外审 haiku F-02 / deepseek F-07）：`password: letmein` 这类纯字母口令。
    explicit = any(ch in (sep or "") for ch in ":=：")
    if not explicit or v.lower() in _STATE_WORDS:
        return False
    # 口令类键名：纯字母也算；token / key 类：≥16 位的纯字母串（`token=abcdefghijklmnop`）。
    return is_password_key or len(v) >= 16


def _fullwidth_to_ascii(m: re.Match) -> str:
    return "".join(chr(ord(c) - 0xFEE0) for c in m.group(0))


def redact_forbidden(text: str) -> tuple[str, list[str]]:
    """Replace secret / ID / signature spans with ``[redacted:<kind>]``.

    Returns (new_text, kinds).  The rest of the sentence is kept: P7 is about
    never *storing* the secret, not about dropping the user's whole turn.

    f0.7.5（外审 6 家一致：零宽 / 软连字符 / 全角变体可绕过）：先按原文打码；
    再在「去掉不可见格式字符 + 全角字母数字串转半角」的视图上重扫。视图多抓到
    东西时采用视图结果——此时落库文本不含零宽字符（它们没有信息量），边界记号
    中和在视图上重做一遍，避免把 neutralize 插入的零宽打断一并剥掉。
    """
    if not isinstance(text, str) or not text or not secret_guard_on():
        return text, []
    out, kinds = _redact_core(text)
    if _INVISIBLE_RE.search(text) or _FULLWIDTH_RUN_RE.search(text):
        view = _FULLWIDTH_RUN_RE.sub(_fullwidth_to_ascii, _INVISIBLE_RE.sub("", text))
        v_out, v_kinds = _redact_core(view)
        if len(v_kinds) > len(kinds):
            try:
                from ducky.security.injection_guard import neutralize_boundary_markers
                v_out = neutralize_boundary_markers(v_out)
            except ImportError:  # pragma: no cover
                pass
            return v_out, v_kinds
    return out, kinds


def _redact_core(text: str) -> tuple[str, list[str]]:
    kinds: list[str] = []
    out = text

    def _sub(kind: str, pattern: re.Pattern, s: str) -> str:
        def rep(m: re.Match) -> str:
            kinds.append(kind)
            return REDACT_FMT.format(kind)
        return pattern.sub(rep, s)

    out = _sub("signature", _SIGNATURE_RE, out)
    for kind, pat in _SECRET_PATTERNS:
        out = _sub(kind, pat, out)

    def _assign(m: re.Match) -> str:
        if not _assignment_is_secret(m.group("v"), m.group("k"), m.group("sep")):
            return m.group(0)
        kinds.append("credential")
        return m.group("k") + m.group("sep") + REDACT_FMT.format("credential")
    out = _ASSIGN_RE.sub(_assign, out)

    def _cnid(m: re.Match) -> str:
        if not _cn_id_valid(m):
            return m.group(0)
        kinds.append("id_number")
        return REDACT_FMT.format("id_number")
    out = _CN_ID_RE.sub(_cnid, out)

    def _doc(m: re.Match) -> str:
        if not any(c.isdigit() for c in m.group("v")):
            return m.group(0)
        kinds.append("id_document")
        return m.group("k") + " " + REDACT_FMT.format("id_document")
    out = _DOC_CONTEXT_RE.sub(_doc, out)

    def _card(m: re.Match) -> str:
        if not _luhn(m.group("v")):
            return m.group(0)
        kinds.append("card_number")
        return m.group("k") + " " + REDACT_FMT.format("card_number")
    out = _CARD_CONTEXT_RE.sub(_card, out)
    return out, kinds


_MEANINGFUL_RE = re.compile(r"\[redacted:[a-z_]+\]|[\s\W_]+", re.UNICODE)


def hard_block_reason(text: str, *, summary: bool = False) -> str | None:
    """Whole-write rejections (P7): a raw e-mail / document dump, an oversized
    paste, or a turn that is nothing but a secret.  None = store it."""
    if not isinstance(text, str) or not text or not secret_guard_on():
        return None
    if len(_EMAIL_HEADER_RE.findall(text)) >= 3:
        return "raw_email_copy"
    if not summary and len(text) > max_raw_chars():
        return "oversized_raw_copy"
    if "[redacted:" in text and len(_MEANINGFUL_RE.sub("", text)) <= 4:
        return "secret_only"
    return None


_MESSAGE_KEYS = ("role", "content")


def _content_text(content) -> str:
    """OpenAI 多模态 list-content → 纯文本（只取文本段）；其他形态 str() 兜底。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(p for p in parts if p)
    if content is None:
        return ""
    return str(content)


def normalize_message_shapes(messages):
    """f0.7.5（外审 qwen F-03 / muse L-08）：/add 入口把消息收敛成 {role, content:str}。

    此前三道结构遍历（控制字符清洗、边界中和、原则 7 打码）只认字符串 content：
    多模态 list-content 与 name / tool_call_id / arguments 等键原样穿过，native
    内核随后在拼接处 500，而原文柜已落了未打码的原文。入口统一成纯文本后，
    检测、清洗、打码、落库看到的是同一份字节；非白名单键丢弃（无召回用途）。
    """
    if isinstance(messages, list):
        return [normalize_message_shapes(m) for m in messages]
    if isinstance(messages, dict):
        out = {k: messages[k] for k in _MESSAGE_KEYS if k in messages}
        out["content"] = _content_text(messages.get("content"))
        if not isinstance(out.get("role", "user"), str):
            out["role"] = "user"
        return out
    return messages


def redact_messages_struct(messages) -> tuple[Any, list[str]]:
    """Structure-preserving redaction for /add messages (str | dict | list).

    f0.7.5：dict 的每个字符串值（不止 content）、list-content 里的文本段都打码；
    role / type 这类枚举键不动。
    """
    kinds: list[str] = []
    if isinstance(messages, str):
        out, k = redact_forbidden(messages)
        return out, k
    if isinstance(messages, dict):
        out = {}
        for key, val in messages.items():
            if key in ("role", "type") or not isinstance(val, (str, list, dict)):
                out[key] = val
                continue
            out[key], k = redact_messages_struct(val)
            kinds += k
        return out, kinds
    if isinstance(messages, list):
        res = []
        for m in messages:
            r, k = redact_messages_struct(m)
            res.append(r)
            kinds += k
        return res, kinds
    return messages, kinds


def redact_metadata(value, depth: int = 0):
    """f0.7.5（结案留账 #3）：metadata 里的字符串值同样过原则 7。键名不动。"""
    if depth > 8:
        return value
    if isinstance(value, str):
        return redact_forbidden(value)[0]
    if isinstance(value, dict):
        return {k: redact_metadata(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_metadata(v, depth + 1) for v in value]
    return value


def message_texts(messages) -> list[tuple[str, str]]:
    if isinstance(messages, str):
        return [("user", messages)] if messages.strip() else []
    if isinstance(messages, dict):
        messages = [messages]
    out = []
    for m in messages or []:
        if isinstance(m, dict) and isinstance(m.get("content"), str) and m.get("role") != "system":
            out.append((str(m.get("role") or "user"), m["content"]))
        elif isinstance(m, str) and m.strip():
            out.append(("user", m))
    return out


# ══ P1 / P6 — worth and layer ═════════════════════════════════════════════
_HOST_NOISE_RE = re.compile(
    r"(?is)^\s*(?:\[SILENT\]|\[Image[:\s#][^\]]*\]|\[Request interrupted[^\]]*\]|"
    r"\[Your previous response had no visible output[^\]]*\]|No response requested\.?|"
    r"This session is being continued from a previous conversation|<system-reminder>|"
    r"Caveat: The messages below were generated|<command-name>|<local-command-stdout>|"
    r"<command-message>|\(no content\)|\[Pasted text[^\]]*\])")
_HOST_FRAGMENT_RE = re.compile(
    r"(?is)\[SILENT\]|\[Image[:\s#][^\]]*\]|\[Request interrupted[^\]]*\]|"
    r"\[Your previous response had no visible output[^\]]*\]|No response requested\.?|\(no content\)")
_ACK_WORDS = (
    "好的", "好了", "好吧", "好呀", "好啊", "好滴", "好", "嗯嗯", "嗯", "行", "行吧", "可以", "可以的",
    "继续", "接着", "继续吧", "收到", "谢谢", "多谢", "感谢", "辛苦了", "辛苦", "明白", "明白了", "懂了",
    "知道了", "了解", "对", "对的", "是的", "没问题", "批准", "同意", "做吧", "做起来", "开始", "开始吧",
    "去吧", "搞吧", "冲", "哈哈", "哈哈哈", "嘻嘻", "早", "早安", "晚安", "在吗", "你好", "嗨",
    "ok", "okay", "yes", "no", "yep", "nope", "thanks", "thank you", "thx", "go", "go on", "continue",
    "sure", "fine", "hi", "hello", "hey", "lol", "done", "next", "lgtm", "approved", "approve",
)
# A go-ahead ("批准，做起来") is still short, but it is the moment a decision was made:
# keep it in the daily layer instead of throwing it away with "好的/谢谢".
_APPROVAL_RE = re.compile(r"(?i)批准|同意|授权|准了|做吧|做起来|开工|开干|照做|执行吧|搞吧|就这么办|"
                          r"\b(?:approved?|go ahead|lgtm|ship it)\b")
_ACK_RE = re.compile("|".join(sorted((re.escape(w) for w in _ACK_WORDS), key=len, reverse=True)), re.I)
_PING_RE = re.compile(r"(?i)^\s*(?:say|reply(?:\s+with)?|respond(?:\s+with)?|只回复|请回复|回复|只说|请说)\s*[:：]?\s*"
                      r"[\"'“]?(?:ok|okay|yes|1|pong|收到|好)[\"'”]?\s*[。.!！]?\s*$")
_PUNCT_RE = re.compile(r"[\s\W_]+", re.UNICODE)
_EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200d]+")

_PROFILE_RE = re.compile(
    r"(?i)我(?:是|叫|姓|今年|出生|生日|住在|老家|的(?:名字|生日|邮箱|电话|手机|地址|老婆|老公|妻子|丈夫|"
    r"孩子|女儿|儿子|爸爸|妈妈|父母|家人|朋友|习惯|偏好|口味|爱好))|不?喜欢|讨厌|偏好|偏爱|最爱|过敏|忌口|"
    r"承诺|答应|约定|爱你|想你|习惯|一律|以后[^，。,.]{0,6}?(?:都|统一|一律)|统一用|今后|从今以后|永远不要|不要再|别再|每次都|铁律|规矩|原则|称呼我|叫我|"
    r"\b(?:i am|i'm|my name|i live|allergic|i prefer|i like|i hate|always|never|from now on)\b")
_TOPIC_RE = re.compile(
    r"(?i)决定|定下|拍板|方案|采用|改用|选用|选择|计划|项目|仓库|版本|配置|部署|端口|地址|路径|域名|服务器|"
    r"机器|账号|流程|架构|设计|规范|标准|原因是|因为|结论|教训|经验|必须|禁止|不许|负责|"
    r"\b(?:decid\w*|plan|project|repo\w*|config\w*|deploy\w*|server|domain|port|because|lesson)\b|"
    r"https?://|\b\d{1,3}(?:\.\d{1,3}){3}\b|(?:^|\s)~?/[\w.\-]+/")
_DAILY_RE = re.compile(
    r"(?i)刚才|刚刚|现在|正在|马上|稍等|等一下|这会儿|今天|今晚|昨天|今早|进度|汇报|报告|已经完成|搞定了?|跑完|"
    r"重启|报错|失败了|成功了|试试|试一下|测试一下|看看|看一下|检查一下|截图|预览|再试|稍后|待会|"
    r"\b(?:just now|right now|currently|today|status|running|retry|in progress|done)\b|"
    r"\d+(?:\.\d+)?\s*(?:%|ms|秒|分钟|小时|元|块钱|条|次|MB|GB|KB)")
_GENERALIZE_RE = re.compile(r"(?i)总是|一直都?|每次|从来|从不|向来|一贯|习惯|永远|\b(?:always|never|every time|usually)\b")


def _norm_short(text: str) -> str:
    return _PUNCT_RE.sub("", _EMOJI_RE.sub("", text or "")).lower()


def is_noise(text: str) -> str | None:
    """Reason code when the text carries nothing worth remembering, else None."""
    raw = (text or "").strip()
    if not raw:
        return "empty"
    if _HOST_NOISE_RE.match(raw):
        rest = _norm_short(_HOST_FRAGMENT_RE.sub("", raw))
        if raw.lower().startswith("this session is being continued"):
            return "host_compaction_copy"
        if len(rest) <= 2:
            return "host_machine_text"
    if _PING_RE.match(raw):
        return "test_ping"
    short = _norm_short(raw)
    if not short:
        return "punctuation_only"
    if len(short) <= 12:
        rest = _ACK_RE.sub("", short)
        if rest != short and len(rest) <= 3:
            return "confirmation" if _APPROVAL_RE.search(raw) else "ack"
    if len(short) <= 1:
        return "too_short"
    return None


def classify_layer(text: str) -> tuple[str, list[str]]:
    reasons: list[str] = []
    t = text or ""
    p = len(_PROFILE_RE.findall(t))
    tp = len(_TOPIC_RE.findall(t))
    d = len(_DAILY_RE.findall(t))
    if p:
        reasons.append(f"profile_cues:{p}")
    if tp:
        reasons.append(f"topic_cues:{tp}")
    if d:
        reasons.append(f"daily_cues:{d}")
    if p and p >= d:
        return "profile", reasons
    stripped = t.strip()
    if len(_norm_short(t)) < 40 and (stripped.endswith(("？", "?", "吗", "吗？")) or "是不是" in t):
        return "daily", reasons + ["short_question"]
    if d > tp + p:
        return "daily", reasons
    if tp or p or len(_norm_short(t)) >= 40:
        return "topic", reasons
    return "daily", reasons + ["short_unanchored"]


# ══ P2 — condition (scene / people / occasion) ═══════════════════════════
_OCCASION_RE = re.compile(
    r"(?i)工作日|周末|周[一二三四五六日天]|早上|上午|中午|下午|晚上|夜里|深夜|睡前|饭后|出差|旅行|旅游|假期|节假日|"
    r"春节|过年|生日|开会|上班|下班|通勤|在家|加班|运动时|开车时|"
    r"\b(?:on weekends?|weekdays?|in the morning|at night|when travel\w*|on trips?|at work|at home)\b")
_PLACE_RE = re.compile(r"(?:在|去|到|回)(?:(?P<a>[\u4e00-\u9fff]{2,4}?)(?=的时候|时候|出差|旅行|旅游|玩|开会)|"
                       r"(?P<b>公司|家里?|办公室|学校|机场|医院|健身房|咖啡馆|餐厅|酒店|老家|"
                       r"[\u4e00-\u9fff]{2,4}(?:市|省|区|县|站|店|馆|院|楼)))")
_PEOPLE_RE = re.compile(
    r"(?:和|跟|与|陪|带)(?P<p>[\u4e00-\u9fffA-Za-z]{1,6}?)(?:一起|吃|去|聊|见|玩|出去|出差|开会|旅行)|"
    r"(?P<f>老婆|老公|妻子|丈夫|孩子|儿子|女儿|爸爸|妈妈|父母|家人|朋友|同事|老板|客户|室友)")


def extract_condition(text: str) -> dict:
    t = text or ""
    cond: dict[str, list[str]] = {}
    occ = sorted({m.group(0) for m in _OCCASION_RE.finditer(t)})
    if occ:
        cond["occasion"] = occ[:4]
    places = sorted({m.group("a") or m.group("b") for m in _PLACE_RE.finditer(t)})
    if places:
        cond["place"] = places[:4]
    people = sorted({(m.group("p") or m.group("f")) for m in _PEOPLE_RE.finditer(t)
                     if (m.group("p") or m.group("f"))})
    if people:
        cond["people"] = people[:4]
    return cond


# ══ P3 — direct vs inferred ═══════════════════════════════════════════════
def source_of(md: dict | None, *, infer_used: bool = False) -> tuple[str, str]:
    """(source_type, basis).  An explicit caller value wins; otherwise any
    derived record (session summary, LLM extraction, reflection, bot relay)
    is inferred and everything else is what was said, verbatim."""
    md = md or {}
    st = str(md.get("source_type") or "").strip().lower()
    if st in ("direct", "inferred"):
        return st, str(md.get("basis") or ("caller_declared"))[:200]
    from ducky.origin_context import is_session_summary
    if is_session_summary(md) or md.get("kind") == "session_distill" or md.get("lane") == "distill":
        n = md.get("distill_source_count")
        return "inferred", f"session_distill:{n} turns" if n else "session_distill"
    if infer_used:
        return "inferred", "llm_extraction"
    if md.get("_origin_is_bot"):
        return "inferred", "relayed_by_bot"
    src = str(md.get("source") or "")
    if src:
        from ducky.epistemic import EPISTEMIC_REASONED, resolve_epistemic
        if resolve_epistemic(src) == EPISTEMIC_REASONED:
            return "inferred", f"source:{src[:60]}"
    return "direct", "verbatim_turn"


def legacy_inferred(md: dict | None) -> bool:
    md = md or {}
    if md.get("source_type") in ("direct", "inferred"):
        return md.get("source_type") == "inferred"
    return source_of(md)[0] == "inferred"


# ══ classification entry points ═══════════════════════════════════════════
def classify(text: str, role: str = "user", md: dict | None = None, *, infer_used: bool = False) -> dict:
    """Pure: one stored text → verdict dict (no I/O)."""
    md = md or {}
    noise = is_noise(text)
    source_type, basis = source_of(md, infer_used=infer_used)
    reasons: list[str] = []
    if noise == "confirmation":
        layer, verdict, worth = "daily", "daily", 0.2
        reasons.append("confirmation")
    elif noise:
        layer, verdict, worth = "daily", "reject", 0.05
        reasons.append(f"noise:{noise}")
    else:
        layer, lr = classify_layer(text)
        reasons += lr
        worth = {"profile": 0.85, "topic": 0.6, "daily": 0.3}[layer]
        verdict = "daily" if layer == "daily" else "keep"
    if role and role != "user":
        reasons.append(f"role:{role}")
    if source_type == "inferred" and _GENERALIZE_RE.search(text or ""):
        reasons.append("generalization_in_inference")
    try:
        from ducky.salience.core import _detect_lane
        lane = _detect_lane(text or "")
    except (ImportError, AttributeError, TypeError, ValueError):   # salience is advisory here
        lane = ""
    if lane == "preference" and verdict != "reject":
        worth = min(1.0, worth + 0.05)
    cond = extract_condition(text) if verdict != "reject" else {}
    return {"verdict": verdict, "worth": round(worth, 3), "layer": layer, "reasons": reasons,
            "source_type": source_type, "basis": basis, "condition": cond, "lane": lane,
            "role": role or "user"}


def enrich_metadata(text: str, role: str, md: dict | None, *, infer_used: bool = False) -> dict:
    """Per-memory metadata stamp (P2/P3/P6).  Additive; caller values win."""
    out = dict(md or {})
    if policy_mode() == "off":
        return out
    try:
        c = classify(text, role, out, infer_used=infer_used)
    except Exception as exc:     # fail-open: the write goes ahead untagged
        logger.warning("write_policy classify failed: %s", type(exc).__name__)
        return out
    out.setdefault("source_type", c["source_type"])
    out.setdefault("basis", c["basis"])
    out.setdefault("layer", c["layer"])
    if c["condition"] and "condition" not in out:
        out["condition"] = c["condition"]
    return out


# ══ P4 / P5 — supersede and conflict (shadow) ════════════════════════════
_TOPIC_KEY_RES = (
    re.compile(r"(?:^|[，,；;。\s])(?:那|那么)?(?:我的|我们的|我家的?|咱们的?|你的)?(?P<k>[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9_\- ]{1,15}?)"
               r"\s*(?:现在|目前|已经|已|以后|从今天起|从现在起)?(?:都)?(?P<verb>改成|改为|换成|换为|变成|变为|设为|设成|定为|调成|调为|是)\s*"
               r"(?P<v>[^，,。；;！!？?\n]{1,24})(?P<end>[，,。；;！!？?\n]|$)"),
)
_PLACE_KEY_RES = (
    ("住处", re.compile(r"(?:我|我们)?(?:现在|目前|已经|以后)?(?:住在|搬到了?|搬去了?|定居在?)(?P<v>[\u4e00-\u9fffA-Za-z]{2,12})")),
    ("工作地点", re.compile(r"(?:我|我们)?(?:现在|目前)?在(?P<v>[\u4e00-\u9fffA-Za-z]{2,12}?)(?:工作|上班)")),
)
_PLACE_ANCHORS = {"住处": ["住在", "搬到", "搬去", "定居"], "工作地点": ["工作", "上班"]}
_KEY_STOP = {"而且", "而不", "是不", "但是", "就是", "还是", "可是", "只是", "然后", "所以", "因为", "如果", "不过",
             "这", "那", "它", "这个", "那个", "问题", "原因", "结果", "目标", "意思", "关键", "重点", "区别",
             "答案", "情况", "事情", "东西", "这里", "那里", "什么", "哪个", "谁", "现在", "今天", "也", "就",
             "还", "都", "最", "真", "不", "没", "有", "只", "我", "你", "他", "她", "我们", "你们", "大家"}
_KEY_PREFIX_RE = re.compile(r"^(?:我的|我们的|我|你的|用户的|用户|现在的?|目前的?|新的|旧的|的)+")
_KEY_SUFFIX_RE = re.compile(r"(?:可能|大概|应该|似乎|也许|好像|估计|或许|一般|通常|都|也|就|还)+$")
_VAL_TAIL_RE = re.compile(r"[了啦呢吧呀哦啊嘛的。.!！~～\s]+$")
CORRECTION_RE = re.compile(r"改成|改为|换成|换为|变成|变为|不再|以后改|现在改|更正|纠正|其实是|应该是|搬到|搬去")


def topic_keys(text: str) -> dict[str, str]:
    keys: dict[str, str] = {}
    t = text or ""
    if len(t) > 400:
        return keys
    for rx in _TOPIC_KEY_RES:
        for m in rx.finditer(t):
            k = _KEY_SUFFIX_RE.sub("", _KEY_PREFIX_RE.sub("", m.group("k").strip()).strip()).strip().lower()
            v = _VAL_TAIL_RE.sub("", m.group("v").strip()).strip().lower()
            # 问句（是不是/…吗/？）、否定式（是不…）、长句不是在陈述「某属性=某值」
            if (m.group("end") in ("？", "?") or v.endswith(("吗", "么")) or v.startswith(("不", "否"))
                    or k.endswith(("是", "不")) or len(v) > 24):
                continue
            if 2 <= len(k) <= 12 and k not in _KEY_STOP and v and k not in v:
                keys.setdefault(k, v)
    for name, rx in _PLACE_KEY_RES:
        m = rx.search(t)
        if m:
            keys.setdefault(name, _VAL_TAIL_RE.sub("", m.group("v")).lower())
    return keys


_SUBJ_RE = re.compile(r"^(?:我们|我|用户|他们|他|她|本人)?(?:可能|大概|似乎|也许|好像|估计|应该|其实|现在|以后|已经|一直|还是|也)*")
_NEG_RE = re.compile(r"不再|不|没有|没|别|从不")


def _core_clause(text: str) -> str:
    first = re.split(r"[，,。；;！!？?\n]", (text or "").strip(), maxsplit=1)[0]
    core = _SUBJ_RE.sub("", first)
    return _VAL_TAIL_RE.sub("", core).strip()


def _polarity_flip(a: str, b: str) -> bool:
    """「我喜欢X」↔「用户可能不喜欢X(了)」：去掉主语/语气后只差一个否定词。"""
    ca, cb = _core_clause(a), _core_clause(b)
    if not ca or not cb or ca == cb:
        return False
    pa, pb = _NEG_RE.sub("", ca, count=1), _NEG_RE.sub("", cb, count=1)
    return len(pa) >= 3 and pa == pb and (pa == ca) != (pb == cb)


def same_topic_change(new_text: str, old_text: str) -> dict | None:
    """Does ``new_text`` restate a topic of ``old_text`` with another value?"""
    nk, ok = topic_keys(new_text), topic_keys(old_text)
    for k, v in nk.items():
        if k in ok and ok[k] != v and v not in ok[k] and ok[k] not in v:
            return {"kind": "value_change", "key": k}
    if _polarity_flip(new_text, old_text):
        return {"kind": "negation", "key": _NEG_RE.sub("", _core_clause(new_text), count=1)[:24]}
    try:
        from ducky.salience.conflict import judge_pair
        j = judge_pair(new_text, old_text)
    except (ImportError, AttributeError, TypeError, ValueError, KeyError):   # shadow-only signal
        j = None
    if j:
        return {"kind": j.get("kind", "contradiction"), "key": j.get("word_pair", "")}
    return None


def resolve_conflict(new_source: str, old_source: str, new_at: str = "", old_at: str = "") -> str:
    """P5: direct > inferred; newer > older; a tie (or an inference against
    something the user said) is not ours to decide → pending_confirm."""
    if new_at and old_at and new_at[:19] == old_at[:19]:
        return "pending_confirm"
    if new_source == "direct":
        return "supersede"
    if old_source == "inferred":
        return "supersede"
    return "pending_confirm"


def supersede_candidates(new_text: str, new_md: dict, rows: Iterable[dict]) -> list[dict]:
    """rows: older memories of the same scope {id, content, metadata, created_at}."""
    out: list[dict] = []
    new_src = new_md.get("source_type") or source_of(new_md)[0]
    rows = list(rows)
    # A "key" found in most neighbouring memories is a subject (a name the
    # user keeps addressing), not a property whose value can change.
    common = {k for k in topic_keys(new_text)
              if sum(1 for r in rows if k in (r.get("content") or "").lower()) > max(3, len(rows) // 3)}
    for r in rows:
        old = r.get("content") or ""
        if not old or old == new_text or is_noise(old):
            continue
        hit = same_topic_change(new_text, old)
        if not hit or (hit["kind"] == "value_change" and hit["key"] in common):
            continue
        omd = r.get("metadata") or {}
        old_src = omd.get("source_type") or source_of(omd)[0]
        decision = resolve_conflict(new_src, old_src, new_md.get("_created_at", ""), r.get("created_at") or "")
        out.append({"old_id": r.get("id"), "decision": decision, **hit,
                    "new_source": new_src, "old_source": old_src})
        if len(out) >= 3:
            break
    return out


# ══ storage: <DATA_DIR>/write_policy.db ═══════════════════════════════════
_DDL = """
CREATE TABLE IF NOT EXISTS verdicts (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  user_id TEXT NOT NULL, bank_id TEXT NOT NULL DEFAULT 'default',
  memory_id TEXT, origin_agent TEXT, session_id TEXT, role TEXT,
  content_sha TEXT, length INTEGER,
  verdict TEXT NOT NULL, worth REAL, layer TEXT, source_type TEXT, basis TEXT,
  reasons TEXT NOT NULL DEFAULT '[]', condition TEXT NOT NULL DEFAULT '{}',
  redacted TEXT NOT NULL DEFAULT '[]', hard_block TEXT,
  blacklist_rule INTEGER, supersede TEXT NOT NULL DEFAULT '[]', mode TEXT NOT NULL DEFAULT 'shadow'
);
CREATE INDEX IF NOT EXISTS idx_wp_verdicts_scope ON verdicts(user_id, bank_id, ts);
CREATE INDEX IF NOT EXISTS idx_wp_verdicts_mem ON verdicts(memory_id);
CREATE TABLE IF NOT EXISTS forget_rules (
  id INTEGER PRIMARY KEY,
  user_id TEXT NOT NULL, bank_id TEXT NOT NULL DEFAULT 'default',
  phrase TEXT NOT NULL, created_at TEXT NOT NULL, created_by TEXT,
  UNIQUE (user_id, bank_id, phrase)
);
CREATE TABLE IF NOT EXISTS key_events (
  day TEXT NOT NULL, key_agent TEXT NOT NULL, key_fp TEXT NOT NULL,
  target_user TEXT NOT NULL, caller TEXT NOT NULL, action TEXT NOT NULL,
  outcome TEXT NOT NULL, n INTEGER NOT NULL DEFAULT 0, last_ts TEXT,
  PRIMARY KEY (day, key_fp, target_user, caller, action, outcome)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""
SCHEMA_VERSION = 1
_db_lock = threading.Lock()
_ready: set[str] = set()


def db_path() -> str:
    from ducky.utils import DATA_DIR
    return os.path.join(os.environ.get("AIDUMEM_DATA_DIR") or DATA_DIR, "write_policy.db")


def connect() -> sqlite3.Connection:
    path = db_path()
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    if path not in _ready:
        with _db_lock:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_DDL)
            conn.execute("INSERT OR IGNORE INTO meta VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
            conn.commit()
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            _ready.add(path)
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:32]


def _scope(user_id: str, bank_id: str) -> tuple[str, str]:
    from ducky.bank_contract import normalize_bank_id, normalize_user_id
    return normalize_user_id(user_id or "default"), normalize_bank_id(bank_id or "default")


def _scope_sql(user_id: str, bank_id: str, alias: str = "") -> tuple[str, list]:
    """Tenant predicate from the one canonical entry (``scope_sql.scope_clause``):
    returns (" AND user_id=? AND bank_id=?", params) — never hand-rolled here."""
    from ducky.bank_contract import make_scope
    from ducky.scope_sql import scope_clause
    pred, params = scope_clause(make_scope(*_scope(user_id, bank_id)), alias=alias)
    return pred, list(params)


def log_verdict(*, user_id: str, bank_id: str, memory_id: str | None, text: str, verdict: dict,
                md: dict | None = None, hard_block: str | None = None, blacklist_rule: int | None = None,
                supersede: list | None = None) -> None:
    md = md or {}
    uid, bid = _scope(user_id, bank_id)
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO verdicts (ts,user_id,bank_id,memory_id,origin_agent,session_id,role,content_sha,length,"
            "verdict,worth,layer,source_type,basis,reasons,condition,redacted,hard_block,blacklist_rule,supersede,mode)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_now(), uid, bid, memory_id, str(md.get("_origin_agent") or md.get("source") or "")[:60] or None,
             str(md.get("_origin_session_id") or md.get("session_id") or "")[:120] or None,
             verdict.get("role"), _sha(text) if text else None, len(text or ""),
             verdict.get("verdict", "keep"), verdict.get("worth"), verdict.get("layer"),
             verdict.get("source_type"), verdict.get("basis"),
             json.dumps(verdict.get("reasons") or [], ensure_ascii=False),
             json.dumps(verdict.get("condition") or {}, ensure_ascii=False),
             json.dumps(list(md.get("redacted") or []), ensure_ascii=False),
             hard_block, blacklist_rule, json.dumps(supersede or [], ensure_ascii=False), "shadow"))
        conn.commit()
    finally:
        conn.close()


def log_hard_block(*, user_id: str, bank_id: str, reason: str, text: str, md: dict | None = None) -> None:
    """A rejected write leaves only its reason code and a hash — never the text."""
    try:
        log_verdict(user_id=user_id, bank_id=bank_id, memory_id=None, text=text,
                    verdict={"verdict": "reject", "reasons": [f"hard_block:{reason}"], "role": None},
                    md=md, hard_block=reason)
    except (sqlite3.Error, OSError, ValueError, TypeError) as exc:   # the 422 still goes out
        logger.warning("write_policy hard-block log failed: %s", type(exc).__name__)


# ── P8 forget rules ───────────────────────────────────────────────────────
def _norm_phrase(s: str) -> str:
    return _PUNCT_RE.sub("", (s or "")).lower()


def add_rule(user_id: str, bank_id: str, phrase: str, created_by: str = "") -> int:
    p = (phrase or "").strip()
    if len(_norm_phrase(p)) < 2:
        raise ValueError("forget rule phrase too short")
    p = p[:200]
    uid, bid = _scope(user_id, bank_id)
    pred, params = _scope_sql(uid, bid)
    conn = connect()
    try:
        conn.execute("INSERT OR IGNORE INTO forget_rules (user_id,bank_id,phrase,created_at,created_by)"
                     " VALUES (?,?,?,?,?)", (uid, bid, p, _now(), (created_by or "")[:60]))
        conn.commit()
        return conn.execute("SELECT id FROM forget_rules WHERE phrase=?" + pred,
                            [p, *params]).fetchone()[0]
    finally:
        conn.close()


def list_rules(user_id: str, bank_id: str) -> list[dict]:
    pred, params = _scope_sql(user_id, bank_id)
    conn = connect()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, phrase, created_at FROM forget_rules WHERE 1=1" + pred + " ORDER BY id", params)]
    finally:
        conn.close()


def delete_rule(user_id: str, bank_id: str, rule_id: int) -> int:
    pred, params = _scope_sql(user_id, bank_id)
    conn = connect()
    try:
        n = conn.execute("DELETE FROM forget_rules WHERE id=?" + pred, [int(rule_id), *params]).rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def blacklist_hit(text: str, user_id: str, bank_id: str) -> int | None:
    """Id of the first forget rule the text matches (all of the rule's words
    present, punctuation/case-insensitive), else None."""
    if not text:
        return None
    norm = _norm_phrase(text)
    for r in list_rules(user_id, bank_id):
        parts = [_norm_phrase(w) for w in re.split(r"[\s,，;；]+", r["phrase"]) if _norm_phrase(w)]
        if parts and all(p in norm for p in parts):
            return int(r["id"])
    return None


def purge_memory(memory_ids: Iterable[str], user_id: str, bank_id: str) -> int:
    ids = [str(i) for i in (memory_ids or []) if str(i or "").strip()]
    if not ids or not os.path.exists(db_path()):
        return 0
    pred, params = _scope_sql(user_id, bank_id)
    conn = connect()
    try:
        n = 0
        for mid in ids:     # one statement per id: no SQL text is built from the id count
            n += conn.execute("DELETE FROM verdicts WHERE memory_id=?" + pred, [mid, *params]).rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def purge_scope(user_id: str, bank_id: str) -> int:
    if not os.path.exists(db_path()):
        return 0
    # f0.7.3: delete_all drops the verdict log but keeps forget_rules — "never store
    # this again" is an instruction about the future, and wiping every memory must
    # not silently re-open the door (self-audit leftover, 2026-11-02).
    pred, params = _scope_sql(user_id, bank_id)
    conn = connect()
    try:
        n = conn.execute("DELETE FROM verdicts WHERE 1=1" + pred, params).rowcount
        conn.commit()
        return n
    finally:
        conn.close()


# ── write observer (called by the native kernel after a committed add) ─────
def observe_written(kernel: Any, written: list[tuple[str, str, str, dict]], user_id: str, bank_id: str) -> None:
    """written: [(memory_id, role, text, metadata)].  Shadow only; never raises."""
    if policy_mode() == "off" or not written:
        return
    for mem_id, role, text, md in written:
        try:
            v = classify(text, role, md)
            v["source_type"] = md.get("source_type") or v["source_type"]
            v["basis"] = md.get("basis") or v["basis"]
            v["layer"] = md.get("layer") or v["layer"]
            rule = blacklist_hit(text, user_id, bank_id)
            if rule is not None:
                v["reasons"].append("forget_rule")
            sup: list = []
            if v["verdict"] != "reject" and (role == "user" or v["source_type"] == "inferred"):
                sup = supersede_candidates(text, {**md, "_created_at": md.get("_created_at", "")},
                                           _older_rows(kernel, mem_id, text, user_id, bank_id))
                if sup:
                    v["reasons"].append("would_supersede" if any(s["decision"] == "supersede" for s in sup)
                                        else "pending_confirm")
            log_verdict(user_id=user_id, bank_id=bank_id, memory_id=mem_id, text=text, verdict=v, md=md,
                        blacklist_rule=rule, supersede=sup)
        except Exception as exc:
            logger.warning("write_policy observe failed: %s", type(exc).__name__)


def _older_rows(kernel: Any, mem_id: str, text: str, user_id: str, bank_id: str, n: int = 12) -> list[dict]:
    if kernel is None or not hasattr(kernel, "_fts"):
        return []
    q = text[:200]
    pred, params = _scope_sql(user_id, bank_id, alias="m")
    where = "m.memory_type!='VERBATIM' AND m.id!=?" + pred
    args = [mem_id, *params]
    anchors = []
    for k in topic_keys(text):
        anchors += _PLACE_ANCHORS.get(k, [k])
    with kernel._lock:
        mids = list(kernel._fts(q, where, args, n))
        for a in anchors[:4]:       # 同一话题换了说法时 trigram 可能一个都对不上,按话题锚点补候选
            mids += [r[0] for r in kernel.conn.execute(
                "SELECT m.mid FROM memories m WHERE " + where + " AND m.content LIKE ? ORDER BY m.mid DESC LIMIT 6",
                (*args, f"%{a}%")) if r[0] not in mids]
        rows = [kernel.conn.execute("SELECT id, content, metadata, created_at FROM memories WHERE mid=?",
                                    (m,)).fetchone() for m in dict.fromkeys(mids)]
    rows = [r for r in rows if r is not None]
    out = []
    for r in rows:
        try:
            md = json.loads(r["metadata"] or "{}")
        except ValueError:
            md = {}
        out.append({"id": r["id"], "content": r["content"], "metadata": md if isinstance(md, dict) else {},
                    "created_at": r["created_at"]})
    return out


# ── queries for the misjudgment report ─────────────────────────────────────
def list_verdicts(user_id: str, bank_id: str, *, since: str = "", verdict: str = "", reason: str = "",
                  limit: int = 100) -> list[dict]:
    pred, args = _scope_sql(user_id, bank_id)
    sql = "SELECT * FROM verdicts WHERE 1=1" + pred
    if since:
        sql += " AND ts>=?"
        args.append(since)
    if verdict:
        sql += " AND verdict=?"
        args.append(verdict)
    if reason:
        sql += " AND reasons LIKE ?"
        args.append(f"%{reason}%")
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit or 100), 1000)))
    conn = connect()
    try:
        rows = []
        for r in conn.execute(sql, args):
            d = dict(r)
            for k in ("reasons", "condition", "redacted", "supersede"):
                try:
                    d[k] = json.loads(d.get(k) or "null")
                except ValueError:
                    pass
            rows.append(d)
        return rows
    finally:
        conn.close()


def summary(user_id: str, bank_id: str, *, since: str = "") -> dict:
    uid, bid = _scope(user_id, bank_id)
    conn = connect()
    try:
        pred, a = _scope_sql(uid, bid)
        base = "FROM verdicts WHERE 1=1" + pred + (" AND ts>=?" if since else "")
        a = a + ([since] if since else [])

        def _grouped(col: str) -> dict:     # col is one of three literal column names below
            return {r[0] or "": r[1] for r in conn.execute("SELECT " + col + ", count(*) " + base + " GROUP BY 1", a)}

        def _count(cond: str) -> int:
            return conn.execute("SELECT count(*) " + base + " AND " + cond, a).fetchone()[0]
        by_verdict, by_layer, by_source = _grouped("verdict"), _grouped("layer"), _grouped("source_type")
        sup = _count("supersede!='[]'")
        bl = _count("blacklist_rule IS NOT NULL")
        hb = {r[0]: r[1] for r in conn.execute("SELECT hard_block, count(*) " + base
                                               + " AND hard_block IS NOT NULL GROUP BY 1", a)}
        red = _count("redacted!='[]'")
        keys = [dict(r) for r in conn.execute(        # only events that touch this scope's identity
            "SELECT key_agent, target_user, caller, action, outcome, sum(n) n FROM key_events"
            " WHERE (target_user=? OR caller=?)" + (" AND day>=?" if since else "")
            + " GROUP BY 1,2,3,4,5 ORDER BY n DESC LIMIT 50", [uid, uid] + ([since[:10]] if since else []))]
        return {"user_id": uid, "bank_id": bid, "since": since or None, "mode": policy_mode(),
                "by_verdict": by_verdict, "by_layer": by_layer, "by_source": by_source,
                "would_supersede_or_pending": sup, "forget_rule_hits": bl, "hard_blocks": hb,
                "redacted_writes": red, "key_events": keys}
    finally:
        conn.close()


# ══ P3 / P6 / P9 — recall side ═══════════════════════════════════════════
def annotate_results(results: list) -> None:
    """Mark each recall row with its provenance (P3) and layer (P6)."""
    for it in results or []:
        if not isinstance(it, dict):
            continue
        md = it.get("metadata") if isinstance(it.get("metadata"), dict) else {}
        try:
            inferred = legacy_inferred(md)
        except (ImportError, AttributeError, TypeError, ValueError):
            inferred = False
        it["source_type"] = "inferred" if inferred else (md.get("source_type") or "direct")
        it["inferred"] = inferred
        if inferred:
            it["basis"] = md.get("basis") or source_of(md)[1]
        if md.get("layer") in LAYERS:
            it["layer"] = md["layer"]


def annotate_and_filter(results: list, limit: int | None = None) -> list:
    """Recall-side hook for the HTTP layer: label rows (P3/P6) and, when
    flagged on, apply the daily/precise filters (P9).  Labels are advisory,
    so any failure returns the rows untouched — recall never breaks on it."""
    try:
        annotate_results(results)
        return precise_filter(results, limit) if limit is not None else results
    except Exception as exc:     # contract: recall output is never lost to a labelling fault
        logger.debug("write_policy recall annotate skipped: %s", type(exc).__name__)
        return results


def exclude_daily_on() -> bool:
    return _flag("AIDUMEI_RECALL_EXCLUDE_DAILY")


def precise_on() -> bool:
    return _flag("AIDUMEI_RECALL_PRECISE")


def precise_k() -> int:
    from ducky.env_config import int_env
    return int_env("AIDUMEI_RECALL_PRECISE_K", 5, minimum=1)


def precise_ratio() -> float:
    from ducky.env_config import float_env
    return float_env("AIDUMEI_RECALL_PRECISE_RATIO", 0.5, minimum=0.0, maximum=1.0)


def _score(it: dict) -> float:
    for k in ("score", "_final_score", "rerank_score"):
        v = it.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return 0.0


def precise_filter(results: list, limit: int) -> list:
    """P9 (flagged): drop daily-layer rows when asked, then keep at most
    ``precise_k`` rows whose score is within reach of the best one."""
    rows = list(results or [])
    if exclude_daily_on():
        kept = [r for r in rows if not (isinstance(r, dict) and r.get("layer") == "daily")]
        rows = kept if kept else rows[:1]
    if precise_on() and rows:
        top = max(_score(r) for r in rows if isinstance(r, dict)) if rows else 0.0
        floor = top * precise_ratio()
        rows = [r for i, r in enumerate(rows) if i == 0 or not isinstance(r, dict) or _score(r) >= floor]
        rows = rows[:min(limit, precise_k())]
    return rows
