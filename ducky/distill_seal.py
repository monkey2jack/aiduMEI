"""f0.8+（外审 R3-05 / R3-08）：会话精华身份由服务端裁定，不信调用方自报的 metadata。

/session/distill 只提炼不落库，调用方再把 summary + metadata 交给 /add。此前 /add 只要看到
metadata 里同时有 ``kind=session_distill``、``lane=distill``、``_origin_agent=session-distill``
就当它是精华（免掉原文长度上限、免 LLM 抽取、能挂来源级联）——三个字段都是调用方能自己填的。

现在：/session/distill 在 metadata 里附一枚服务端签名 ``distill_seal``
（HMAC-SHA256，钥匙只在服务端数据目录，绑定 身份 + 域 + 精华正文 + 来源指纹）。
/add 验签：
  · 签名对得上 → 保留精华标记（签名本身不落库）；
  · 对不上 / 没有 → 精华标记与 ``distill_*`` 字段一律剥掉，按普通写入处理
    （普通长度上限、普通来源判定）。
钥匙文件 ``distill_seal.key`` 首次使用时生成（0600）；丢了只会让在途的旧签名失效，
那几条精华按普通写入落库，不影响别的数据。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import threading

logger = logging.getLogger("aiduMEM.distill_seal")

SEAL_FIELD = "distill_seal"
_VERSION = "v1"
_KEY_FILE = "distill_seal.key"
_MARKERS = {"kind": "session_distill", "lane": "distill", "_origin_agent": "session-distill"}
_lock = threading.Lock()
_cache: dict[str, bytes] = {}


def _key_path() -> str:
    from ducky.utils import DATA_DIR
    return os.path.join(os.environ.get("AIDUMEM_DATA_DIR") or DATA_DIR, _KEY_FILE)


def _key() -> bytes:
    path = _key_path()
    with _lock:
        got = _cache.get(path)
        if got:
            return got
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w", encoding="ascii") as fh:
                fh.write(secrets.token_hex(32) + "\n")
        with open(path, encoding="ascii") as fh:
            raw = fh.read().strip()
        if len(raw) < 32:
            raise ValueError("distill seal key file is truncated")
        key = raw.encode("ascii")
        _cache[path] = key
        return key


def canonical_scope(user_id, bank_id) -> tuple[str, str]:
    """Same normalisation /add applies to (user_id, bank_id) before it reaches the seal check."""
    from ducky.bank_contract import make_scope
    from ducky.mem0_runtime import _normalize_user_id
    scope = make_scope(user_id, bank_id)
    uid = _normalize_user_id(scope.user_id) if scope.user_id else "default"
    return uid, scope.bank_id


def _text_of(messages) -> str | None:
    """The summary text an /add payload carries: a bare string, or exactly one message."""
    if isinstance(messages, str):
        return messages
    if isinstance(messages, dict):
        messages = [messages]
    if isinstance(messages, list) and len(messages) == 1:
        m = messages[0]
        if isinstance(m, str):
            return m
        if isinstance(m, dict) and isinstance(m.get("content"), str):
            return m["content"]
    return None


def _digest(text: str, user_id: str, bank_id: str, fingerprint: str, mode: str) -> str:
    body = json.dumps([_VERSION, user_id, bank_id,
                       hashlib.sha256(text.strip().encode("utf-8")).hexdigest(),
                       str(fingerprint or ""), str(mode or "")],
                      ensure_ascii=False, separators=(",", ":"))
    mac = hmac.new(_key(), body.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{_VERSION}:{mac}"


def seal(summary: str, user_id, bank_id, metadata: dict) -> str:
    """Signature /session/distill attaches to the metadata it hands back."""
    uid, bid = canonical_scope(user_id, bank_id)
    return _digest(summary or "", uid, bid, metadata.get("distill_source_fingerprint", ""),
                   metadata.get("distill_mode", ""))


def verify(messages, user_id, bank_id, metadata: dict | None) -> bool:
    md = metadata if isinstance(metadata, dict) else {}
    sig = md.get(SEAL_FIELD)
    text = _text_of(messages)
    if not isinstance(sig, str) or not sig or text is None:
        return False
    if any(md.get(k) != v for k, v in _MARKERS.items()):
        return False
    try:
        uid, bid = canonical_scope(user_id, bank_id)
        want = _digest(text, uid, bid, md.get("distill_source_fingerprint", ""), md.get("distill_mode", ""))
    except (OSError, ValueError) as exc:          # no key → nothing can verify (fail closed)
        logger.warning("distill seal unavailable: %s", type(exc).__name__)
        return False
    return hmac.compare_digest(want.encode("utf-8"), sig.encode("utf-8"))


def settle(md: dict, messages, user_id, bank_id) -> bool:
    """In place on the /add metadata: keep the summary identity only when the seal verifies.

    Returns True for a verified summary.  The seal itself is never stored."""
    if not isinstance(md, dict):
        return False
    ok = verify(messages, user_id, bank_id, md)
    md.pop(SEAL_FIELD, None)
    if ok:
        return True
    claimed = any(md.get(k) == v for k, v in _MARKERS.items()) or any(
        isinstance(k, str) and k.startswith("distill_") for k in md)
    for k, v in _MARKERS.items():
        if md.get(k) == v:
            md.pop(k, None)
    for k in [k for k in md if isinstance(k, str) and k.startswith("distill_")]:
        md.pop(k, None)
    if claimed:
        # 调用方自称「这是精华」= 自称推断产物；降格总是允许的（与 neutralize_client_provenance 同口径）
        md["source_type"], md["basis"] = "inferred", "caller_declared:unsigned_summary"
        logger.info("🛡️ [WritePolicy] /add 自报会话精华但签名不符：按普通写入处理")
    return False
