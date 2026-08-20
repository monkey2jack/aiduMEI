"""Shared v20 memory-bank contract and additive schema helpers.

The first generations of aiduMEI used ``source``/``agent_id`` as an
implicit tenant marker.  That was sufficient for a single bank, but it is
not a safe namespace once two independent memory domains contain the same
memory id or fact key.  This module is deliberately small and dependency
free: every storage layer can import it without importing the API server.

Compatibility rules
-------------------
* A missing bank is always the literal ``default`` bank.
* Existing rows are never deleted or rewritten to a different user.  The
  additive migration only adds columns and fills them with the compatibility
  default; legacy ``source``/``agent_id`` values remain available to callers
  that still use them.
* User supplied values are validated as opaque identifiers.  They are never
  interpolated into SQL identifiers or table names.

The helper intentionally returns SQL *predicates* and parameters separately.
Callers must append the predicate to a statement and extend the parameters;
this keeps the boundary exact-match only and makes accidental ``LIKE`` based
tenant filters difficult to introduce.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Any, Iterable

from ducky.utils import DEFAULT_USER_ID, get_facts_conn

logger = logging.getLogger("aiduMEM.bank_contract")

DEFAULT_BANK_ID = "default"
MAX_SCOPE_LENGTH = 128

# Keep the accepted alphabet intentionally conservative.  Unicode user ids
# are allowed (the deployment already supports them), while control
# characters, SQL punctuation and path separators are not.  The value is
# still bound as a SQL parameter; this check is a defence-in-depth contract,
# not an escaping mechanism.
_SCOPE_RE = re.compile(r"^[^\x00-\x1f\x7f\r\n\t/\\]{1,128}$")


class BankScopeError(ValueError):
    """Raised when a user/bank scope is empty or malformed."""


def normalize_scope_value(value: Any, *, field: str, default: str = "") -> str:
    """Normalize and validate one opaque scope component.

    ``None``/blank values are replaced by the supplied default.  We reject
    values containing control characters and path separators so the same
    contract is safe for logs, filesystem namespaces and SQL parameters.
    """
    text = str(value if value is not None else "").strip()
    if not text:
        text = str(default or "").strip()
    if not text:
        raise BankScopeError(f"{field} 不能为空")
    if len(text) > MAX_SCOPE_LENGTH or not _SCOPE_RE.fullmatch(text):
        raise BankScopeError(f"{field} 包含非法字符或长度超过 {MAX_SCOPE_LENGTH}")
    return text


def normalize_user_id(user_id: Any = None) -> str:
    """Return the canonical user id (environment-backed default)."""
    return normalize_scope_value(user_id, field="user_id", default=DEFAULT_USER_ID)


def normalize_bank_id(bank_id: Any = None) -> str:
    """Return the canonical bank id; omitted values map to ``default``."""
    return normalize_scope_value(bank_id, field="bank_id", default=DEFAULT_BANK_ID)


@dataclass(frozen=True)
class BankScope:
    """Immutable, validated scope passed between storage layers."""

    user_id: str = DEFAULT_USER_ID
    bank_id: str = DEFAULT_BANK_ID

    def __post_init__(self) -> None:
        # dataclass frozen fields can be normalised through object.__setattr__.
        object.__setattr__(self, "user_id", normalize_user_id(self.user_id))
        object.__setattr__(self, "bank_id", normalize_bank_id(self.bank_id))

    @property
    def key(self) -> str:
        """Stable non-SQL key useful for scoped legacy primary keys."""
        return f"{self.user_id}\x1f{self.bank_id}"


def make_scope(user_id: Any = None, bank_id: Any = None) -> BankScope:
    return BankScope(normalize_user_id(user_id), normalize_bank_id(bank_id))


def _table_columns(conn: Any, table: str) -> set[str]:
    """Return columns for a known table; tolerate absent/legacy tables."""
    # ``table`` is always an internal constant at call sites.  Still bind the
    # lookup parameter and never interpolate user input into this statement.
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _add_column_if_missing(conn: Any, table: str, column: str, ddl: str) -> bool:
    if column in _table_columns(conn, table):
        return False
    try:
        # table/column names come only from the fixed internal call sites.
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        return True
    except Exception as exc:
        # Concurrent workers can race the same idempotent ALTER.  Re-check
        # before logging a real migration failure.
        if column in _table_columns(conn, table):
            return False
        logger.warning("bank schema add column skipped table=%s column=%s: %s", table, column, exc)
        return False


def ensure_memory_banks_schema(conn: Any | None = None) -> dict[str, Any]:
    """Create the bank registry and additive scope columns.

    This function is safe to call from every module's lazy initialiser.  It
    never drops/rebuilds a table and never deletes data.  ``facts`` may not
    exist yet during a clean import, in which case only the registry is made;
    the next schema bootstrap call will add the columns once ``facts`` exists.
    """
    own_conn = conn is None
    conn = conn or get_facts_conn()
    added: list[str] = []
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_banks (
                user_id            TEXT NOT NULL,
                bank_id            TEXT NOT NULL,
                display_name       TEXT NOT NULL DEFAULT '',
                status             TEXT NOT NULL DEFAULT 'active',
                sensitivity_policy TEXT NOT NULL DEFAULT 'internal',
                created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (user_id, bank_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memory_banks_status "
            "ON memory_banks(user_id, status)"
        )

        # Scope columns on the fact and event ledgers.  ``user_id`` is an
        # additive canonical field; source/agent_id are intentionally kept as
        # legacy aliases and are consulted by conflict resolution during the
        # transition.
        if "facts" in _table_names(conn):
            for column, ddl in (
                ("user_id", "TEXT NOT NULL DEFAULT 'default'"),
                ("bank_id", "TEXT NOT NULL DEFAULT 'default'"),
            ):
                if _add_column_if_missing(conn, "facts", column, ddl):
                    added.append(f"facts.{column}")
            # Existing rows retain their old source/agent markers.  Only fill
            # genuinely blank canonical values; do not reinterpret a source
            # channel as ownership during migration.
            try:
                conn.execute(
                    "UPDATE facts SET user_id=? WHERE user_id IS NULL OR TRIM(user_id)=''",
                    (DEFAULT_USER_ID,),
                )
                conn.execute(
                    "UPDATE facts SET bank_id=? WHERE bank_id IS NULL OR TRIM(bank_id)=''",
                    (DEFAULT_BANK_ID,),
                )
            except Exception as exc:
                logger.debug("facts scope backfill skipped: %s", exc)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_facts_scope "
                "ON facts(user_id, bank_id)"
            )

        if "fact_events" in _table_names(conn):
            for column, ddl in (
                ("user_id", "TEXT NOT NULL DEFAULT 'default'"),
                ("bank_id", "TEXT NOT NULL DEFAULT 'default'"),
            ):
                if _add_column_if_missing(conn, "fact_events", column, ddl):
                    added.append(f"fact_events.{column}")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_fact_events_scope "
                "ON fact_events(user_id, bank_id)"
            )

        # Register the compatibility bank.  A caller may subsequently use a
        # different scope; registration is idempotent and does not overwrite
        # display/status policy chosen by an operator.
        conn.execute(
            "INSERT OR IGNORE INTO memory_banks "
            "(user_id, bank_id, display_name) VALUES (?, ?, ?)",
            (DEFAULT_USER_ID, DEFAULT_BANK_ID, "Default memory bank"),
        )
        conn.commit()
        return {"status": "ok", "added_columns": added}
    except Exception as exc:
        logger.warning("memory_banks schema 初始化失败（服务继续）: %s", exc)
        return {"status": "error", "detail": str(exc), "added_columns": added}
    finally:
        if own_conn:
            conn.close()


def _table_names(conn: Any) -> set[str]:
    try:
        return {
            str(r[0])
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    except Exception:
        return set()


def ensure_bank_registered(scope: BankScope, conn: Any | None = None) -> None:
    """Ensure a requested bank exists without changing its policy."""
    own_conn = conn is None
    conn = conn or get_facts_conn()
    try:
        ensure_memory_banks_schema(conn)
        conn.execute(
            "INSERT OR IGNORE INTO memory_banks "
            "(user_id, bank_id, display_name) VALUES (?, ?, ?)",
            (scope.user_id, scope.bank_id, scope.bank_id),
        )
        conn.commit()
    finally:
        if own_conn:
            conn.close()


def scope_predicate(
    scope: BankScope | None = None,
    *,
    alias: str = "",
    include_legacy_aliases: bool = False,
) -> tuple[str, list[str]]:
    """Return an exact-match SQL predicate for canonical scope columns.

    The returned fragment starts with ``AND`` so callers can append it to a
    pre-existing ``WHERE`` clause.  For the compatibility default scope we
    still constrain ``bank_id`` to default; this is what prevents a default
    read from accidentally seeing a newly-created named bank.

    ``include_legacy_aliases`` is only for transition code that must see old
    rows whose ownership was recorded in ``source``/``agent_id``.  It remains
    exact equality, never ``LIKE``.
    """
    scope = scope or make_scope()
    pfx = f"{alias}." if alias else ""
    if include_legacy_aliases:
        return (
            f" AND {pfx}bank_id=? AND ("
            f"{pfx}user_id=? OR {pfx}source=? OR {pfx}agent_id=?)",
            [scope.bank_id, scope.user_id, scope.user_id, scope.user_id],
        )
    return (
        f" AND {pfx}user_id=? AND {pfx}bank_id=?",
        [scope.user_id, scope.bank_id],
    )


def legacy_fact_scope_predicate(
    scope: BankScope | None = None, *, alias: str = ""
) -> tuple[str, list[str]]:
    """Transition predicate for facts with mixed old/new ownership fields.

    Canonical rows use ``user_id + bank_id``.  During additive migration a
    deployment can still contain rows written before ``user_id`` existed;
    those rows are visible in the default bank only, or when their legacy
    source/agent marker exactly equals the requested user.  Named banks never
    fall back to an unscoped row.

    ⚠️ 归属只认 ``user_id``；``source``/``agent_id`` 是**渠道标记**，不是所有权。
    本条谓词初版把两者并列成 OR::

        AND bank_id=? AND (user_id=? OR user_id IS NULL
                           OR source=? OR agent_id=?)

    这几个 OR 分支对**已经有正规归属的行也照样生效** —— 只要某一行的
    ``source`` 恰好等于另一个租户的 id，那一行就会出现在对方的读结果里。
    而 ``source`` 取的是 ``cli``/``hook``/``mcp``/``local`` 这类值，租户
    完全可能叫这些名字；v19 里也确实有把用户名写进 source/agent_id 的行。
    换句话说：**这是一条跨租户读的口子**，且它出现在契约层、被 ``__all__``
    导出、docstring 还写着「这是读老行的正规姿势」。

    现在把回落分支**关进「这行还没有正规主人」的前提里**：有主的行一律
    只认 ``user_id``，渠道标记再像也不给看。

    「还没有主人」= ``user_id`` 为 NULL/空白，**或仍是占位符 ``default``**。
    最后这一项不能少：迁移会把所有存量行的 ``user_id`` 回填成 ``default``，
    若只认 NULL/空白，回落分支在真实升级库上一行都命不中 —— v19 里靠
    source/agent_id 记录归属的租户会连自己的记忆都读不到、删不掉
    （`test_cascade_delete_isolation_and_exact_match` 正是钉这件事的）。
    占位符代表「尚未认领」，不代表「属于 default 这个人」。

    调用点（v20 起已接通，此注保持与代码同步）：
    ``wal_engine.cascade_delete_all``（facts 步骤的作用域枚举）与
    ``conflict_resolver._fact_scope_sql``（四列齐全时直接委托本函数）；
    ``facts_recall.tenant_clause`` 的迁移路径与本谓词同语义独立实现
    （它还要多管 strict/宽松档与共享 agent 兜底）。
    迁移把存量行 ``user_id`` 回填成 ``'default'``（占位＝尚未认领），
    所以回落分支恰好命中这批行 —— v19 里靠 source/agent_id 记归属的
    租户升级后仍能读到、删得掉自己的记忆。
    """
    scope = scope or make_scope()
    pfx = f"{alias}." if alias else ""
    if scope.bank_id == DEFAULT_BANK_ID:
        return (
            f" AND {pfx}bank_id=? AND ("
            f"{pfx}user_id=? OR ("
            f"({pfx}user_id IS NULL OR TRIM({pfx}user_id)='' "
            f"OR {pfx}user_id=?) AND "
            f"({pfx}source=? OR {pfx}agent_id=?)))",
            [
                scope.bank_id, scope.user_id,
                DEFAULT_USER_ID, scope.user_id, scope.user_id,
            ],
        )
    return (
        f" AND {pfx}bank_id=? AND {pfx}user_id=?",
        [scope.bank_id, scope.user_id],
    )


def scoped_storage_key(raw_key: Any, scope: BankScope | None = None) -> str:
    """Build a collision-free internal key for legacy single-column PKs."""
    scope = scope or make_scope()
    raw = str(raw_key or "").strip()
    if not raw:
        raise BankScopeError("memory key 不能为空")
    # Every pre-v20 row lives in the default bank with a bare id — for any
    # tenant, not only the default user.  The default bank therefore keeps the
    # exact v19 key shape (ownership stays enforced by the user_id/bank_id
    # columns); only named banks get a prefixed key to avoid PK collisions.
    if scope.bank_id == DEFAULT_BANK_ID:
        return raw
    # Length is bounded after validation; the separator is a control byte and
    # therefore cannot occur in a valid user/bank/raw key.
    return f"{scope.user_id}\x1f{scope.bank_id}\x1f{raw}"


def raw_storage_key(value: Any, scope: BankScope | None = None) -> str:
    """Recover a public key from a scoped legacy key."""
    scope = scope or make_scope()
    text = str(value or "")
    prefix = f"{scope.user_id}\x1f{scope.bank_id}\x1f"
    return text[len(prefix):] if text.startswith(prefix) else text


# ── 向量侧的域契约 ────────────────────────────────────────────────────
# 🔴v20 结构性缺陷：v20 在五个向量读取点加了 ``filters={"user_id":…,
# "bank_id":…}``，但**写入侧从来没把 bank_id 盖进 mem0 metadata**（全仓只有
# hot/crud.py 的 update 一处）。而 Qdrant 的 payload 过滤是 must 语义 ——
# payload 里没有 bank_id 这个**字段**的点，会被 ``bank_id=?`` 条件直接判为不
# 匹配（已实测：同一批向量，只按 user_id 过滤返回 2 条，加上 bank_id=default
# 只返回 1 条，缺字段的那条被滤掉）。
#
# 后果不是「命名域搜不到」，而是**所有域、所有租户的向量召回全部归零**，
# 默认域和全部 v19 存量数据一起消失。且它不抛异常：mem0 返回空列表，
# engine.py 的 except 都不会进，日志一行不留 —— 标准的静默失败。
#
# 因此向量侧的域隔离必须拆成两半来做：
#   · 过滤下推（:func:`vector_scope_filters`）：只有**命名域**才把 bank_id
#     下推给 mem0。默认域沿用 v19 的裸过滤，保证存量向量可见。
#   · 结果复筛（:func:`vector_item_in_bank`）：回到 Python 侧按域复筛，把
#     命名域的点从默认域结果里剔掉。**缺字段一律算默认域**（存量语义）。
# 两半合起来，才等价于 FTS 侧 ``m.bank_id=?`` 那种严格隔离。
#
# 为什么不下推一个 ``OR(bank_id=default, bank_id 不存在)``：mem0 的
# ``_build_field_condition`` 对通配 ``{"k": "*"}`` 直接返回 ``None``（注释写着
# "Qdrant has no direct field exists condition … skip this filter"），
# 它的过滤语言压根表达不了「字段缺失」。


def vector_scope_filters(user_id: Any = None, bank_id: Any = None) -> dict[str, Any]:
    """构造 mem0 ``search``/``get_all`` 的 filters。

    默认域**故意不下推** bank_id —— 存量向量没有这个 payload 字段，下推即清零。
    与 :func:`scoped_storage_key` 同一套哲学：默认域保持 v19 原形。
    调用方拿到结果后必须再用 :func:`vector_item_in_bank` 复筛，否则命名域的
    点会漏进默认域结果里。
    """
    scope = make_scope(user_id, bank_id)
    filters: dict[str, Any] = {"user_id": scope.user_id}
    if scope.bank_id != DEFAULT_BANK_ID:
        filters["bank_id"] = scope.bank_id
    return filters


def vector_item_bank(item: Any) -> str:
    """取一条 mem0 返回项所属的域；顶层优先，其次 metadata/payload。

    取不到就是 :data:`DEFAULT_BANK_ID` —— v19 存量点没有这个字段，它们**就是**
    默认域的数据，不是「未知」。
    """
    if not isinstance(item, dict):
        return DEFAULT_BANK_ID
    raw = item.get("bank_id")
    if raw in (None, ""):
        for holder in ("metadata", "payload"):
            nested = item.get(holder)
            if isinstance(nested, dict) and nested.get("bank_id") not in (None, ""):
                raw = nested.get("bank_id")
                break
    return str(raw or "").strip() or DEFAULT_BANK_ID


def vector_item_in_bank(item: Any, bank_id: Any = None) -> bool:
    """结果复筛谓词：这条向量召回结果属不属于目标域。"""
    return vector_item_bank(item) == normalize_bank_id(bank_id)


def stamp_bank_metadata(metadata: Any, bank_id: Any = None) -> dict[str, Any]:
    """写入侧盖章：把 bank_id 写进 mem0 metadata。

    不盖这个戳，向量 payload 里就没有 bank_id 字段，命名域的隔离在向量侧
    等于不存在（见本节顶部说明）。返回新 dict，不改调用方的原对象。
    """
    md = dict(metadata or {})
    md["bank_id"] = normalize_bank_id(bank_id)
    return md


def list_banks(*, user_id: Any = None, status: str = "") -> list[dict[str, Any]]:
    """List banks for one user; exact-match filtering only."""
    scope_user = normalize_user_id(user_id)
    conn = get_facts_conn()
    try:
        ensure_memory_banks_schema(conn)
        sql = "SELECT * FROM memory_banks WHERE user_id=?"
        params: list[Any] = [scope_user]
        if status:
            sql += " AND status=?"
            params.append(str(status).strip())
        sql += " ORDER BY bank_id"
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


__all__ = [
    "BankScope",
    "BankScopeError",
    "DEFAULT_BANK_ID",
    "DEFAULT_USER_ID",
    "ensure_memory_banks_schema",
    "ensure_bank_registered",
    "legacy_fact_scope_predicate",
    "list_banks",
    "make_scope",
    "normalize_bank_id",
    "normalize_scope_value",
    "normalize_user_id",
    "raw_storage_key",
    "scope_predicate",
    "scoped_storage_key",
    "stamp_bank_metadata",
    "vector_item_bank",
    "vector_item_in_bank",
    "vector_scope_filters",
]
