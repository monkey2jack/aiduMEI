"""
aiduMEM CoreMemory — LLM 可编辑结构化记忆块
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
v11 Hyperion · 三大块：user_profile / current_project / key_decisions
LLM 通过 API 自行维护，每轮注入到 Hermes 上下文

v11.1 Opus 升级：30天验证失效机制，超期 block 注入时标注 [⚠️ 需验证]
"""
import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta

from ducky.utils import DEFAULT_USER_ID, get_facts_conn
from ducky.bank_contract import (
    DEFAULT_BANK_ID,
    ensure_bank_registered,
    ensure_memory_banks_schema,
    is_legacy_schema_error,
    make_scope,
    raw_storage_key,
    scoped_storage_key,
)

logger = logging.getLogger("aiduMEM.CoreMemory")

# 三大 block 的键名和显示标签
BLOCK_KEYS = {
    "core_user_profile":     "👤 user",
    "core_current_project":  "📋 当前项目",
    "core_key_decisions":    "🔑 关键决策",
}

# 默认初始值 — 首次建表时写入的占位内容。
# 这三块由 LLM 通过 API 自行改写，因此默认值只需说明「该写什么」，
# 不要在这里硬编码任何真实的用户信息。
DEFAULT_BLOCKS = {
    "core_user_profile": (
        "（尚未填写）用户的稳定身份信息：称呼、时区、语言偏好、职业背景、沟通风格。"
    ),
    "core_current_project": (
        "（尚未填写）当前正在推进的项目：目标、技术栈、进度、下一步。"
    ),
    "core_key_decisions": (
        "（尚未填写）已达成的关键决策与约定：架构选择、操作红线、协作规范。"
    ),
}

# 验证失效天数：超过此天数未更新的 block 注入时标注为需验证
STALENESS_DAYS = 30

_init_lock = threading.Lock()
_initialized = False
_initialized_scopes: set[tuple[str, str]] = set()


def _get_conn():
    """复用 utils 的线程本地连接"""
    return get_facts_conn()


def _ensure_table():
    """确保 core_memory 表存在，并自动迁移新增列"""
    conn = _get_conn()
    try:
        ensure_memory_banks_schema(conn)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS core_memory (
                block_key        TEXT PRIMARY KEY,
                content          TEXT NOT NULL,
                updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                user_id          TEXT NOT NULL DEFAULT 'default',
                bank_id          TEXT NOT NULL DEFAULT 'default',
                block_key_raw    TEXT
            )
        """)
        # 自动迁移：旧表可能没有 last_verified_at 列
        try:
            conn.execute("SELECT last_verified_at FROM core_memory LIMIT 1")
        except sqlite3.OperationalError as exc:
            # 甲8：探针失败不等于「缺这一列」。库被锁、磁盘写满同样抛
            # OperationalError，此时去 ALTER 只会再失败一次，最终抛出来的是那个
            # 二次错误（甚至可能是 "duplicate column"），把真正的病因盖住。
            # 这一处的危害**止于难查**——ALTER 也必然失败，不存在静默走错，
            # 不必夸大；但让报错说真话是本轮的统一口径。
            if not is_legacy_schema_error(exc):
                raise
            conn.execute("ALTER TABLE core_memory ADD COLUMN last_verified_at TIMESTAMP")
            # 用 updated_at 回填
            conn.execute("UPDATE core_memory SET last_verified_at = updated_at WHERE last_verified_at IS NULL")
            logger.info("CoreMemory: 迁移添加 last_verified_at 列")
        # v20.0 additive scope migration.  Keep block_key as the legacy
        # primary key and use a deterministic scoped storage key for named
        # banks; block_key_raw remains the public API key.
        for column, ddl in (
            ("user_id", "TEXT NOT NULL DEFAULT 'default'"),
            ("bank_id", "TEXT NOT NULL DEFAULT 'default'"),
            ("block_key_raw", "TEXT"),
        ):
            try:
                conn.execute(f"ALTER TABLE core_memory ADD COLUMN {column} {ddl}")
            except Exception:
                pass
        conn.execute(
            "UPDATE core_memory SET block_key_raw=block_key "
            "WHERE block_key_raw IS NULL OR block_key_raw=''"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_core_memory_scope_key "
            "ON core_memory(user_id, bank_id, block_key_raw)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_core_memory_scope "
            "ON core_memory(user_id, bank_id)"
        )
        conn.commit()
    except Exception as e:
        logger.error(f"CoreMemory ensure table error: {e}")
        raise
    finally:
        conn.close()


def _seed_defaults(scope=None) -> dict:
    """首次运行时写入指定 bank 的默认值。

    返回 ``{"readable": int, "blocked": list[str]}`` —— 播种完成后这个域**实际
    能读到几块**，以及有哪几块因为裸键被别的域占着而根本没能建起来。

    🔴甲16（v20.0）：这里原来是一句光秃秃的 ``INSERT OR IGNORE``，而
    ``INSERT OR IGNORE`` 撞上冲突时**不报错、不写入、也不吭声**。默认域对任何
    租户都用 v19 裸键（硬契约，见 put_block 里 甲1a 的长注释），于是：v19 存量
    行被增量迁移打上 ``user_id='default'`` 的戳 → 运行时身份是个非 default 的
    值 → 播种时裸键已被 default 那三行占着 → 三次 IGNORE、一行没建 →
    ``get_all_blocks`` 查这个域得 0 行 → ``inject_context()`` 返回 ``""``。

    **整个 CoreMemory 静默失效，日志上一个字都没有。** 这就是「静默拒绝」比
    「静默覆盖」更难发现的地方：覆盖至少留下了一行被改坏的数据，拒绝什么都不留，
    而调用方拿到的是「初始化完成」。

    本轮只做「让它可见」这一件事：写路径**一个字节不动**（下面那句 INSERT OR
    IGNORE 照旧无条件执行，观察代码不参与任何决策——护栏自己不许有能力改变结果），
    只在播完之后回查实际可读块数，撞域的留 warning。真正的修复是把存量行归并到
    正确的域，属于 丙9 数据对账；键形状改造属于 甲1b，且必须排在 丙9 之后。
    """
    scope = scope or make_scope()
    try:
        ensure_bank_registered(scope)
    except Exception as exc:
        logger.debug("CoreMemory bank registration skipped: %s", exc)
    conn = _get_conn()
    blocked: list[str] = []
    readable = 0
    try:
        now = datetime.now().isoformat()
        for key, content in DEFAULT_BLOCKS.items():
            conn.execute(
                "INSERT OR IGNORE INTO core_memory "
                "(block_key, content, updated_at, last_verified_at, user_id, bank_id, block_key_raw) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    scoped_storage_key(key, scope), content, now, now,
                    scope.user_id, scope.bank_id, key,
                )
            )
        conn.commit()
        # 播种之后回查：这个域现在究竟能读到几块，哪几块被别的域占着。
        for key in DEFAULT_BLOCKS:
            owner = conn.execute(
                "SELECT user_id, bank_id FROM core_memory WHERE block_key=?",
                (scoped_storage_key(key, scope),),
            ).fetchone()
            if owner is None:
                continue
            if (owner["user_id"], owner["bank_id"]) == (scope.user_id, scope.bank_id):
                readable += 1
            else:
                blocked.append(key)
        if blocked:
            logger.warning(
                "CoreMemory 播种被静默拒绝：user_id=%s bank_id=%s 下有 %d/%d 块建不起来"
                "（裸键已被别的域占着，INSERT OR IGNORE 不报错也不写入）—— "
                "这个域的 inject_context() 会返回空串，CoreMemory 对它整块失效。"
                "受影响 block：%s；存量行归并见 丙9 数据对账（甲16）",
                scope.user_id, scope.bank_id, len(blocked), len(DEFAULT_BLOCKS),
                ",".join(blocked),
            )
    except Exception as e:
        logger.error(f"CoreMemory seed defaults error: {e}")
        raise
    finally:
        conn.close()
    return {"readable": readable, "blocked": blocked}


def init_core_memory(
    user_id: str = DEFAULT_USER_ID,
    bank_id: str = DEFAULT_BANK_ID,
):
    """初始化指定 bank 的 CoreMemory（幂等）。"""
    global _initialized, _initialized_scopes
    scope = make_scope(user_id, bank_id)
    scope_key = (scope.user_id, scope.bank_id)
    with _init_lock:
        if scope_key in _initialized_scopes:
            return
        _ensure_table()
        seeded = _seed_defaults(scope)
        _initialized_scopes.add(scope_key)
        _initialized = True
        # 🔴甲16：这句原来无条件写「3 个 block 就绪」。播种被静默拒绝时一块都没
        # 就绪，它照样这么报 —— 日志本身在撒谎，比没有日志更糟。改成报实数。
        logger.info(
            "CoreMemory 初始化完成（%d/%d 个 block 就绪） user_id=%s bank_id=%s",
            seeded["readable"], len(DEFAULT_BLOCKS), scope.user_id, scope.bank_id,
        )


def get_all_blocks(
    user_id: str = DEFAULT_USER_ID,
    bank_id: str = DEFAULT_BANK_ID,
) -> dict:
    """读取指定 bank 的全部 block（含验证时间）。"""
    scope = make_scope(user_id, bank_id)
    _ensure_table()
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT block_key, block_key_raw, content, updated_at, last_verified_at "
            "FROM core_memory WHERE user_id=? AND bank_id=?",
            (scope.user_id, scope.bank_id),
        ).fetchall()
        return {(r["block_key_raw"] or raw_storage_key(r["block_key"], scope)): {
            "content": r["content"],
            "updated_at": r["updated_at"],
            "last_verified_at": r["last_verified_at"] or r["updated_at"],
        } for r in rows}
    except Exception as e:
        logger.error(f"CoreMemory get_all_blocks error: {e}")
        return {}
    finally:
        conn.close()


def get_block(
    block_key: str,
    user_id: str = DEFAULT_USER_ID,
    bank_id: str = DEFAULT_BANK_ID,
) -> dict | None:
    """读取指定 bank 的单个 block（含验证时间）。"""
    scope = make_scope(user_id, bank_id)
    _ensure_table()
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT block_key, block_key_raw, content, updated_at, last_verified_at "
            "FROM core_memory WHERE user_id=? AND bank_id=? "
            "AND (block_key_raw=? OR block_key=?)",
            (scope.user_id, scope.bank_id, block_key, scoped_storage_key(block_key, scope)),
        ).fetchone()
        if not row:
            return None
        return {
            "block_key": row["block_key_raw"] or raw_storage_key(row["block_key"], scope),
            "content": row["content"],
            "updated_at": row["updated_at"],
            "last_verified_at": row["last_verified_at"] or row["updated_at"],
        }
    except Exception as e:
        logger.error(f"CoreMemory get_block error for {block_key}: {e}")
        return None
    finally:
        conn.close()


def put_block(
    block_key: str,
    content: str,
    user_id: str = DEFAULT_USER_ID,
    bank_id: str = DEFAULT_BANK_ID,
) -> dict:
    """更新指定 bank 的单个 block（同时刷新验证时间）。"""
    if block_key not in BLOCK_KEYS:
        raise ValueError(f"无效的 block_key: {block_key}，允许值: {list(BLOCK_KEYS.keys())}")

    # 长度和内容校验
    content = content.strip() if content else ""
    if len(content) < 10:
        raise ValueError("content 太短，至少 10 个字符")
    if len(content) > 600:
        raise ValueError("content 太长，最多 600 个字符")

    scope = make_scope(user_id, bank_id)
    _ensure_table()
    try:
        ensure_bank_registered(scope)
    except Exception as exc:
        logger.debug("CoreMemory bank registration skipped: %s", exc)
    now = datetime.now().isoformat()
    conn = _get_conn()
    owner_user, owner_bank = scope.user_id, scope.bank_id
    try:
        storage_key = scoped_storage_key(block_key, scope)
        # 🔴甲1a（v20.0）：键的形状本轮**一个字不动**。
        #
        # 默认域对**任何**租户都保留 v19 裸键，这不是疏漏而是硬契约，见
        # tests/test_v20_bank_scope_core.py::
        #   test_default_bank_keeps_the_v19_key_shape_for_every_tenant
        # —— 那条用例是从一次真实的「非默认租户的 v19 存量行整体失踪」里长出来
        # 的：写进去带前缀、读出来带前缀，自己跟自己对得上，测试全绿，而升级前
        # 就存在的裸键行从此再也没人去读。
        #
        # 代价是单列主键 block_key 比唯一索引 (user_id,bank_id,block_key_raw)
        # 粗一档：默认域里两个域写同一个 block_key 会撞进同一行。本轮只做两件
        # **不可能变坏**的事：
        #   ① upsert 不再回写 user_id/bank_id —— 一次内容更新永远不该把这一行
        #      搬到另一个域去。这是本条缺陷里唯一「静默改归属」的来源。
        #   ② 撞上别的域时留一条 warning，并把**真实归属**回给调用方，让这个
        #      现象在生产上可见（丙9 域对账要靠它认出哪些行需要归并）。
        #
        # 真正的隔离（键形状改造 + 读侧回落 + 存量迁移）横跨 5 个模块
        # （tombstone / memory_types / text_fts / core_memory / wal_engine），
        # 依赖 丙1 的租户模型定论，且**必须排在 丙9 数据对账之后**：现在就把
        # 撞键改成硬失败，会把生产机器上「运行时身份写、存量行挂在 default」
        # 那条正在工作的路径直接打断。
        owner = conn.execute(
            "SELECT user_id, bank_id FROM core_memory WHERE block_key=?",
            (storage_key,),
        ).fetchone()
        if owner is not None and (owner["user_id"], owner["bank_id"]) != (scope.user_id, scope.bank_id):
            owner_user, owner_bank = owner["user_id"], owner["bank_id"]
            logger.warning(
                "CoreMemory 跨域撞键：block_key=%s 现属 user_id=%s bank_id=%s，"
                "本次写入来自 user_id=%s bank_id=%s —— 内容被更新，归属保持不变（甲1a/丙9）",
                block_key, owner_user, owner_bank, scope.user_id, scope.bank_id,
            )
        conn.execute(
            "INSERT INTO core_memory "
            "(block_key, content, updated_at, last_verified_at, user_id, bank_id, block_key_raw) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(block_key) DO UPDATE SET content=excluded.content, "
            "updated_at=excluded.updated_at, last_verified_at=excluded.last_verified_at, "
            "block_key_raw=excluded.block_key_raw",
            (
                storage_key, content, now, now,
                scope.user_id, scope.bank_id, block_key,
            )
        )
        conn.commit()
    except Exception as e:
        logger.error(f"CoreMemory put_block error for {block_key}: {e}")
        raise
    finally:
        conn.close()

    logger.info(
        "CoreMemory: %s 已更新+验证 user_id=%s bank_id=%s",
        block_key, owner_user, owner_bank,
    )
    return {
        "block_key": block_key,
        "user_id": owner_user,
        "bank_id": owner_bank,
        "content": content,
        "updated_at": now,
        "last_verified_at": now,
        "status": "ok",
    }


def verify_block(
    block_key: str,
    user_id: str = DEFAULT_USER_ID,
    bank_id: str = DEFAULT_BANK_ID,
) -> dict:
    """仅刷新指定 bank 的验证时间（不修改内容）。"""
    if block_key not in BLOCK_KEYS:
        raise ValueError(f"无效的 block_key: {block_key}，允许值: {list(BLOCK_KEYS.keys())}")

    scope = make_scope(user_id, bank_id)
    _ensure_table()
    now = datetime.now().isoformat()
    conn = _get_conn()
    try:
        cursor = conn.execute(
            "UPDATE core_memory SET last_verified_at = ? WHERE user_id=? AND bank_id=? "
            "AND (block_key_raw=? OR block_key=?)",
            (now, scope.user_id, scope.bank_id, block_key, scoped_storage_key(block_key, scope)),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return {"block_key": block_key, "user_id": scope.user_id, "bank_id": scope.bank_id, "status": "not_found"}
    except Exception as e:
        logger.error(f"CoreMemory verify_block error for {block_key}: {e}")
        raise
    finally:
        conn.close()

    logger.info(
        "CoreMemory: %s 已验证（内容未改） user_id=%s bank_id=%s",
        block_key, scope.user_id, scope.bank_id,
    )
    return {
        "block_key": block_key,
        "user_id": scope.user_id,
        "bank_id": scope.bank_id,
        "last_verified_at": now,
        "status": "verified",
    }


def _is_stale(last_verified: str) -> bool:
    """判断 block 是否超过 STALENESS_DAYS 未验证"""
    if not last_verified:
        return True
    try:
        verified_dt = datetime.fromisoformat(last_verified)
        return datetime.now() - verified_dt > timedelta(days=STALENESS_DAYS)
    except (ValueError, TypeError):
        return True


def inject_context(
    user_id: str = DEFAULT_USER_ID,
    bank_id: str = DEFAULT_BANK_ID,
) -> str:
    """生成指定 bank 的注入上下文（超期 block 自动标注）。"""
    blocks = get_all_blocks(user_id=user_id, bank_id=bank_id)
    if not blocks:
        return ""

    lines = ["[CoreMemory · Hyperion]"]
    stale_count = 0
    for key, label in BLOCK_KEYS.items():
        b = blocks.get(key)
        if b and b["content"].strip():
            stale = _is_stale(b.get("last_verified_at", ""))
            if stale:
                stale_count += 1
                lines.append(f"{label}: {b['content']} [⚠️ {STALENESS_DAYS}天+未验证]")
            else:
                lines.append(f"{label}: {b['content']}")
    if stale_count:
        lines.append(f"⚠️ {stale_count} 个 block 超过 {STALENESS_DAYS} 天未验证，建议通过 API 更新或 verify")
    return "\n".join(lines) if len(lines) > 1 else ""


if __name__ == "__main__":
    init_core_memory()
    print("=== 全部 block ===")
    print(json.dumps(get_all_blocks(), ensure_ascii=False, indent=2))
    print("\n=== 注入上下文 ===")
    print(inject_context())
