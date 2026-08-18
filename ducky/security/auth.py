"""
ducky.security.auth — 控制台凭据与会话的单一真相源（v19.4.1 · P0-1 / P2-2）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

为什么需要这个模块
    v19.4.0 及之前，控制台有两套互不相通的凭据：

      1. `AIDUMEM_API_TOKEN` → HTTP 中间件校验 `Authorization: Bearer`；
      2. `AIDUMEM_UI_PASSWORD` / `data/.ui_password_hash` → `/login` 校验，
         校验通过后**只在浏览器 sessionStorage 写了个标记**。

    前端 `js/api.js` 从不发送 Authorization 头，于是两套凭据组合出两种
    都不可用的部署形态（v19.4.1 审计探针实测）：

      · 只设 UI 密码（最自然的部署方式）：中间件因 token 为空整段放行，
        未登录直接 `GET /api/facts` → **200**，全部记忆裸奔。
        UI 密码对 REST 接口完全无效 —— 它只是一道前端障眼法。
      · 只设 API token：`/api/login` 返回 200，但登录后所有面板请求
        → **401**，控制台彻底报废。

    根因：**认证结果没有服务端载体**。密码校验通过后没有任何服务端凭据
    被签发，浏览器后续请求无法自证身份。

本模块的解法
    · 会话：`/login` 成功后服务端签发不可猜测的 session token，
      以 HttpOnly + SameSite=Lax cookie 下发；服务端持有过期时间与
      撤销能力。cookie 与 Bearer token 是**同一道门禁的两把钥匙**，
      任一有效即放行（浏览器用前者，脚本/MCP 用后者）。
    · 口令：统一 PBKDF2-HMAC-SHA256（200k 轮）取代单轮 sha256；
      兼容旧格式 `salt:sha256hex`，校验通过后**自动升级**为新格式，
      存量部署无需改密码、无需人工干预（P2-2）。

安全承诺
    · 会话 token 用 `secrets.token_urlsafe(32)` 生成，比较走 `hmac.compare_digest`
    · 只存 token 的哈希，不落明文；进程内存储，重启即全部失效
    · 密码哈希文件权限收紧到 0600
    · 任何异常都不会把门禁「失败放行」——认证失败一律拒绝
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time

logger = logging.getLogger("aiduMEM.security.auth")

# ── 口令哈希 ────────────────────────────────────────────────────────────

_PBKDF2_ALGO = "pbkdf2_sha256"
_PBKDF2_ROUNDS = 200_000

# 会话默认有效期 12 小时；可由部署方按需调整。
SESSION_TTL_SECONDS = int(os.environ.get("AIDUMEM_SESSION_TTL_SECONDS", "43200"))
SESSION_COOKIE_NAME = "aidumei_session"


def hash_password(password: str) -> str:
    """生成 `pbkdf2_sha256$<rounds>$<salt_hex>$<hash_hex>` 格式的口令哈希。"""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return f"{_PBKDF2_ALGO}${_PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> tuple[bool, bool]:
    """校验口令。返回 (是否通过, 是否需要升级哈希格式)。

    兼容两种存储格式：
      · 新：`pbkdf2_sha256$<rounds>$<salt_hex>$<hash_hex>`
      · 旧：`<salt_hex>:<sha256_hex>`（v19.4.0 及之前的单轮 sha256）
    旧格式校验通过时第二个返回值为 True，调用方应就地重写为新格式。
    """
    if not isinstance(password, str) or not password:
        return False, False
    stored = (stored or "").strip()
    if not stored:
        return False, False

    try:
        if stored.startswith(_PBKDF2_ALGO + "$"):
            _, rounds_s, salt_hex, expected = stored.split("$", 3)
            dk = hashlib.pbkdf2_hmac(
                "sha256", password.encode("utf-8"),
                bytes.fromhex(salt_hex), int(rounds_s),
            )
            return hmac.compare_digest(dk.hex(), expected), False

        if ":" in stored:
            salt, expected = stored.split(":", 1)
            cand = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
            ok = hmac.compare_digest(cand, expected)
            # 旧格式：校验通过则提示调用方升级
            return ok, ok
    except Exception as exc:
        logger.debug("verify_password 解析失败（视为不通过）: %s", exc)

    return False, False


def password_hash_path() -> str:
    from ducky.utils import DATA_DIR
    return os.path.join(DATA_DIR, ".ui_password_hash")


# 口令来源标记（provenance）。文件格式：
#     第 1 行 = 哈希本体
#     第 2 行 = `source=auto` | `source=user`（缺失时按 user 处理，兼容旧文件）
#
# 为什么需要区分来源（v19.4.1 施工中发现的兼容风险）：
#     自 v19.2.0 起，服务启动时若未配置 AIDUMEM_UI_PASSWORD 会**自动生成**
#     一个随机口令并落哈希文件。也就是说「哈希文件存在」在所有存量部署里
#     都成立。若把它一律当作「部署方已配置口令」来启用 API 门禁，
#     那么升级瞬间既有的 hermes 插件、MCP 客户端、cron 脚本会全部 401 ——
#     它们从来只走回环、从不带凭据。这是不可接受的破坏性变更。
#
#     因此：只有**部署方显式设置**的口令（环境变量，或通过控制台改过密）
#     才启用 API 门禁；自动生成的口令只守 UI 登录，不改变 API 的既有语义。
#     公网部署另有独立防线 —— main() 在非回环监听且无 token 时拒绝启动。
_SOURCE_AUTO = "auto"
_SOURCE_USER = "user"


def _read_hash_file() -> tuple[str, str]:
    """读取 (哈希本体, 来源标记)。文件不存在或读失败返回 ("", "")。"""
    path = password_hash_path()
    if not os.path.exists(path):
        return "", ""
    try:
        with open(path, encoding="utf-8") as f:
            lines = [ln.strip() for ln in f.read().splitlines() if ln.strip()]
    except Exception as exc:
        logger.debug("读取口令哈希失败: %s", exc)
        return "", ""
    if not lines:
        return "", ""
    hashed = lines[0]
    # 旧文件（v19.4.0 及之前）没有 source 行，无法区分它是自动生成的还是
    # 部署方改过密的 —— 两种情况写的都是同一个 `salt:hash` 格式。
    # 这里取 auto：**升级不改变任何既有部署的 API 访问语义**。
    # 理由：v19.2.0 起「未配口令就自动生成」是默认路径，绝大多数存量哈希
    # 文件都是自动生成的；若一律按 user 处理，既有的 hermes 插件 / MCP /
    # cron（全走回环、从不带凭据）会在升级瞬间集体 401 —— 破坏性变更。
    # 想启用门禁的部署方有两条明确路径：设 AIDUMEM_API_TOKEN，
    # 或通过控制台改一次密（会写 source=user）。
    # 公网场景另有独立防线：main() 在非回环监听且无 token 时拒绝启动。
    source = _SOURCE_AUTO
    for ln in lines[1:]:
        if ln.startswith("source="):
            source = ln.split("=", 1)[1].strip() or _SOURCE_USER
    return hashed, source


def read_password_hash() -> str:
    return _read_hash_file()[0]


def password_source() -> str:
    """口令来源：auto（自动生成）| user（部署方设置）| ""（无口令）。"""
    hashed, source = _read_hash_file()
    return source if hashed else ""


def write_password_hash(hashed: str, source: str = _SOURCE_USER) -> bool:
    """写入口令哈希（含来源标记）并把文件权限收紧到 0600。"""
    path = password_hash_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"{hashed}\nsource={source}\n")
        try:
            os.chmod(path, 0o600)
        except Exception as pexc:
            logger.debug("口令哈希文件权限设置跳过: %s", pexc)
        return True
    except Exception as exc:
        logger.error("写入口令哈希失败: %s", exc)
        return False


def ui_password_configured() -> bool:
    """部署方是否**显式**配置了控制台口令 —— 决定 API 门禁是否启用。

    自动生成的口令（source=auto）不计入：它只守 UI 登录，
    不改变既有部署的 API 访问语义（见上方 provenance 说明）。
    """
    if os.environ.get("AIDUMEM_UI_PASSWORD", "").strip():
        return True
    hashed, source = _read_hash_file()
    return bool(hashed) and source != _SOURCE_AUTO


def check_ui_password(password: str) -> bool:
    """校验控制台口令（哈希文件优先，回退环境变量）。

    旧格式哈希校验通过后就地升级为 PBKDF2（P2-2 平滑迁移）。
    """
    if not isinstance(password, str) or not password:
        return False

    stored, source = _read_hash_file()
    if stored:
        ok, needs_upgrade = verify_password(password, stored)
        if ok:
            # 升级哈希格式时**保留原来源标记** —— 否则自动生成的口令会被
            # 误升成 user，凭空给存量部署打开 API 门禁。
            if needs_upgrade and write_password_hash(
                hash_password(password), source or _SOURCE_USER
            ):
                logger.info("🔐 控制台口令哈希已自动升级为 PBKDF2（旧单轮 sha256 已淘汰）")
            return True

    env_pwd = os.environ.get("AIDUMEM_UI_PASSWORD", "").strip()
    if env_pwd and hmac.compare_digest(password, env_pwd):
        return True
    return False


# ── 会话存储 ────────────────────────────────────────────────────────────
#
# 进程内存储：服务重启后所有会话失效（自托管单进程场景下这是期望行为，
# 也避免把会话状态落盘带来额外的泄露面）。

_sessions: dict[str, float] = {}   # token_hash -> expires_at(epoch)
_session_lock = threading.Lock()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _purge_expired_locked(now: float) -> None:
    for th in [th for th, exp in _sessions.items() if exp <= now]:
        _sessions.pop(th, None)


def create_session(ttl_seconds: int | None = None) -> tuple[str, int]:
    """签发会话。返回 (明文 token, 有效期秒数)。明文只在此刻出现一次。"""
    token = secrets.token_urlsafe(32)
    ttl = int(ttl_seconds or SESSION_TTL_SECONDS)
    now = time.time()
    with _session_lock:
        _purge_expired_locked(now)
        _sessions[_token_hash(token)] = now + ttl
    return token, ttl


def validate_session(token: str) -> bool:
    """校验会话 token 是否有效且未过期。"""
    if not token or not isinstance(token, str):
        return False
    th = _token_hash(token)
    now = time.time()
    with _session_lock:
        _purge_expired_locked(now)
        exp = _sessions.get(th)
        return bool(exp and exp > now)


def revoke_session(token: str) -> bool:
    if not token:
        return False
    with _session_lock:
        return _sessions.pop(_token_hash(token), None) is not None


def revoke_all_sessions() -> int:
    """撤销全部会话（改密码后调用，强制所有端重新登录）。"""
    with _session_lock:
        n = len(_sessions)
        _sessions.clear()
        return n


def active_session_count() -> int:
    now = time.time()
    with _session_lock:
        _purge_expired_locked(now)
        return len(_sessions)
