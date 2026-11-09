"""f0.8 ③-6 列表/检索 limit 统一钳制 + 读路径按钥匙限流。

外审 muse M-19 / M-20 / M-21：
  · GET /facts 无 LIMIT 全域回包；entities / graph neighbors 的 limit 不钳，负数在 SQLite 里等于无上限；
  · 限流只覆盖 /add 与登录，最贵的读/检索路径没有任何护栏。

做法（在鉴权中间件放行之后、进路由之前，一处覆盖全部路由）：
  1. query string 里的 `limit` / `top_k` / `k` / `n` / `max_results` 钳到 [1, AIDUMEI_LIST_LIMIT_MAX]（默认 500），
     `offset` 不许为负。不是整数的值原样交给路由（FastAPI 照旧 422）。`depth` 不动（路由自己 422）。
     `/graph/nebula` 自带 [50, 3000] 钳制（星云一屏要 1500 个点），不走 500 上限。
  2. 读请求（GET/HEAD 与 POST 到 */search）按「凭据摘要 → 否则来源 IP」分桶限流，
     默认 AIDUMEI_RATE_READ_PER_MIN=600/min/钥匙（CC 钩子每轮一次检索，远够不着）；0 = 关闭。
路由内部仍各自钳一次（clamp_limit），防止内部直调绕过中间件。
"""
from __future__ import annotations

import hashlib
from typing import Optional
from urllib.parse import parse_qsl, urlencode

from ducky.env_config import int_env

_LIMIT_KEYS = frozenset(("limit", "top_k", "k", "n", "max_results"))
_LIST_MAX_ENV = "AIDUMEI_LIST_LIMIT_MAX"
_READ_RATE_ENV = "AIDUMEI_RATE_READ_PER_MIN"
_DEFAULT_LIST_MAX = 500
_DEFAULT_READ_PER_MIN = 600
_OWN_CLAMP_PATHS = frozenset(("/graph/nebula", "/api/graph/nebula"))


def list_limit_max() -> int:
    return int_env(_LIST_MAX_ENV, _DEFAULT_LIST_MAX, minimum=1, maximum=100000)


def read_rate_limit() -> int:
    return int_env(_READ_RATE_ENV, _DEFAULT_READ_PER_MIN, minimum=0)


def clamp_limit(value, default: int = 50, maximum: Optional[int] = None) -> int:
    """任意输入 → [1, maximum]；解析不了用 default。"""
    hi = maximum or list_limit_max()
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = default
    return max(1, min(v, hi))


def _clamp_pair(key: str, raw: str, hi: int) -> str:
    try:
        v = int(raw)
    except (TypeError, ValueError):
        return raw
    if key in _LIMIT_KEYS:
        return str(max(1, min(v, hi)))
    if key == "offset":
        return str(max(0, v))
    return raw


def clamp_query_string(qs: bytes) -> bytes:
    if not qs:
        return qs
    try:
        pairs = parse_qsl(qs.decode("latin-1"), keep_blank_values=True)
    except (UnicodeDecodeError, ValueError):
        return qs
    hi = list_limit_max()
    changed = False
    out = []
    for k, v in pairs:
        nv = _clamp_pair(k, v, hi) if (k in _LIMIT_KEYS or k == "offset") else v
        changed |= nv != v
        out.append((k, nv))
    return urlencode(out).encode("latin-1") if changed else qs


def _bucket_key(request) -> str:
    h = request.headers
    cred = h.get("authorization") or h.get("x-api-token") or ""
    if not cred:
        try:
            from ducky.security.auth import SESSION_COOKIE_NAME
            cred = request.cookies.get(SESSION_COOKIE_NAME, "")
        except (ImportError, AttributeError):
            cred = ""
    if cred:
        return "key:" + hashlib.sha256(cred.encode("utf-8", "replace")).hexdigest()[:16]
    client = getattr(request, "client", None)
    return "ip:" + str(getattr(client, "host", "") or "?")


def is_read_request(method: str, path: str) -> bool:
    m = (method or "").upper()
    if m in ("GET", "HEAD"):
        return True
    return m == "POST" and path.rstrip("/").endswith("/search")


def apply(request) -> Optional[int]:
    """钳 query 参数（原地改 scope）；读请求超限返回 Retry-After 秒数，否则 None。"""
    scope = request.scope
    qs = scope.get("query_string") or b""
    new_qs = qs if request.url.path in _OWN_CLAMP_PATHS else clamp_query_string(qs)
    if new_qs != qs:
        scope["query_string"] = new_qs
        if hasattr(request, "_query_params"):   # Starlette 惰性缓存，改 scope 后作废
            del request._query_params
    if not is_read_request(request.method, request.url.path):
        return None
    limit = read_rate_limit()
    if limit <= 0:
        return None
    from ducky.rate_guard import check_rate
    return check_rate("read", _bucket_key(request), limit=limit)
