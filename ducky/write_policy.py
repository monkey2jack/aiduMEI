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
import struct
import threading
import unicodedata
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


# f0.8 ②：影子周之后按条开拦截。每条被开的原则都能用环境变量拨回影子，不改代码：
#   AIDUMEI_WRITE_POLICY_P1=enforce|shadow       机器噪声两类（机器文本 / 上下文续接说明）
#   AIDUMEI_WRITE_POLICY_P8=enforce|shadow       忘记黑名单（本人要求忘记的）
#   AIDUMEI_WRITE_POLICY_THINKING=enforce|shadow 会话精华里的模型思考过程
# AIDUMEI_WRITE_POLICY=off 时全部不生效（P7 打码除外，它有自己的开关）。
# 环境变量名必须是字面量（ducky.env_registry 按 AST 字符串常量登记）。
_ENFORCE_ENV = {"P1": "AIDUMEI_WRITE_POLICY_P1", "P8": "AIDUMEI_WRITE_POLICY_P8",
                "THINKING": "AIDUMEI_WRITE_POLICY_THINKING"}


def principle_mode(name: str) -> str:
    """enforce | shadow | off for one enforceable principle (P1 / P8 / THINKING); default enforce."""
    if policy_mode() == "off":
        return "off"
    env = _ENFORCE_ENV.get(name.upper())
    if env is None:
        return "shadow"
    # f0.8+（外审 R3-06）：只认显式的降级值；空值 / 拼错一律按 enforce（fail-closed）——
    # systemd EnvironmentFile / compose 里留一行 `KEY=` 不许悄悄把拦截降成影子。
    val = (os.environ.get(env) or "").strip().lower()
    return "shadow" if val in ("shadow", "off", "0", "false", "no") else "enforce"


def gate_ttl_days() -> int:
    """Gated writes stay restorable in the tombstone table this many days (default 7)."""
    from ducky.env_config import int_env
    return int_env("AIDUMEI_WRITE_POLICY_GATE_TTL_DAYS", 7, minimum=1)


def secret_guard_on() -> bool:
    """AIDUMEI_SECRET_GUARD: on (default).  P7 is a hard rule; off only as an
    explicit escape hatch for a deployment that stores credentials on purpose."""
    return os.environ.get("AIDUMEI_SECRET_GUARD", "on").strip().lower() not in ("off", "0", "false", "no")


# f0.8+（外审 R3-05）：会话精华的长度上限。真实精华 ≤400 字（llm）或两段 ≤120 字的引文（fallback），
# 4000 字足够宽；即便服务端验过签名，也不许借「精华」身份存整篇原文。
SUMMARY_MAX_CHARS = 4000


def max_raw_chars() -> int:
    from ducky.env_config import int_env
    return int_env("AIDUMEI_WRITE_MAX_RAW_CHARS", 20000, minimum=1000)


# ══ P7 — never store secrets, ID documents, signatures, raw dumps ════════
REDACT_FMT = "[redacted:{}]"

# Specific, prefixed credential shapes: near-zero false positives.
# A pattern with a named group ``v`` redacts only that group; otherwise the whole match.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("webhook", re.compile(
        r"https?://(?:open\.)?(?:feishu\.cn|larksuite\.com)/open-apis/bot/(?:v2/)?hook/"
        r"[A-Za-z0-9_\-]{8,}(?:\?[^\s<>\"']*)?", re.I)),
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
    # f0.7.6（外审 R2-07）：补 sk_test_ / rk_test_。
    ("api_key", re.compile(r"\b(?:hf|r8|pk_live|sk_live|rk_live|sk_test|rk_test)_[A-Za-z0-9]{20,}\b")),
    ("api_key", re.compile(r"(?i)\b(?:Shared)?Access?Key\s*=\s*(?P<v>[A-Za-z0-9+/]{20,}={0,2})")),
    ("bot_token", re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_\-]{30,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+(?=[A-Za-z0-9._~+/\-]*\d)[A-Za-z0-9._~+/\-]{20,}=*")),
    ("basic_auth", re.compile(r"\bBasic\s+(?P<v>(?=[A-Za-z0-9+/]*[0-9+/=])[A-Za-z0-9+/]{12,}={0,2})")),
    ("cookie", re.compile(
        r"(?i)\b(?:set-)?cookie\s*:\s*(?P<v>[^\s=;,]+=[^\s;]+(?:;\s*[^\s=;]+=?[^\s;]*)*)")),
    ("private_key", re.compile(
        r"(?i)(?:私钥|private[\s_\-]?key)[^\n]{0,24}?(?P<v>(?<![0-9A-Fa-f])(?:0x)?[0-9A-Fa-f]{64}(?![0-9A-Fa-f]))")),
    ("seed_phrase", re.compile(
        # f0.8+（外审 R3-10）：关键词与词串之间放宽到 40 字（「这是我钱包的助记词，千万别告诉任何人，内容如下：…」）
        r"(?i)(?:助记词|mnemonic|seed[\s_\-]?phrase|recovery[\s_\-]?phrase)[^\n]{0,40}?"
        r"(?P<v>(?<![A-Za-z])[a-z]{3,8}(?:[ \t,]+[a-z]{3,8}){11,23}(?![A-Za-z]))")),
    ("embedded_image", re.compile(r"data:image/[A-Za-z0-9.+\-]+;base64,[A-Za-z0-9+/=\s]{200,}")),
    ("url_credentials", re.compile(r"(?<=://)[^\s/:@]{1,64}:[^\s/@]{3,128}(?=@)")),
)
# A seed phrase is BIP39-like words, not an English sentence that follows the word "mnemonic".
_SEED_STOPWORDS = frozenset(
    "the and is are was were to of in for with that this it you have has when what how can will be not "
    "but or if my your our on at as from by an do does just very easy hard help helps".split())

# ── f0.7.6（外审 R2-01..R2-07）：凭据「键 + 分隔 + 值」的识别 ─────────────
# 键名按 _ - . 空格与驼峰拆词，任一词（或相邻两词）属于下表就是凭据键：
#   pw     口令类：纯小写字母、中文、≥4 位纯数字都算口令；
#   pin    PIN / CVV：≥4 位数字或「强」值，纯字母不算；
#   secret secret / 私钥 / 助记词：纯字母只在显式 : = 下算；
#   token  token / api key / cookie：纯字母需显式分隔且 ≥16 位。
_KEY_TIERS: dict[str, str] = {
    **dict.fromkeys(("password", "passwords", "passwd", "pwd", "passphrase", "passcode"), "pw"),
    **dict.fromkeys(("pin", "cvv", "cvc"), "pin"),
    **dict.fromkeys(("secret", "secrets", "privatekey", "secretkey", "mnemonic"), "secret"),
    **dict.fromkeys(("token", "tokens", "apikey", "accesskey", "credential", "credentials",
                     "cookie", "authorization"), "token"),
}
_KEY_PAIRS: dict[tuple[str, str], str] = {
    ("api", "key"): "token", ("access", "key"): "token", ("account", "key"): "token",
    ("private", "key"): "secret", ("secret", "key"): "secret",
    ("seed", "phrase"): "secret", ("recovery", "phrase"): "secret",
}
# 这些词出现在键名里时，键描述的是口令的属性而不是口令本身（password_hint / max_tokens）。
_KEY_NEG = frozenset({
    "hint", "hints", "policy", "policies", "length", "len", "reset", "required", "strength", "manager",
    "field", "prompt", "expiry", "expires", "expired", "limit", "limits", "max", "min", "count", "type",
    "name", "file", "path", "url", "uri", "endpoint", "header", "usage", "rule", "rules", "format",
    "regex", "pattern", "id", "ids", "ttl", "timeout", "budget", "size", "economy", "rotation", "env",
})
_TIER_RANK = {"pw": 4, "pin": 3, "secret": 2, "token": 1}
_CJK_KEY_RE = re.compile("密码|口令|安全码|助记词|密钥|秘钥|私钥|令牌")
_CJK_KEY_TIER = {"密码": "pw", "口令": "pw", "安全码": "pin", "助记词": "secret",
                 "密钥": "secret", "秘钥": "secret", "私钥": "secret", "令牌": "token"}
# 中文键后紧跟这些字：说的是口令的属性（密码学、密码提示、密码长度），不是口令本身。
_CJK_KEY_NEG_RE = re.compile(r"学|提示|策略|规则|长度|强度|管理|错误|重置|找回|保护|输入框|复杂度|位数|格式|要求|框")
_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]*")
_CAMEL_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_KEY_STEM_RE = re.compile(r"(?i)pass|pwd|secret|token|key|credential|mnemonic|pin|cvv|cvc|cookie|authoriz|phrase")
# 分隔：verb 动词（改成/设为…）、nat 自然语言（is/是/为/叫，可再跟 : =）、exp 显式（: = => ->）、
# weak 弱分隔（, ——）。键名后允许 JSON/markdown 的收尾引号与 `码/号`（PIN码）。
_SEP_RE = re.compile(
    "(?:码|号)?[*_`\"'\u201d\u2019\u300d\u300f\u300b]{0,3}[ \t]*"
    r"(?:(?P<verb>(?:改成|改为|设为|设置为|设置成|设成|换成|换为|更新为|更改为|修改为)了?)"
    r"|(?P<nat>(?<![A-Za-z])(?:is|was|IS|Is|Was|WAS)(?![A-Za-z])|是|为|叫)(?:[ \t]*(?P<natx>=>|->|[:=]))?"
    r"|(?P<exp>=>|->|[:=])"
    r"|(?P<weak>,|——|—))"
    r"[ \t]*(?:\n[ \t]*)?[*_`]{0,3}[ \t]*")
# 「the password for my gmail is X」：英文键与分隔之间允许一小段介词短语；此时值必须「强」。
_GAP_RE = re.compile(
    r"[ \t]+(?:for|of|to|on|at|in)[ \t]+(?:[\w.@'\-]+[ \t]+){0,3}?[\w.@'\-]+[ \t]*"
    r"(?=(?:is|was)(?![A-Za-z])|[:=])", re.I)
_QUOTES = {'"': '"', "'": "'", "`": "`", "\u201c": "\u201d", "\u2018": "\u2019",
           "\u300c": "\u300d", "\u300e": "\u300f", "\u300a": "\u300b", "<": ">"}
_VALUE_STOP = set(" \t\r\n,;\u3002\u3001\"'`\u201c\u201d\u2018\u2019\u300c\u300d\u300e\u300f\u300a\u300b<>")
_SYMBOLS = set("!@#$%^&*+/=~?:()[]{}|\\")
_PLACEHOLDER_WORDS = frozenset({"changeme", "example", "placeholder", "redacted", "none", "null",
                                "true", "false", "nil", "todo", "tbd"})
_PLACEHOLDER_TOKENS = frozenset({"your", "my", "placeholder", "example", "changeme", "redacted"})
_PLACEHOLDER_RE = re.compile(
    r"^(?:[xX]{3,}|\*{3,}|\.{3,}|\u2026+|\$\{\w+\}|\$[A-Z_][A-Z0-9_]*|\{\{\s*[\w.]+\s*\}\}|%[A-Z_][A-Z0-9_]*%)$")
_STATE_WORDS = frozenset({
    "required", "optional", "invalid", "expired", "missing", "empty", "wrong", "correct",
    "incorrect", "changed", "reset", "needed", "protected", "hidden", "unknown", "default",
    "enabled", "disabled", "masked", "omitted", "removed", "revoked", "rotated", "updated",
    "provided", "supplied", "redacted", "secret", "password", "token", "notset", "unset",
    "valid", "weak", "strong", "short", "simple", "complex", "secure", "insecure", "same", "different",
    "saved", "stored", "encrypted", "hashed", "compromised", "leaked", "stolen", "forgotten", "shared",
    "attached", "below", "above", "here", "there", "case-sensitive", "working", "broken", "blank",
    "fine", "good", "great", "okay", "accepted", "rejected", "locked", "unlocked",
})
# 中文值里出现这些字/词，说的是状态（已经改好了、是什么、不对），不是口令本身。
_CJK_STATE_RE = re.compile(
    r"[了吗呢吧么啊呀嘛哦不没已改忘错对否请要需该]|什么|怎么|多少|几位|位数|正确|过期|失效|重置|修改|设置|"
    r"提示|一样|相同|记得|告诉|发给|输入|填写|知道|问题|空的")
# 识别视图：逐字 NFKC，去掉所有 Cf/Mn 与韩文填充符，拉丁/西里尔/希腊同形字折叠到拉丁。
_FILLERS = frozenset("\u3164\u115f\u1160\uffa0")
_CONFUSABLES = str.maketrans({
    "\u0430": "a", "\u0435": "e", "\u043e": "o", "\u0440": "p", "\u0441": "c", "\u0443": "y",
    "\u0445": "x", "\u0456": "i", "\u0458": "j", "\u0455": "s", "\u0501": "d", "\u04bb": "h",
    "\u0410": "A", "\u0412": "B", "\u0415": "E", "\u041a": "K", "\u041c": "M", "\u041d": "H",
    "\u041e": "O", "\u0420": "P", "\u0421": "C", "\u0422": "T", "\u0425": "X", "\u0406": "I",
    "\u0408": "J", "\u0405": "S",
    "\u03bf": "o", "\u03b1": "a", "\u03b5": "e", "\u03b9": "i", "\u03ba": "k", "\u03c1": "p",
    "\u03c4": "t", "\u03c5": "u", "\u03c7": "x", "\u03bd": "v",
    "\u0391": "A", "\u0392": "B", "\u0395": "E", "\u0396": "Z", "\u0397": "H", "\u0399": "I",
    "\u039a": "K", "\u039c": "M", "\u039d": "N", "\u039f": "O", "\u03a1": "P", "\u03a4": "T",
    "\u03a5": "Y", "\u03a7": "X",
    # f0.8+（外审 R3-11）：连字符变体折叠成 ASCII `-`（sk\u2013ant-… 不再漏）。em dash（\u2014）不在内：
    # 它是中文「——」分隔符，_SEP_RE 要认它。
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2043": "-", "\u2212": "-",
    "\ufe63": "-",
})


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


def _is_cjk(c: str) -> bool:
    return "\u3400" <= c <= "\u9fff" or "\uf900" <= c <= "\ufaff"


def _split_ident(word: str) -> list[str]:
    toks: list[str] = []
    for part in re.split(r"[_.\-\s]+", word):
        toks.extend(t.lower() for t in _CAMEL_RE.findall(part))
    return toks


def _key_tier(tokens: list[str]) -> str | None:
    if not tokens or any(t in _KEY_NEG for t in tokens):
        return None
    best = None
    cands = [_KEY_TIERS.get(t) for t in tokens]
    cands += [_KEY_PAIRS.get(p) for p in zip(tokens, tokens[1:])]
    for tier in cands:
        if tier and (best is None or _TIER_RANK[tier] > _TIER_RANK[best]):
            best = tier
    return best


def _is_placeholder(v: str) -> bool:
    if v.lower() in _PLACEHOLDER_WORDS or _PLACEHOLDER_RE.match(v):
        return True
    m = re.fullmatch(r"<\s*([A-Za-z_][A-Za-z_ \-]*)\s*>", v)
    if m:
        inner = m.group(1)
        toks = set(_split_ident(inner))
        return not any(c.islower() for c in inner) or bool(toks & _PLACEHOLDER_TOKENS) or any(
            t in _KEY_TIERS for t in toks)
    # an env-var reference that names itself: YOUR_PASSWORD / AIDUMEM_API_TOKEN / placeholder_value_here
    if "_" in v and re.fullmatch(r"[A-Za-z][A-Za-z_]*", v):
        toks = _split_ident(v)
        return bool(set(toks) & _PLACEHOLDER_TOKENS) or _key_tier(
            [t for t in toks if t not in _KEY_NEG]) is not None
    return False


def _digits_ok(v: str, tier: str, mode: str) -> bool:
    # f0.8 ③-10：secret / 助记词 键后的长数字串（≥8 位）同样算秘密；「the secret is 42」不算
    if tier == "secret" and mode != "weak":
        return len(v) >= 8
    return tier in ("pw", "pin") and len(v) >= (6 if mode == "weak" else 4)


def _value_ok(v: str, tier: str, mode: str, raw: str | None = None) -> bool:
    """mode: explicit (: = verb quotes) | natural (is / 是 / 为) | weak (, —— gap no-separator)."""
    v = v.strip()
    if not v or v.startswith("[redacted") or _is_placeholder(raw or v) or _is_placeholder(v):
        return False
    if v.startswith(("/", "~/", "./", "../", "http://", "https://")):
        return False
    if any(_is_cjk(c) for c in v):
        if tier != "pw" or mode == "weak" or len(v) < 4:
            return False
        return not _CJK_STATE_RE.search(v)
    if v.isdigit():
        return _digits_ok(v, tier, mode)
    has_alpha = any(c.isalpha() for c in v)
    has_digit = any(c.isdigit() for c in v)
    has_symbol = any(c in _SYMBOLS for c in v)
    has_mixed = any(c.isupper() for c in v[1:]) and any(c.islower() for c in v)
    if mode == "weak":
        return has_alpha and (has_digit or has_symbol) and len(v) >= 6
    if has_digit or has_symbol or has_mixed:
        return len(v) >= (4 if tier in ("pw", "pin") else 6)
    if v.lower() in _STATE_WORDS or tier == "pin":
        return False
    if tier == "pw":
        return len(v) >= (5 if mode == "natural" else 4)
    if mode != "explicit":
        return False
    return len(v) >= (4 if tier == "secret" else 16)


def _read_value(view: str, p: int, tier: str) -> tuple[int, int, str | None, bool] | None:
    """Value span after a separator: (start, end, raw-for-placeholder, quoted)."""
    if p >= len(view):
        return None
    close = _QUOTES.get(view[p])
    if close:
        end = view.find(close, p + 1, p + 202)
        nl = view.find("\n", p + 1, end if end > 0 else p + 202)
        if end > p + 1 and nl < 0:
            return p + 1, end, view[p:end + 1], True
        p += 1
    allow_cjk = tier == "pw"
    q = p
    while q < len(view) and q - p < 200:
        c = view[q]
        if c in _VALUE_STOP or (_is_cjk(c) and not allow_cjk) or (not _is_cjk(c) and not c.isprintable()):
            break
        q += 1
    # an ASCII password followed by more Chinese (「密码：Hunter2024!x别告诉别人」) ends at the Chinese
    if q > p and not _is_cjk(view[p]):
        for k in range(p, q):
            if _is_cjk(view[k]):
                q = k
                break
    seg = view[p:q]
    while seg and (seg[-1] == "." or any(seg[-1] == c and seg.count(o) < seg.count(c) for o, c in ("()", "[]", "{}"))):
        seg, q = seg[:-1], q - 1
    return (p, q, None, False) if q > p else None


def _cjk_nosep_value(view: str, p: int) -> tuple[int, int] | None:
    if p < len(view) and view[p] in " \t":
        p += 1
    q = p
    while q < len(view) and q - p < 200 and view[q] not in _VALUE_STOP and not _is_cjk(view[q]) \
            and view[q].isprintable():
        q += 1
    return (p, q) if q > p else None


# ── f0.8+（计划 ⑫ 打码误伤）：口令键后跟的是一句英文，不是口令 ───────────────────────
# 「the password is printed on the back」「my wifi password is pretty long」：纯字母值后面还接着
# 英文词（句子没完），或值本身是常见副词 / 形容词 / 代词，就不是口令。值后面是句末、标点、
# 或 and / but / lol 这类分句词时照旧打码（「password is sunshine」「password is hunter but…」）。
_PROSE_WORDS = frozenset("""
something anything nothing everything somewhere anywhere nowhere everywhere elsewhere whatever
pretty quite really very rather fairly kinda sorta still already again also actually basically
literally probably definitely usually always never often sometimes mostly totally super
easy hard long obvious complicated ridiculous terrible awful horrible impossible annoying weird
random unique personal private important sensitive confidential similar written printed listed
shown known visible inside under behind mine yours theirs their these those where which being
getting going becoming starting expiring changing stuck somewhat almost exactly honestly seriously
simply clearly apparently currently recently finally only nearly mainly completely extremely likely
unlikely easily typically
""".split())
_CLAUSE_WORDS = frozenset("""
and but so or because lol haha ok okay btw please pls dont don't now then yet though right
thanks thx just
""".split())
_NEXT_WORD_RE = re.compile(r"[ \t]+([A-Za-z][A-Za-z']*)")


def _reads_as_prose(view: str, s: int, e: int) -> bool:
    """A pure-letter value that is really the start of an English sentence."""
    v = view[s:e]
    if not v.isascii() or not v.isalpha():
        return False
    low = v.lower()
    if low in _PROSE_WORDS:
        return True
    nxt = _NEXT_WORD_RE.match(view, e)
    return bool(nxt) and nxt.group(1).lower() not in _CLAUSE_WORDS


def _credential_at(view: str, key_end: int, tier: str, *, ascii_key: bool, cjk_key: bool) -> tuple[int, int] | None:
    gap = False
    pos = key_end
    m = _SEP_RE.match(view, pos)
    if not m and ascii_key:
        g = _GAP_RE.match(view, pos)
        if g:
            gap, pos = True, g.end()
            m = _SEP_RE.match(view, pos)
    if not m:
        if cjk_key:
            span = _cjk_nosep_value(view, key_end)
            if span and _value_ok(view[span[0]:span[1]], tier, "weak"):
                return span
        return None
    if m.group("weak"):
        mode = "weak"
    elif m.group("nat") and not m.group("natx"):
        mode = "natural"
    else:
        mode = "explicit"
    if gap:
        mode = "weak"
    got = _read_value(view, m.end(), tier)
    if not got:
        return None
    s, e, raw, quoted = got
    if quoted and mode == "natural":
        mode = "explicit"
    if not quoted and mode != "weak" and _reads_as_prose(view, s, e):
        return None
    return (s, e) if _value_ok(view[s:e], tier, mode, raw) else None


def _credential_spans(view: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    prev_end, prev_last = -2, ""
    for wm in _ASCII_WORD_RE.finditer(view):
        word = wm.group(0).rstrip("._-")
        if not word:
            continue
        start, end = wm.start(), wm.start() + len(word)
        adj = prev_last if prev_end == start - 1 and view[start - 1] == " " else ""
        toks = _split_ident(word)
        prev_end, prev_last = end, (toks[-1] if toks else "")
        if not toks or not _KEY_STEM_RE.search(word):
            continue
        tier = _key_tier(toks) or (_key_tier([adj, *toks]) if adj else None)
        if tier:
            span = _credential_at(view, end, tier, ascii_key=True, cjk_key=False)
            if span:
                spans.append(span)
    for km in _CJK_KEY_RE.finditer(view):
        if _CJK_KEY_NEG_RE.match(view, km.end()):
            continue
        span = _credential_at(view, km.end(), _CJK_KEY_TIER[km.group(0)], ascii_key=False, cjk_key=True)
        if span:
            spans.append(span)
    return spans


_VIEW_CACHE: dict[str, str] = {}


def _view_char(ch: str) -> str:
    rep = _VIEW_CACHE.get(ch)
    if rep is None:
        rep = "".join(c.translate(_CONFUSABLES) for c in unicodedata.normalize("NFKC", ch)
                      if c not in _FILLERS and (c.isascii() or unicodedata.category(c) not in ("Cf", "Mn")))
        if len(_VIEW_CACHE) < 65536:
            _VIEW_CACHE[ch] = rep
    return rep


def _detection_view(text: str) -> tuple[str, list[int] | None]:
    """NFKC per character, drop Cf/Mn/fillers, fold confusables; idx maps view → original
    (None when the view is the text itself)."""
    if text.isascii():
        return text, None
    reps = [ch if ch.isascii() else _view_char(ch) for ch in text]
    view = "".join(reps)
    if view == text:
        return text, None
    idx = [i for i, rep in enumerate(reps) for _ in rep]
    return view, idx


def redact_forbidden(text: str) -> tuple[str, list[str]]:
    """Replace secret / ID / signature spans with ``[redacted:<kind>]``.

    Returns (new_text, kinds).  The rest of the sentence is kept: P7 is about
    never *storing* the secret, not about dropping the user's whole turn.

    f0.7.6（外审 R2-05/R2-06）：识别在「逐字 NFKC + 去掉所有 Cf/Mn 与韩文填充符 +
    同形字折叠」的视图上做，命中区间再映射回原文替换——原文其余字符原样保留。
    视图与原文不同且有命中时，边界记号中和在输出上重做一遍。
    """
    if not isinstance(text, str) or not text or not secret_guard_on():
        return text, []
    view, idx = _detection_view(text)
    found = _redact_spans(view)
    if not found:
        return text, []
    # view → original coordinates, then coalesce
    if idx is not None:
        found = [(idx[s], idx[e - 1] + 1, k) for s, e, k in found if e > s]
    mapped = sorted(found, key=lambda t: (t[0], -t[1]))
    merged: list[list] = []
    for s, e, k in mapped:
        if merged and s < merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e, k])
    parts: list[str] = []
    last = 0
    for s, e, k in merged:
        parts.append(text[last:s])
        parts.append(REDACT_FMT.format(k))
        last = e
    parts.append(text[last:])
    out = "".join(parts)
    kinds = [k for _, _, k in merged]
    if view != text:
        try:
            from ducky.security.injection_guard import neutralize_boundary_markers
            out = neutralize_boundary_markers(out)
        except ImportError:  # pragma: no cover
            pass
    return out, kinds


def _redact_spans(view: str) -> list[tuple[int, int, str]]:
    """All redaction spans on the detection view, in priority order (earlier wins on overlap)."""
    raw: list[tuple[int, int, str]] = []

    def _add(kind: str, m: re.Match) -> None:
        g = "v" if "v" in m.re.groupindex and m.group("v") is not None else 0
        raw.append((m.start(g), m.end(g), kind))

    for m in _SIGNATURE_RE.finditer(view):
        _add("signature", m)
    for kind, pat in _SECRET_PATTERNS:
        for m in pat.finditer(view):
            if kind == "seed_phrase" and sum(
                    w.lower() in _SEED_STOPWORDS for w in re.findall(r"[A-Za-z]+", m.group("v"))) >= 2:
                continue
            _add(kind, m)
    raw.extend((s, e, "seed_phrase") for s, e in _bip39_spans(view))
    raw.extend((s, e, "encoded_secret") for s, e in _encoded_secret_spans(view))
    raw.extend((s, e, "credential") for s, e in _credential_spans(view))
    for m in _CN_ID_RE.finditer(view):
        if _cn_id_valid(m):
            _add("id_number", m)
    for m in _DOC_CONTEXT_RE.finditer(view):
        if any(c.isdigit() for c in m.group("v")):
            _add("id_document", m)
    for m in _CARD_CONTEXT_RE.finditer(view):
        if _luhn(m.group("v")):
            _add("card_number", m)
    accepted: list[list] = []
    for s, e, k in raw:
        hit = next((a for a in accepted if s < a[1] and e > a[0]), None)
        if hit:
            hit[0], hit[1] = min(hit[0], s), max(hit[1], e)
        else:
            accepted.append([s, e, k])
    return [(s, e, k) for s, e, k in accepted]


# ── f0.8+（外审 R3-10 / 计划 ⑫）：没有关键词的助记词——BIP39 英文词表 + 校验和 ─────────
# 连续 12/15/18/21/24 个 BIP39 词、且 BIP39 校验和成立，才算助记词：随手列的 12 个常用词
# （购物清单）只有 1/16 的机会碰上校验和，24 词是 1/256。没有关键词时词间只允许空白、换行与编号——
# 逗号分隔的是清单，不是钱包导出（带「助记词 / mnemonic」关键词时逗号照认，见 _SECRET_PATTERNS）。
_BIP39_TOKEN_RE = re.compile(r"[A-Za-z]+")
_BIP39_GAP_RE = re.compile(r"(?:[ \t\r\n]|\(?\d{1,2}[.):\u3001]?)+")
_BIP39_LENGTHS = (24, 21, 18, 15, 12)


def _bip39_checksum_ok(words: list[str]) -> bool:
    index = _bip39_index()
    n = len(words)
    if n not in _BIP39_LENGTHS:
        return False
    bits = "".join(format(index[w], "011b") for w in words)
    cs = n // 3
    ent, chk = bits[:-cs], bits[-cs:]
    digest = hashlib.sha256(int(ent, 2).to_bytes(len(ent) // 8, "big")).digest()
    return format(digest[0], "08b")[:cs] == chk


_BIP39_INDEX: dict[str, int] = {}


def _bip39_index() -> dict[str, int]:
    if not _BIP39_INDEX:
        from ducky.bip39_english import WORDS
        _BIP39_INDEX.update({w: i for i, w in enumerate(WORDS)})
    return _BIP39_INDEX


def _bip39_spans(view: str) -> list[tuple[int, int]]:
    """Runs of ≥12 BIP39 words whose some 12–24-word window carries a valid BIP39 checksum."""
    if len(view) < 12 * 4:
        return []
    index = _bip39_index()
    spans: list[tuple[int, int]] = []
    run: list[re.Match] = []

    def _flush() -> None:
        if len(run) < 12:
            return
        words = [m.group(0).lower() for m in run]
        for n in _BIP39_LENGTHS:
            for i in range(0, len(words) - n + 1):
                if _bip39_checksum_ok(words[i:i + n]):
                    spans.append((run[i].start(), run[i + n - 1].end()))
                    return

    for m in _BIP39_TOKEN_RE.finditer(view):
        w = m.group(0)
        ok = w.lower() in index and (w.islower() or w.istitle() or w.isupper())
        if ok and run and m.start() - run[-1].end() <= 8 \
                and _BIP39_GAP_RE.fullmatch(view, run[-1].end(), m.start()):
            run.append(m)
            continue
        _flush()
        run = [m] if ok else []
    _flush()
    return spans


# ── f0.8+（外审 R3-11，尽力而为）：编码、倒序或字符串拼接后的密钥 ───────────────────────
# 长 base64 / hex / URL 编码串解一次码、长串倒过来、`"sk-ant-" + "…"` 拼起来，再用前缀明确的
# 密钥形态（_SECRET_PATTERNS 里的 api_key 一类）认一次；认出来就把整串打成 encoded_secret。
# 空格或换行把一把钥匙拆成几段、逐字加空格这类写法仍认不出——正则打码的固有局限，文档里写明。
_ENCODED_KINDS = frozenset({"api_key", "private_key", "bot_token", "jwt", "webhook"})
_LONG_RUN_RE = re.compile(r"(?<![A-Za-z0-9+/_\-])[A-Za-z0-9+/_\-]{28,}={0,2}(?![A-Za-z0-9+/=_\-])")
_PCT_RUN_RE = re.compile(r"[A-Za-z0-9_.~\-]*(?:%[0-9A-Fa-f]{2}[A-Za-z0-9_.~\-]*)+")
_CONCAT_RE = re.compile(r"""(["'])[^"'\n]{1,120}\1(?:[ \t]*\+[ \t]*(["'])[^"'\n]{1,120}\2){1,8}""")
_QUOTED_RE = re.compile(r"""(["'])([^"'\n]*)\1""")


def _plain_secret(text: str) -> bool:
    if not text or len(text) < 16 or not text.isprintable():
        return False
    return any(kind in _ENCODED_KINDS and pat.search(text) for kind, pat in _SECRET_PATTERNS)


def _decodings(run: str) -> list[str]:
    import base64
    import binascii
    out = [run[::-1]]
    core = run.rstrip("=")
    if len(core) % 2 == 0 and re.fullmatch(r"[0-9A-Fa-f]+", core):
        try:
            out.append(bytes.fromhex(core).decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            pass
    pad = "=" * (-len(core) % 4)
    for dec in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            out.append(dec(core + pad).decode("ascii"))
        except (binascii.Error, ValueError, UnicodeDecodeError):
            continue
    return out


def _encoded_secret_spans(view: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for m in _LONG_RUN_RE.finditer(view):
        if any(_plain_secret(d) for d in _decodings(m.group(0))):
            spans.append((m.start(), m.end()))
    if "%" in view:
        from urllib.parse import unquote
        for m in _PCT_RUN_RE.finditer(view):
            if len(m.group(0)) >= 20 and _plain_secret(unquote(m.group(0))):
                spans.append((m.start(), m.end()))
    if "+" in view:
        for m in _CONCAT_RE.finditer(view):
            if _plain_secret("".join(q[1] for q in _QUOTED_RE.findall(m.group(0)))):
                spans.append((m.start(), m.end()))
    return spans


_MEANINGFUL_RE = re.compile(r"\[redacted:[a-z_]+\]|[\s\W_]+", re.UNICODE)


def hard_block_reason(text: str, *, summary: bool = False) -> str | None:
    """Whole-write rejections (P7): a raw e-mail / document dump, an oversized
    paste, or a turn that is nothing but a secret.  None = store it."""
    if not isinstance(text, str) or not text or not secret_guard_on():
        return None
    if len(_EMAIL_HEADER_RE.findall(text)) >= 3:
        return "raw_email_copy"
    # f0.8+（R3-05）：摘要身份不再免掉长度上限，只换一个更紧的上限
    if len(text) > (min(max_raw_chars(), SUMMARY_MAX_CHARS) if summary else max_raw_chars()):
        return "oversized_raw_copy"
    if "[redacted:" in text and len(_MEANINGFUL_RE.sub("", text)) <= 4:
        return "secret_only"
    return None


def drop_secret_only_turns(messages):
    """f0.8 ③-9：多消息载荷逐条过 P7 闸门。

    hard_block_reason 只看拼接文本：一条「只有密钥」的消息夹在正常对话里，拼起来就不再是
    secret_only，于是它以 `[redacted:…]` 空壳落库。这里把这类消息逐条摘掉（原值早已打码），
    只剩它们时原样返回，交给整体闸门按 secret_only 拒收。返回 (messages, 摘掉条数)。"""
    if not isinstance(messages, list) or len(messages) < 2:
        return messages, 0
    keep = [m for m in messages
            if not (isinstance(m, dict) and hard_block_reason(_content_text(m.get("content")), summary=True)
                    == "secret_only")]
    if not keep or len(keep) == len(messages):
        return messages, 0
    return keep, len(messages) - len(keep)


_MESSAGE_KEYS = ("role", "content")
# f0.7.6（外审 R2-08）：role / type 是枚举键，不打码——所以必须白名单，否则任意字符串原样落 4 张表。
_ROLES = frozenset({"user", "assistant", "system", "tool", "function", "developer"})
_PART_TYPES = frozenset({"text", "input_text", "output_text", "image_url", "input_image",
                         "image", "audio", "input_audio", "file", "refusal",
                         "function", "tool_use", "tool_result"})


def _enum_value(key: str, val):
    v = val.strip().lower() if isinstance(val, str) else ""
    if key == "role":
        return v if v in _ROLES else "user"
    return v if v in _PART_TYPES else "text"


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
        if "role" in out:
            out["role"] = _enum_value("role", out["role"])
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
            if key in ("role", "type"):
                out[key] = _enum_value(key, val)
                continue
            if not isinstance(val, (str, list, dict)):
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


def _redact_meta_entry(key, val, depth: int):
    """f0.8 ③-10：键名本身就是凭据语境（{"secret": "…"} / {"mnemonic": "…"} / {"session_cookie": "…"}）。
    按「键: 值」整体判一次——值像秘密才打码（{"cookie": "chocolate chip"} 不动），否则照旧只看值。"""
    # f0.8+（外审 R3-09）：凭据键下的数字值（{"pin": 4821} / {"password": 12345678}）按字符串同样判一次；
    # 普通键下的数字（{"count": 4821}）不动。
    if isinstance(val, (int, float)) and not isinstance(val, bool) and isinstance(key, str) \
            and _KEY_STEM_RE.search(key):
        # a card security code is 3 digits; in metadata the key alone says what it is
        if isinstance(val, int) and 100 <= abs(val) <= 9999 and _key_tier(_split_ident(key)) == "pin" \
                and {"cvv", "cvc"} & set(_split_ident(key)):
            return REDACT_FMT.format("credential")
        prefix = f"{key}: "
        out, kinds = redact_forbidden(prefix + str(val))
        if kinds and out.startswith(prefix):
            return out[len(prefix):]
        return val
    if isinstance(key, str) and isinstance(val, str) and val and _KEY_STEM_RE.search(key):
        prefix = f"{key}: "
        out, kinds = redact_forbidden(prefix + val)
        if kinds and out.startswith(prefix):
            return out[len(prefix):]
    return redact_metadata(val, depth + 1)


def redact_metadata(value, depth: int = 0):
    """f0.7.5（结案留账 #3）：metadata 里的字符串值同样过原则 7。键名不动。"""
    if depth > 8:
        return value
    if isinstance(value, str):
        return redact_forbidden(value)[0]
    if isinstance(value, dict):
        return {k: _redact_meta_entry(k, v, depth) for k, v in value.items()}
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


# f0.8 ②：只有这两类噪声开拦截（影子周 68/68 判对）；其余噪声码仍只是影子判定。
ENFORCED_NOISE = frozenset({"host_machine_text", "host_compaction_copy"})
# 短确认 / 单字：不拒，归当日流水（classify 里处理）。
SHORT_REPLY_NOISE = frozenset({"ack", "too_short"})


# ── f0.8 ②：模型思考过程（会话精华路径） ─────────────────────────────────
# 推理模型把思考写进正文时的三种形态：<think>…</think>、harmony 通道串
# （analysis…assistantfinal / <|channel|>final<|message|>）、以及干脆只有英文思考。
_THINK_BLOCK_RE = re.compile(r"(?is)<(think|thinking|reasoning)>.*?(?:</\1>|\Z)")
_HARMONY_FINAL_RE = re.compile(
    r"(?is)(?:<\|channel\|>\s*final\s*<\|message\|>|<\|start\|>assistant<\|channel\|>final<\|message\|>|"
    r"\bassistantfinal\b|^\s*analysis.*?\bfinal\s*:)")
_HARMONY_TOKEN_RE = re.compile(r"<\|[a-z_]+\|>")
_HARMONY_ANALYSIS_RE = re.compile(
    r"(?is)(?:<\|start\|>assistant)?(?:<\|channel\|>)?\s*\banalysis\s*<\|message\|>.*?(?:<\|end\|>|\Z)")
_REASONING_START_RE = re.compile(
    r"(?i)^\s*[\"'*`]*(?:we need to|we have to|we should|we must|we are asked|we're asked|let me|let's|let us|"
    r"i need to|i should|i must|i will now|i'll (?:now|start|write|output)|okay[,.]? (?:so|let|we|i|the)|"
    r"ok[,.]? (?:so|let|we|i)|alright[,.]|hmm\b|first[,]? (?:i|we|let)|so we need|now (?:we|i) (?:need|should|must)|"
    r"the user (?:wants|asks|asked|is asking|requests|requested|wants us)|the task (?:is|asks)|"
    r"the instruction|analysis\b)")
_REASONING_MARKERS = (
    "we need to", "need to output", "should output", "must output", "let me", "let's", "the user wants",
    "the user asks", "the user is", "the instruction", "the prompt", "in chinese", "one or two sentences",
    "1-2 sentences", "two sentences", "under 80", "80 characters", "80 chars", "no more than",
    "the summary should", "we should", "i should", "the answer", "maybe", "wait,", "so the summary",
)


def strip_reasoning(text: str) -> str:
    """Remove reasoning wrappers a model may put around its answer; returns what is left."""
    t = text or ""
    t = _THINK_BLOCK_RE.sub("", t)
    t = _HARMONY_ANALYSIS_RE.sub(" ", t)
    m = None
    for m in _HARMONY_FINAL_RE.finditer(t):
        pass
    if m is not None and m.end() < len(t):
        t = t[m.end():]
    t = _HARMONY_TOKEN_RE.sub("", t)
    return t.strip()


_FINAL_MARKER_RE = re.compile(
    r"(?im)(?:^|\n)\s*(?:\*\*)?(?:final answer|final|answer|summary|最终答案|最终输出|答案|输出)(?:\*\*)?\s*[:：]\s*")
_JSON_OBJ_RE = re.compile(r"\{[^{}]*\}", re.S)
_ANSWER_KEYS = ("summary", "answer", "final", "output", "精华", "答案")


def _answer_ok(text: str) -> bool:
    t = (text or "").strip()
    return len(t) >= 4 and looks_like_reasoning(t) is None


def extract_final_answer(text: str) -> tuple[str, str | None]:
    """Model output → (answer, reason).  reason is None when an answer was found.

    f0.8+ C: a reasoning model (GPT-OSS-20B on an OpenAI-compatible gateway)
    may put its thinking in front of the answer.  Order: strip known wrappers
    (<think>, harmony channels); then a JSON object carrying an answer key;
    then text after an explicit "Final answer:" marker; then the last paragraph
    when it is a clean answer.  The reasoning itself is never returned."""
    cand = strip_reasoning(text)
    if not cand:
        return "", "reasoning_only"
    why = looks_like_reasoning(cand)
    if not why:
        return cand, None
    for m in reversed(list(_JSON_OBJ_RE.finditer(cand))):
        try:
            obj = json.loads(m.group(0))
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            for key in _ANSWER_KEYS:
                val = obj.get(key)
                if isinstance(val, str) and _answer_ok(val):
                    return val.strip(), None
    markers = list(_FINAL_MARKER_RE.finditer(cand))
    if markers:
        tail = cand[markers[-1].end():].strip().strip("\"'“”「」")
        if _answer_ok(tail):
            return tail, None
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n", cand) if p.strip()]
    if len(paras) >= 2:
        last = paras[-1].strip("\"'“”「」")
        letters = [c for c in last if c.isalpha()]
        if letters and sum(1 for c in letters if _is_cjk(c)) / len(letters) >= 0.5 and _answer_ok(last):
            return last, None
    return "", why


def looks_like_reasoning(text: str) -> str | None:
    """Reason code when ``text`` is a model's chain-of-thought rather than an answer, else None.

    Only applied to model-produced text (session summaries), never to what a user said."""
    raw = (text or "").strip()
    if not raw:
        return None
    if _THINK_BLOCK_RE.search(raw) or _HARMONY_TOKEN_RE.search(raw) or re.search(r"(?i)\bassistantfinal\b", raw):
        return "reasoning_wrapper"
    letters = [c for c in raw if c.isalpha()]
    cjk = sum(1 for c in letters if _is_cjk(c))
    mostly_latin = not letters or cjk / len(letters) < 0.3
    if _REASONING_START_RE.match(raw) and mostly_latin:
        return "reasoning_preamble"
    low = raw.lower()
    hits = sum(1 for k in _REASONING_MARKERS if k in low)
    if mostly_latin and hits >= 3:
        return "reasoning_markers"
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
_BASIS_SAFE_RE = re.compile(r"[^\w:.\- ]")


def neutralize_client_provenance(md: dict) -> dict:
    """f0.8（外审 muse M-14 / deepseek F-10）：provenance 是服务端裁定的保留字段。

    调用方只能把自己**降格**为 inferred（「这条是我推断的」），不能自封 direct（「用户原话」）。
    其余写法（direct、user_said、乱码）一律丢掉，交给 source_of() 按服务端信号推导。原地修改并返回。"""
    if not isinstance(md, dict):
        return md
    md.pop("role_asserted_by", None)       # f0.8+（R3-08）：服务端按凭据填，调用方填的不认
    st = str(md.pop("source_type", "") or "").strip().lower()
    basis = md.pop("basis", None)
    if st == "inferred":
        tail = _BASIS_SAFE_RE.sub("", str(basis or ""))[:80].strip()
        md["source_type"] = "inferred"
        md["basis"] = f"caller_declared:{tail}" if tail else "caller_declared"
    return md


def caller_assertion() -> str:
    """f0.8+（外审 R3-08）：这次写入里「用户说的」由谁断言——服务端按本请求的凭据裁定。

    ui_session（控制台登录）/ token（主钥匙）/ agent_key:<agent>（每 agent 一把的钥匙）/
    local（门禁关闭的本机直连）。写进 metadata 的 ``role_asserted_by``；调用方自填的值入口处已丢弃。"""
    try:
        from ducky.security.auth import current_request_auth_kind
        kind = current_request_auth_kind()
    except ImportError:          # pragma: no cover
        kind = ""
    if kind == "session":
        return "ui_session"
    if kind != "bearer":
        return "local"
    try:
        from ducky.agent_keys import current
        cur = current()
    except ImportError:          # pragma: no cover
        cur = None
    if cur and not cur.get("main") and cur.get("agent"):
        from ducky.origin_context import safe_origin_id
        return f"agent_key:{safe_origin_id(cur['agent'])[:60]}"
    return "token"


def source_of(md: dict | None, *, infer_used: bool = False) -> tuple[str, str]:
    """(source_type, basis).  An explicit *inferred* marker wins (callers may
    only downgrade themselves); a stored ``direct`` is re-derived rather than
    trusted (f0.8): any derived record (session summary, LLM extraction,
    reflection, bot relay) is inferred and everything else is what was said."""
    md = md or {}
    st = str(md.get("source_type") or "").strip().lower()
    if st == "inferred":
        return st, str(md.get("basis") or ("caller_declared"))[:200]
    from ducky.origin_context import is_session_summary
    if is_session_summary(md) or md.get("kind") == "session_distill" or md.get("lane") == "distill":
        n = md.get("distill_source_count")
        return "inferred", f"session_distill:{n} turns" if n else "session_distill"
    if infer_used:
        return "inferred", "llm_extraction"
    if md.get("_origin_is_bot"):
        return "inferred", "relayed_by_bot"
    # f0.8+（外审 R3-08）：每 agent 一把的钥匙代填的「用户原话」只算 inferred——那是另一个 agent
    # 的转述，服务端核实不了；主钥匙 / 控制台 / 本机写入照旧按消息角色判。
    asserted = str(md.get("role_asserted_by") or "")
    if asserted.startswith("agent_key:"):
        return "inferred", f"asserted_by:{asserted[:80]}"
    src = str(md.get("source") or "")
    if src:
        from ducky.epistemic import EPISTEMIC_REASONED, resolve_epistemic
        if resolve_epistemic(src) == EPISTEMIC_REASONED:
            return "inferred", f"source:{src[:60]}"
    return "direct", "verbatim_turn"


def legacy_inferred(md: dict | None) -> bool:
    return source_of(md or {})[0] == "inferred"


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
    elif noise in SHORT_REPLY_NOISE:
        # f0.8 ②：影子周里「好，那走吧」「改」是在回答上一句，不该当噪声扔——归当日流水。
        layer, verdict, worth = "daily", "daily", 0.1
        reasons.append(f"short_reply:{noise}")
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
        # f0.8 ③-9：多消息 /add 逐条打出处——助手/工具的发言不是用户原话，不许落成 direct。
        if source_type == "direct":
            source_type, basis = "inferred", f"turn_role:{str(role)[:20]}"
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
    # f0.8：provenance 服务端裁定——只有 inferred（降格）能保留调用方的值
    if out.get("source_type") != "inferred" or c["source_type"] == "inferred":
        out["source_type"], out["basis"] = c["source_type"], c["basis"]
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


# ── f0.8 ②：「同一件事」先看话题重合 ─────────────────────────────────────
# 影子周 8/8 误配：两条只共享「是/不是」「既然」「放行/未放行」这类判断词就被当成同一件事改口。
# 现在先剥掉判断词与虚词，再比实词：至少 2 个共享词元且重合度够，或向量余弦够高，才算同一件事。
_TOPIC_FILLER_RE = re.compile(
    "是不是|不是|未放行|放行|既然|就是|还是|可是|但是|而是|不过|所以|因为|如果|然后|而且|是|"
    "没有|没|不|别|也|就|都|还|又|再|了|的|吗|呢|吧|啊|呀|哦|嘛|啦|"
    "我们|你们|他们|咱们|大家|我|你|您|他|她|它|这个|那个|这些|那些|这样|那样|这里|那里|这|那|"
    "可能|应该|现在|已经|用户|一下|一个|什么|怎么|为什么|可以")
_LATIN_STOP = frozenset(
    "the a an is are was were be been not no yes and or but so to of in on at for with it this that these "
    "those we you i he she they my your our its as by do does did can will just very if then than".split())


def topic_grams(text: str) -> frozenset:
    """Content-word grams: CJK bigrams and Latin words, judgement/filler words removed."""
    t = _TOPIC_FILLER_RE.sub(" ", (text or "").lower())
    grams: set[str] = set()
    for run in re.findall(r"[\u3400-\u9fff\uf900-\ufaff]+", t):
        if len(run) == 1:
            continue
        grams.update(run[i:i + 2] for i in range(len(run) - 1))
    for w in re.findall(r"[a-z][a-z0-9_.\-]*[a-z0-9]|[a-z]{2,}", t):
        if w not in _LATIN_STOP:
            grams.add(w)
    return frozenset(grams)


def topic_overlap_min() -> float:
    from ducky.env_config import float_env
    return float_env("AIDUMEI_WP_TOPIC_MIN_OVERLAP", 0.3, minimum=0.0, maximum=1.0)


def topic_min_cosine() -> float:
    from ducky.env_config import float_env
    return float_env("AIDUMEI_WP_TOPIC_MIN_COSINE", 0.8, minimum=0.0, maximum=1.0)


def topical_overlap(new_text: str, old_text: str, *, vec_sim: float | None = None,
                    common: frozenset | set = frozenset()) -> bool:
    """Are two memories about the same thing at all?  ``common`` = grams that appear in most
    neighbouring memories (a name the user keeps addressing) and so say nothing about the topic."""
    if vec_sim is not None and vec_sim >= topic_min_cosine():
        return True
    ga, gb = topic_grams(new_text) - common, topic_grams(old_text) - common
    if not ga or not gb:
        return False
    shared = ga & gb
    return len(shared) >= 2 and len(shared) / min(len(ga), len(gb)) >= topic_overlap_min()


def same_topic_change(new_text: str, old_text: str, *, vec_sim: float | None = None,
                      common: frozenset | set = frozenset()) -> dict | None:
    """Does ``new_text`` restate a topic of ``old_text`` with another value?"""
    nk, ok = topic_keys(new_text), topic_keys(old_text)
    for k, v in nk.items():
        if k in ok and ok[k] != v and v not in ok[k] and ok[k] not in v:
            # 住处/工作地点：锚点（住在/搬到/在…上班）本身就是话题，值换了词面自然对不上
            if k in _PLACE_ANCHORS or topical_overlap(new_text, old_text, vec_sim=vec_sim, common=common):
                return {"kind": "value_change", "key": k}
    if _polarity_flip(new_text, old_text):
        return {"kind": "negation", "key": _NEG_RE.sub("", _core_clause(new_text), count=1)[:24]}
    if not topical_overlap(new_text, old_text, vec_sim=vec_sim, common=common):
        return None
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
    new_src = source_of(new_md)[0]
    rows = list(rows)
    # A "key" found in most neighbouring memories is a subject (a name the
    # user keeps addressing), not a property whose value can change.
    common = {k for k in topic_keys(new_text)
              if sum(1 for r in rows if k in (r.get("content") or "").lower()) > max(3, len(rows) // 3)}
    # f0.8 ②：同理，几乎每条邻居都有的词元（常被称呼的名字）不算话题重合
    row_grams = [topic_grams(r.get("content") or "") for r in rows]
    common_grams = frozenset(g for g in topic_grams(new_text)
                             if sum(1 for rg in row_grams if g in rg) > max(3, len(rows) // 3))
    for r in rows:
        old = r.get("content") or ""
        if not old or old == new_text or is_noise(old):
            continue
        hit = same_topic_change(new_text, old, vec_sim=r.get("_vec_sim"), common=common_grams)
        if not hit or (hit["kind"] == "value_change" and hit["key"] in common):
            continue
        omd = r.get("metadata") or {}
        old_src = source_of(omd)[0]
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
    conn.execute("PRAGMA secure_delete=ON")   # f0.7.6（外审 R2-15）
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
                supersede: list | None = None, mode: str = "shadow") -> None:
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
             hard_block, blacklist_rule, json.dumps(supersede or [], ensure_ascii=False),
             mode if mode in ("shadow", "enforce") else "shadow"))
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
def _fold(s: str) -> str:
    """f0.8+（外审 R3-03）：与打码识别视图同口径——NFKC（全角→半角）+ 同形字折叠（西里尔 / 希腊 → 拉丁，
    含连字符变体）+ casefold + 去掉 Cf/Mn（零宽、组合附加符）与韩文填充符。只用于忘记规则匹配，不改存储内容。"""
    # NFKD first so precomposed accents split off and drop with the other Mn marks (é → e, Tomás → tomas)
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(ch for ch in s if ch not in _FILLERS and unicodedata.category(ch) not in ("Cf", "Mn"))
    return unicodedata.normalize("NFKC", s).translate(_CONFUSABLES).casefold()


def _norm_phrase(s: str) -> str:
    return _PUNCT_RE.sub("", _fold(s))


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


_P8_WORD_RE = re.compile(r"[0-9a-z]+")


def _part_in(part: str, spaced: str, tight: str) -> bool:
    """One word of a forget rule against the folded text.  Latin/digit words match on word
    boundaries (「Tom」 hits 「Tom called」「和Tom吵架」, not tomorrow / customs / atom);
    anything with CJK keeps substring matching (Chinese has no spaces)."""
    if _P8_WORD_RE.fullmatch(part):
        return re.search(rf"(?<![0-9a-z]){re.escape(part)}(?![0-9a-z])", spaced) is not None
    return part in tight


def rule_matches(phrase: str, text: str) -> bool:
    """Pure: does one forget-rule phrase match the text (all of its words present)?

    f0.8+（外审 R3-03 / R3-04）：两边先过 _fold（全角 / 西里尔 / 零宽都归一）；拉丁词按词边界匹配。
    多词规则仍按全文匹配（规则跨消息拆开也要命中，test_auto_memory_p8_rule_split_across_messages）；
    全是拉丁词的多词规则，连写（wangxiaohua）也算命中。"""
    if not text or not phrase:
        return False
    parts = [_norm_phrase(w) for w in re.split(r"[\s,，;；]+", phrase) if _norm_phrase(w)]
    if not parts:
        return False
    f = _fold(text)
    spaced, tight = _PUNCT_RE.sub(" ", f), _PUNCT_RE.sub("", f)
    hit = [_part_in(p, spaced, tight) for p in parts]
    # neighbouring Latin words of the rule may be written as one word (wang xiaohua → wangxiaohua)
    for i in range(len(parts)):
        for j in range(i + 1, len(parts)):
            if all(hit[i:j + 1]):
                continue
            joined = "".join(parts[i:j + 1])
            if _P8_WORD_RE.fullmatch(joined) and _part_in(joined, spaced, tight):
                hit[i:j + 1] = [True] * (j + 1 - i)
    return all(hit)


def blacklist_hit(text: str, user_id: str, bank_id: str) -> int | None:
    """Id of the first forget rule the text matches (see rule_matches), else None."""
    if not text:
        return None
    for r in list_rules(user_id, bank_id):
        if rule_matches(r["phrase"], text):
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


# ── f0.8 ② enforced gate (called by /add before anything is stored) ───────
def gate_decision(text: str, role: str = "user", md: dict | None = None, *,
                  user_id: str = "default", bank_id: str = "default") -> dict | None:
    """What an ENFORCED principle does with one stored text, or None (= store as before).

    P8 forget rule, session-summary thinking text and P1 machine noise → tombstone (restorable
    for ``gate_ttl_days`` through /tombstone/restore, then the text is erased).  f0.8+（外审 R3-04）：
    P8 不再直接丢弃——误拦时用户能在墓碑里看到并恢复。Shadow-mode principles return None here;
    their verdict is still logged by observe_written."""
    if policy_mode() == "off" or not isinstance(text, str) or not text.strip():
        return None
    md = md or {}
    if principle_mode("P8") == "enforce" and os.path.exists(db_path()):
        rule = blacklist_hit(text, user_id, bank_id)
        if rule is not None:
            return {"action": "tombstone", "principle": "P8", "reason": "forget_rule", "rule": rule}
    from ducky.origin_context import is_session_summary
    # a fallback summary quotes the user's own turns: only model-written summaries are checked
    if (is_session_summary(md) and md.get("distill_mode") != "fallback"
            and principle_mode("THINKING") == "enforce"):
        why = looks_like_reasoning(text)
        if why:
            return {"action": "tombstone", "principle": "THINKING", "reason": why}
    if principle_mode("P1") == "enforce":
        noise = is_noise(text)
        if noise in ENFORCED_NOISE:
            return {"action": "tombstone", "principle": "P1", "reason": noise}
    return None


def _apply_gate(text: str, role: str, md: dict, decision: dict, user_id: str, bank_id: str) -> dict:
    """Carry out one gate decision; returns the receipt.  Raises on storage failure."""
    receipt = {k: decision[k] for k in ("action", "principle", "reason")}
    verdict = {"verdict": "reject", "worth": 0.0, "layer": "daily", "role": role or "user",
               "reasons": [f"enforced:{decision['principle']}:{decision['reason']}"]}
    rule = decision.get("rule")
    if decision["action"] == "drop":     # no principle drops today; kept for a future hard rule
        log_verdict(user_id=user_id, bank_id=bank_id, memory_id=None, text="", verdict=verdict,
                    md=md, blacklist_rule=rule, mode="enforce")
        return receipt
    from ducky.tombstone import tombstone_gated_write
    tid, target = tombstone_gated_write(
        text, user_id=user_id, bank_id=bank_id, role=role, metadata=md, ttl_days=gate_ttl_days(),
        reason=f"write_policy:{decision['principle']}:{decision['reason']}"
               + (f":rule{int(rule)}" if rule is not None else ""))
    receipt.update(tombstone_id=tid, memory_id=target, restorable_days=gate_ttl_days())
    if rule is not None:
        receipt["rule"] = rule
    # a P8 verdict row keeps the rule id but not the text hash (the tombstone holds the text until it expires)
    log_verdict(user_id=user_id, bank_id=bank_id, memory_id=target, text="" if rule is not None else text,
                verdict=verdict, md=md, blacklist_rule=rule, mode="enforce")
    return receipt


def enforce_messages(messages, md: dict | None, *, user_id: str, bank_id: str) -> tuple[Any, list[dict]]:
    """f0.8 ②: split an /add payload into what is stored and what an enforced principle took.

    Returns (messages_to_store, receipts).  ``messages_to_store`` keeps the input shape;
    it is "" / [] / None when nothing is left.  Fail-open per item: if a decision or the
    tombstone write fails, that item is stored exactly as before."""
    md = md or {}

    def _one(role: str, text: str) -> dict | None:
        try:
            d = gate_decision(text, role, md, user_id=user_id, bank_id=bank_id)
            return _apply_gate(text, role, md, d, user_id, bank_id) if d else None
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError, AttributeError,
                ImportError, RuntimeError) as exc:     # fail-open: store it as before
            logger.warning("write_policy gate failed (stored as before): %s", type(exc).__name__)
            return None

    receipts: list[dict] = []
    if isinstance(messages, str):
        r = _one("user", messages) if messages.strip() else None
        return ("", [r]) if r else (messages, [])
    if isinstance(messages, dict):
        kept, receipts = enforce_messages([messages], md, user_id=user_id, bank_id=bank_id)
        return (kept[0] if kept else None), receipts
    if not isinstance(messages, list):
        return messages, []
    kept: list = []
    for m in messages:
        if isinstance(m, dict) and isinstance(m.get("content"), str) and m.get("role") != "system":
            r = _one(str(m.get("role") or "user"), m["content"])
        elif isinstance(m, str):
            r = _one("user", m)
        else:
            r = None
        if r:
            receipts.append(r)
        else:
            kept.append(m)
    if receipts and not any(not (isinstance(m, dict) and m.get("role") == "system") for m in kept):
        kept = []           # only system prompts left: nothing to store
    return kept, receipts


def gate_derived_write(text: str, md: dict | None, *, user_id: str, bank_id: str,
                       role: str = "assistant") -> tuple[str | None, list[dict]]:
    """f0.8+（计划 ⑤ 写入门漏口）：服务端自己生成、要写进活记忆的正文（instinct 毕业的技能、self_edit
    的合并结果）过 /add 同一道门——注入拦截 + 控制字符清洗 + 原则 7 打码、边界记号中和、原则 7 整段
    拒收、enforce_messages（P1 / P8 / THINKING，拦下的进墓碑可恢复）。

    Returns (clean_text, receipts).  clean_text is None when nothing may be written; the caller then
    skips its write and keeps whatever it was about to replace.  ``md['redacted']`` gets the P7 kinds."""
    if not isinstance(text, str) or not text.strip():
        return None, []
    from ducky.security.injection_guard import neutralize_boundary_markers, validate_and_sanitize_memory_content
    kinds = redact_forbidden(text)[1]
    ok, clean, rejection = validate_and_sanitize_memory_content(text)
    if not ok:
        return None, [{"action": "reject", "principle": "INJECTION", "reason": str(rejection or "rejected")[:60]}]
    clean = neutralize_boundary_markers(clean)
    block = hard_block_reason(clean)
    if block:
        log_hard_block(user_id=user_id, bank_id=bank_id, reason=block, text=clean, md=md)
        return None, [{"action": "reject", "principle": "P7", "reason": block}]
    if kinds and isinstance(md, dict):
        md["redacted"] = sorted(set(kinds) | set(md.get("redacted") or []))
    kept, receipts = enforce_messages([{"role": role or "assistant", "content": clean}], md,
                                      user_id=user_id, bank_id=bank_id)
    if receipts:
        return None, receipts
    return clean, []


def nothing_left(messages) -> bool:
    if messages is None:
        return True
    if isinstance(messages, str):
        return not messages.strip()
    if isinstance(messages, (list, dict)):
        return not messages
    return False


# ── write observer (called by the native kernel after a committed add) ─────
def observe_written(kernel: Any, written: list[tuple[str, str, str, dict]], user_id: str, bank_id: str) -> None:
    """written: [(memory_id, role, text, metadata)].  Shadow only; never raises."""
    if policy_mode() == "off" or not written:
        return
    for mem_id, role, text, md in written:
        try:
            v = classify(text, role, md)
            # f0.8：classify 已按 source_of 服务端裁定（md 里只有 inferred 会被采信）
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
        rows = [kernel.conn.execute("SELECT id, content, metadata, created_at, mid FROM memories WHERE mid=?",
                                    (m,)).fetchone() for m in dict.fromkeys(mids)]
        sims = _vector_sims(kernel, mem_id, [r["mid"] for r in rows if r is not None], user_id, bank_id)
    rows = [r for r in rows if r is not None]
    out = []
    for r in rows:
        try:
            md = json.loads(r["metadata"] or "{}")
        except ValueError:
            md = {}
        out.append({"id": r["id"], "content": r["content"], "metadata": md if isinstance(md, dict) else {},
                    "created_at": r["created_at"], "_vec_sim": sims.get(r["mid"])})
    return out


def _vector_sims(kernel: Any, mem_id: str, mids: list, user_id: str, bank_id: str) -> dict:
    """Cosine of the new memory's stored vector against each candidate's (caller holds the lock).
    Missing vectors (FTS-only write, local gear) simply give no similarity."""
    if not mids or not hasattr(kernel, "conn"):
        return {}
    try:
        pred, params = _scope_sql(user_id, bank_id, alias="m")
        q = kernel.conn.execute(
            "SELECT v.embedding FROM memory_vectors v JOIN memories m ON m.mid=v.mid"
            " WHERE m.id=?" + pred, [mem_id, *params]).fetchone()
        if q is None:
            return {}
        from ducky.memory_kernel.impl_native import _cosines
        qv = struct.unpack(f"<{len(q[0]) // 4}f", q[0])
        got = [(m, b) for m in mids for (b,) in kernel.conn.execute(
            "SELECT embedding FROM memory_vectors WHERE mid=?", (m,)) if len(b) == len(q[0])]
        if not got:
            return {}
        return {m: c for (m, _b), c in zip(got, _cosines([b for _m, b in got], qv)) if c is not None}
    except (sqlite3.Error, ImportError, ValueError, TypeError, struct.error) as exc:
        logger.debug("write_policy vector similarity skipped: %s", type(exc).__name__)
        return {}


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
        it["source_type"] = "inferred" if inferred else "direct"
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
