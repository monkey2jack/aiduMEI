"""
aiduMEM CoreMemory — LLM 可编辑结构化记忆块
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
v11 Hyperion · 三大块：user_profile / current_project / key_decisions
LLM 通过 API 自行维护，每轮注入到 Hermes 上下文

v11.1 Opus 升级：30天验证失效机制，超期 block 注入时标注 [⚠️ 需验证]
"""
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta

from ducky.utils import DEFAULT_USER_ID, get_facts_conn
from ducky.bank_contract import (
    DEFAULT_BANK_ID,
    LEGACY_PLACEHOLDER_USER_ID,
    ensure_bank_registered,
    ensure_memory_banks_schema,
    is_legacy_schema_error,
    make_scope,
    raw_storage_key,
    scoped_storage_key,
    visible_user_clause,
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
STALENESS_DAYS = 30  # 全局兜底（未知块 / 未分级配置），与 v20.0 逐字节一致

# v20.1 WP-D2：陈旧阈值按块分级。三块共用一个 30 天常数是 v20.0.1 登记的
# 告警疲劳源：用户画像和关键决策本来就是低频稳定信息，每 30 天例行告警一次，
# 告警很快就没人看了 —— 而真正易过期的「当前项目」（审计实锤：写着三个版本
# 以前的状态）淹没在同一种叫声里。
#
# 分级默认值的依据是联邦分层的既有 TTL 语义（ARCHITECTURE.md 分层生命周期：
# semantic 180 天 / episodic 30 天）：画像与决策 ≈ semantic 档，当前项目 ≈
# episodic 档。**分级是给依据，不是调大消音**：最易过期的 current_project
# 保持 30 天，一分没放松。
STALENESS_DAYS_BY_BLOCK = {
    "core_user_profile": 180,      # 稳定身份信息 ≈ semantic 档
    "core_current_project": 30,    # 高频易变 ≈ episodic 档，保持原阈值
    "core_key_decisions": 180,     # 决策沉淀 ≈ semantic 档
}

#: 部署侧覆盖：每块 > 全局 > 分级默认 > 全局兜底。生效值经
#: staleness_status() / /health 可查 —— 配置写了不等于生效。
_STALENESS_ENV_GLOBAL = "AIDUMEI_CORE_STALENESS_DAYS"
_STALENESS_ENV_PREFIX = "AIDUMEI_CORE_STALENESS_DAYS_"


def staleness_threshold_days(block_key: str) -> int:
    """块的生效陈旧阈值。

    显式配置非法时打 warning 后落到下一档 —— 「设了打错的值」和「没设」
    行为一样、意图完全不同（铁律 13），必须出声但不许让探针崩掉。
    """
    for raw, which in (
        (os.environ.get(_STALENESS_ENV_PREFIX + block_key.upper()), "每块"),
        (os.environ.get(_STALENESS_ENV_GLOBAL), "全局"),
    ):
        if raw is None:
            continue
        try:
            v = int(raw)
            if v <= 0:
                raise ValueError("必须为正整数")
            return v
        except (ValueError, TypeError):
            # v20.1 整改轮（R-15 · 外审 y P2）：点名到块 —— 排错时才知道
            # 是哪个块的哪档配置写坏了。
            logger.warning("核心记忆%s陈旧阈值配置无效: %r（需正整数，block=%s），忽略此档",
                           which, raw, block_key)
    return STALENESS_DAYS_BY_BLOCK.get(block_key, STALENESS_DAYS)

_init_lock = threading.Lock()
_initialized = False
_initialized_scopes: set[tuple[str, str]] = set()


def _get_conn():
    """复用 utils 的线程本地连接"""
    return get_facts_conn()


# ── 读侧可见域 & 三元组主键（甲1b；替代 丙9 的存量数据对账） ────────────────
#
# 「占位符」有两种拼写：迁移 DDL 写下的字面量 'default'，和部署方配的
# AIDUMEM_DEFAULT_USER_ID。默认部署里两者相等，裂缝隐形；一旦部署方改名，
# 存量行（scope 列是 ALTER TABLE … DEFAULT 'default' 一次性写满的，全部停在
# 字面量上）就整体掉出读侧射程 —— CoreMemory 对这台机器**整块失效**，而
# 日志上一个字都没有，/health 还是绿的。
#
# bank_contract.visible_user_clause 就是为这条裂缝准备的既有解法，
# memory_types / facts_recall / routes_p1 都已经接上，core_memory 是唯一漏接的
# 模块。它**只放宽读**：改过名的默认身份 → ['<新名>','default']；具名租户
# alice → ['alice']（看不到别人的行，也丢不掉自己的行）；老部署 → ['default']
# 逐字节不变。删除/更新一律保持精确匹配 —— 放宽读是让用户看见的变多，放宽删
# 是让用户的数据变少。
#
# 于是 丙9 那套「存量行归属靠先到先得猜一个」的数据对账**不需要做**：
# 一行数据都不动，改过名的默认身份就能重新看见自己那三块。


def _core_memory_ddl(table: str, *, if_not_exists: bool = False) -> str:
    """core_memory 建表 DDL。

    🔴甲1b（v20.0）：主键是 ``(user_id, bank_id, block_key_raw)`` 三元组。

    v19 是单列 ``block_key TEXT PRIMARY KEY``。默认域对**任何**租户都保留 v19
    裸键（硬契约，见 put_block 里的长注释），于是两个域写同一块会**撞进同一
    行**：后来者盖掉先来者，或者 ``INSERT OR IGNORE`` 直接静默拒绝、一个字都
    不吭。三元组主键让每个 ``(租户, bank)`` 各自拥有自己的一行，而裸键的形状
    **一个字节不变**。
    """
    guard = "IF NOT EXISTS " if if_not_exists else ""
    return f"""
        CREATE TABLE {guard}{table} (
            block_key        TEXT NOT NULL,
            content          TEXT NOT NULL,
            updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            user_id          TEXT NOT NULL DEFAULT 'default',
            bank_id          TEXT NOT NULL DEFAULT 'default',
            block_key_raw    TEXT NOT NULL,
            PRIMARY KEY (user_id, bank_id, block_key_raw)
        )
    """


def _visible_where(scope) -> tuple[str, list]:
    """返回 ``(sql, params)``：本调用方在这个 bank 下**能读到**哪些行。

    **只给 SELECT 用。** UPDATE/DELETE 必须走精确归属，理由见上方长注释。
    """
    clause, owners = visible_user_clause(scope.user_id)
    return f"{clause} AND bank_id=?", [*owners, scope.bank_id]


def _owner_first_order(alias: str = "") -> str:
    """精确归属优先的排序片段 —— 尾部多吃一个 ``?``（本调用方的 user_id）。

    可见集合可能同时命中「自己的行」和「存量占位符行」。SQLite 不保证任何行
    序，不排序就等于两次读可能给出不同内容 —— 那是最难查的一类 bug。规则：
    自己的行永远排第一，其余按 user_id 稳定排。本仓此前没有现成惯例
    （``grep "ORDER BY CASE"`` 无命中），这里定一条。
    """
    col = f"{alias}.user_id" if alias else "user_id"
    return f"ORDER BY CASE WHEN {col}=? THEN 0 ELSE 1 END, {col}"


def _visible_raw_keys(conn, scope) -> set:
    """这个域现在**实际能读到**的裸键集合（含存量占位符行）。"""
    where, params = _visible_where(scope)
    rows = conn.execute(
        f"SELECT block_key, block_key_raw FROM core_memory WHERE {where}",
        params,
    ).fetchall()
    return {(r["block_key_raw"] or raw_storage_key(r["block_key"], scope)) for r in rows}


def _migrate_pk_to_scope_triple(conn) -> None:
    """把 v19 的单列主键 ``block_key`` 重建成 ``(user_id, bank_id, block_key_raw)``。

    SQLite 改不了主键，只能整表重建（建新表 → 拷数据 → 删旧表 → 改名）。
    主键已经是三元组时直接返回，可反复调用。

    三条纪律：
      ① 拷贝用**裸 INSERT**，不用 ``INSERT OR IGNORE`` —— 静默丢行正是本轮要治
         的病，重建过程自己再丢一次就荒唐了。冲突必须炸出来。
      ② 前后**数行数**，不等就抛；异常一律 rollback 后重抛，绝不吞。上游
         ``_ensure_table`` 也是重抛，失败会直接打到启动日志上。宁可起不来，
         也不要「部署后冒烟是绿的」（甲4 的教训）。这也是为什么重建放在这里、
         而不是放进 ``schema_bootstrap.apply_migrations`` —— 那个函数整体裹在
         ``except Exception: logger.error("…服务继续启动…")`` 里，一次失败的
         表重建会被降级成一行 error 日志，然后服务照常起来、记忆照常空。
      ③ 自己显式开事务：连接默认 ``isolation_level`` 只在 DML 前隐式 BEGIN，
         DDL 走 autocommit —— 不显式开事务的话，「删旧表」和「改名」之间崩一下
         就回不去了。SQLite 的 DDL 本身是事务性的，包得住。
    """
    pk_cols = {r["name"] for r in conn.execute("PRAGMA table_info(core_memory)") if r["pk"]}
    if pk_cols == {"user_id", "bank_id", "block_key_raw"}:
        return
    before = conn.execute("SELECT COUNT(*) FROM core_memory").fetchone()[0]
    # 拷贝之前自己先查一遍撞键。裸 INSERT 撞上主键会抛 IntegrityError，而它的报文
    # 只说一句「UNIQUE constraint failed」，不告诉你是哪一块、属于谁、有几行。这个
    # 函数要在别人的生产库上跑，起不来是可以接受的，起不来又说不清为什么不行。
    dups = conn.execute(
        "SELECT COALESCE(NULLIF(TRIM(user_id), ''), ?) AS u, "
        "COALESCE(NULLIF(TRIM(bank_id), ''), ?) AS b, "
        "COALESCE(NULLIF(TRIM(block_key_raw), ''), block_key) AS k, COUNT(*) AS n "
        "FROM core_memory GROUP BY u, b, k HAVING n > 1",
        (LEGACY_PLACEHOLDER_USER_ID, DEFAULT_BANK_ID),
    ).fetchall()
    if dups:
        detail = "；".join(
            f"user_id={r['u']} bank_id={r['b']} block={r['k']} 共 {r['n']} 行" for r in dups
        )
        raise RuntimeError(
            "CoreMemory 主键重建无法进行：同一 (user_id, bank_id, block_key_raw) "
            f"下存在多行 —— {detail}。保留哪一份必须由人来定：自动合并等于替用户"
            "扔掉一份他自己写的内容，本函数拒绝猜。"
        )
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DROP TABLE IF EXISTS core_memory__scoped")
        conn.execute(_core_memory_ddl("core_memory__scoped"))
        conn.execute(
            "INSERT INTO core_memory__scoped "
            "(block_key, content, updated_at, last_verified_at, user_id, bank_id, block_key_raw) "
            "SELECT block_key, content, updated_at, last_verified_at, "
            "COALESCE(NULLIF(TRIM(user_id), ''), ?), "
            "COALESCE(NULLIF(TRIM(bank_id), ''), ?), "
            "COALESCE(NULLIF(TRIM(block_key_raw), ''), block_key) "
            "FROM core_memory",
            (LEGACY_PLACEHOLDER_USER_ID, DEFAULT_BANK_ID),
        )
        after = conn.execute("SELECT COUNT(*) FROM core_memory__scoped").fetchone()[0]
        if after != before:
            raise RuntimeError(
                f"CoreMemory 主键重建丢行：迁移前 {before} 行，迁移后 {after} 行"
            )
        conn.execute("DROP TABLE core_memory")
        conn.execute("ALTER TABLE core_memory__scoped RENAME TO core_memory")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    logger.info(
        "CoreMemory: 主键已重建为 (user_id, bank_id, block_key_raw)，%d 行原样迁移（甲1b）",
        before,
    )


def _ensure_table():
    """确保 core_memory 表存在，并自动迁移新增列"""
    conn = _get_conn()
    try:
        ensure_memory_banks_schema(conn)
        conn.execute(_core_memory_ddl("core_memory", if_not_exists=True))
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
        # v20.0 additive scope migration.  block_key holds the deterministic
        # scoped storage key for named banks (the bare v19 key in the default
        # bank); block_key_raw remains the public API key and, together with
        # (user_id, bank_id), is the real primary key as of 甲1b.
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
        # 主键重建必须排在这里：回填之后（拷贝依赖 block_key_raw 非空），建索引
        # 之前（DROP TABLE 会把旧表上的索引一起带走，之后两句 CREATE INDEX
        # IF NOT EXISTS 正好重建到新表上）。
        _migrate_pk_to_scope_triple(conn)
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
        # 🔴缺陷#13 第四处：本函数里那两句 UPDATE 也会隐式开事务，
        # 建表/补列半路失败同样会把线程本地连接卡住。理由见 _seed_defaults 处长注释。
        try:
            conn.rollback()
        except Exception:
            pass
        logger.error(f"CoreMemory ensure table error: {e}")
        raise
    finally:
        conn.close()


def _seed_defaults(scope=None) -> dict:
    """首次运行时写入指定 bank 的默认值。

    返回 ``{"readable": int, "blocked": list[str]}`` —— 播种完成后这个域**实际
    能读到几块**（含通过 :func:`_visible_where` 看见的存量行），以及有哪几块
    到最后还是读不到。

    🔴甲16（v20.0）：这里原来是一句光秃秃的 ``INSERT OR IGNORE``，而
    ``INSERT OR IGNORE`` 撞上冲突时**不报错、不写入、也不吭声**。默认域对任何
    租户都用 v19 裸键（硬契约，见 put_block 里 甲1a 的长注释），于是：v19 存量
    行被增量迁移打上 ``user_id='default'`` 的戳 → 运行时身份是个非 default 的
    值 → 播种时裸键已被 default 那三行占着 → 三次 IGNORE、一行没建 →
    ``get_all_blocks`` 查这个域得 0 行 → ``inject_context()`` 返回 ``""``。

    **整个 CoreMemory 静默失效，日志上一个字都没有。** 这就是「静默拒绝」比
    「静默覆盖」更难发现的地方：覆盖至少留下了一行被改坏的数据，拒绝什么都不留，
    而调用方拿到的是「初始化完成」。

    v20.0pre 起两处都焊掉了：键形状换成三元组主键（甲1b，见
    :func:`_core_memory_ddl`），存量行靠读侧放宽重新可见（见
    :func:`_visible_where`）—— **一行数据都没动**，所以 丙9 那套「归属靠先到
    先得猜一个」的数据对账不必做。这两件事各自都对，凑在一起会打起来，所以
    下面那句 INSERT 前面多了一道护栏，理由写在护栏边上。
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
        # 🔴甲1b 反噬防线 —— 这道护栏不是保险，是必需品：
        # 换成三元组主键之后，INSERT OR IGNORE 再也不会被拒绝了。于是「改过名的
        # 默认身份」一播种，就会给自己插进三条全新的「（尚未填写）」占位块；而它们
        # 按「精确归属优先」的排序**赢过**存量的真实内容 —— 用户打开一看，记忆全空。
        # 修好读、又用播种把它盖回去，比原来那个 bug 更难查。
        # 所以播种前先问一句：这一块我现在**已经能读到**了吗？能读到就不播。
        # 播种是为了「从无到有」，不是为了盖掉已有的东西。
        visible = _visible_raw_keys(conn, scope)
        for key, content in DEFAULT_BLOCKS.items():
            if key in visible:
                continue
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
        # 播种之后回查：这个域现在究竟能读到几块。口径必须和 get_all_blocks 完全
        # 一致（同一个 _visible_raw_keys），否则「报告 3/3、实际读到 0」这种假绿灯
        # 又会回来 —— 观察代码不许自己另立一套口径。
        readable_keys = _visible_raw_keys(conn, scope)
        readable = sum(1 for key in DEFAULT_BLOCKS if key in readable_keys)
        blocked = [key for key in DEFAULT_BLOCKS if key not in readable_keys]
        if blocked:
            logger.warning(
                "CoreMemory 播种后仍有 %d/%d 块读不到：user_id=%s bank_id=%s —— "
                "这个域的 inject_context() 会少掉这几块。受影响 block：%s。"
                "三元组主键（甲1b）之后这里本该恒为空，非空说明写入真的失败了，"
                "不再是「裸键被别的域占着」那种静默拒绝（甲16）",
                len(blocked), len(DEFAULT_BLOCKS), scope.user_id, scope.bank_id,
                ",".join(blocked),
            )
    except Exception as e:
        # 🔴缺陷#13（v20.0pre 负向对照时发现的**新**缺陷，不在审计清单里）：
        # 这一句 rollback 看着多余，其实是必需品，别当成噪音删掉。
        #   · sqlite3 默认 isolation_level 会在 DML 前隐式 BEGIN；
        #   · 上面那句写失败时，事务是**开着**的；
        #   · finally 里的 conn.close() 是 no-op —— 见 utils._ConnProxy
        #     「close() 变 no-op，防止线程本地连接被意外关闭」，
        #     这条线程本地连接会活到进程结束。
        # 三条凑齐 = 一次失败的写入把这条连接永久卡在开着的写事务里，
        # 之后这个库上每一个写入方都要先等满锁超时、再收 database is locked。
        # 症状是「CoreMemory 从此写不进去了」，而日志里只有第一次那条报错。
        try:
            conn.rollback()
        except Exception:
            pass    # 回滚本身失败没关系，原始异常比它重要，绝不能被它顶掉
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
        where, params = _visible_where(scope)
        rows = conn.execute(
            "SELECT block_key, block_key_raw, content, updated_at, last_verified_at "
            f"FROM core_memory WHERE {where} {_owner_first_order()}",
            (*params, scope.user_id),
        ).fetchall()
        blocks: dict = {}
        for r in rows:
            raw = r["block_key_raw"] or raw_storage_key(r["block_key"], scope)
            if raw in blocks:
                continue  # 自己的行已排在前面，后面的存量影子行不许覆盖它
            blocks[raw] = {
                "content": r["content"],
                "updated_at": r["updated_at"],
                "last_verified_at": r["last_verified_at"] or r["updated_at"],
            }
        return blocks
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
        where, params = _visible_where(scope)
        row = conn.execute(
            "SELECT block_key, block_key_raw, content, updated_at, last_verified_at "
            f"FROM core_memory WHERE {where} AND (block_key_raw=? OR block_key=?) "
            f"{_owner_first_order()} LIMIT 1",
            (*params, block_key, scoped_storage_key(block_key, scope), scope.user_id),
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
        # 🔴甲1a（v20.0pre）：键的形状**一个字节不动**，隔离改由主键承担。
        #
        # 默认域对**任何**租户都保留 v19 裸键，这不是疏漏而是硬契约，见
        # tests/test_v20_bank_scope_core.py::
        #   test_default_bank_keeps_the_v19_key_shape_for_every_tenant
        # —— 那条用例是从一次真实的「非默认租户的 v19 存量行整体失踪」里长出来
        # 的：写进去带前缀、读出来带前缀，自己跟自己对得上，测试全绿，而升级前
        # 就存在的裸键行从此再也没人去读。
        #
        # v19 为这份兼容付的代价是单列主键 block_key 比
        # (user_id, bank_id, block_key_raw) 粗一档：默认域里两个域写同一个裸键会
        # 撞进同一行 —— 后来者盖掉先来者，或者 INSERT OR IGNORE 一声不吭地拒绝。
        # 甲1b 把主键换成了三元组（见 _core_memory_ddl / _migrate_pk_to_scope_triple），
        # 于是：
        #   ① 每个 (租户, bank) 各自拥有自己的一行，而裸键的形状一个字节没变；
        #   ② ON CONFLICT 落在三元组上，upsert 永远只改自己那一行 —— 「一次内容
        #      更新把这一行搬到另一个域去」这条病根从此不存在；
        #   ③ 写入一律精确归属。存量占位符行靠**读侧**放宽被看见（见
        #      _visible_where），不靠写侧去猜、去搬、去合并。
        #
        # 副作用要说在明面上：调用方显式写下的内容，会盖过它此前透过放宽读看见的
        # 那份存量影子行。这是对的 —— 用户亲手写的那一版就该赢；影子行原地留着，
        # 一个字节没动，读侧只是不再优先它。
        #
        # v19 时代这里还有一句「按裸键查现任归属、撞了就 warning」的探针。三元组
        # 主键之后写入永远落在调用方自己那一行上，跨域撞键这个现象**不存在了**，
        # 探针恒不触发 —— 留着就是一段会撒谎的死代码，已删。
        conn.execute(
            "INSERT INTO core_memory "
            "(block_key, content, updated_at, last_verified_at, user_id, bank_id, block_key_raw) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, bank_id, block_key_raw) DO UPDATE SET content=excluded.content, "
            "updated_at=excluded.updated_at, last_verified_at=excluded.last_verified_at, "
            "block_key_raw=excluded.block_key_raw",
            (
                storage_key, content, now, now,
                scope.user_id, scope.bank_id, block_key,
            )
        )
        conn.commit()
    except Exception as e:
        # 🔴缺陷#13（v20.0pre 负向对照时发现的**新**缺陷，不在审计清单里）：
        # 这一句 rollback 看着多余，其实是必需品，别当成噪音删掉。
        #   · sqlite3 默认 isolation_level 会在 DML 前隐式 BEGIN；
        #   · 上面那句写失败时，事务是**开着**的；
        #   · finally 里的 conn.close() 是 no-op —— 见 utils._ConnProxy
        #     「close() 变 no-op，防止线程本地连接被意外关闭」，
        #     这条线程本地连接会活到进程结束。
        # 三条凑齐 = 一次失败的写入把这条连接永久卡在开着的写事务里，
        # 之后这个库上每一个写入方都要先等满锁超时、再收 database is locked。
        # 症状是「CoreMemory 从此写不进去了」，而日志里只有第一次那条报错。
        try:
            conn.rollback()
        except Exception:
            pass    # 回滚本身失败没关系，原始异常比它重要，绝不能被它顶掉
        logger.error(f"CoreMemory put_block error for {block_key}: {e}")
        raise
    finally:
        conn.close()

    # v20 P0-6：写完就进检索索引。
    #
    # 用户视角审计 🟡-3 实测：核心记忆块**永远搜不到**。全文 0 处向量／嵌入调用，
    # 它们只经由 `inject_context()` 直接拼进上下文。于是 `/search` 和核心记忆是
    # 两套互不相通的系统 —— 用户问什么能不能得到答案，取决于他问的那句话恰好
    # 走了哪一条路。而这两条路的边界，用户无从得知。
    #
    # 失败不阻断写入：核心记忆的**正本**在 core_memory 表里，索引只是副本。
    # 索引挂了要能看见（走 P1-8 的账本），但不能因此让一次正常的写入失败。
    _index_core_block(block_key, content, owner_user, owner_bank)
    # v20.1 WP-D1：同一份内容进向量索引。FTS 归 FTS、向量归向量，
    # 两腿各自失败各自记账，互不拖累。verify_block 只刷时间戳不动内容，
    # 无需重嵌 —— 只有内容真变了（这里）才花一次嵌入调用。
    _vector_index_core_block(block_key, content, owner_user, owner_bank)

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
        # 定位用放宽后的可见集合（否则用户明明读得到、却「验证不到」），落笔用
        # rowid 精确到那一行 —— 放宽读，绝不放宽写。
        where, params = _visible_where(scope)
        target = conn.execute(
            f"SELECT rowid FROM core_memory WHERE {where} "
            f"AND (block_key_raw=? OR block_key=?) {_owner_first_order()} LIMIT 1",
            (*params, block_key, scoped_storage_key(block_key, scope), scope.user_id),
        ).fetchone()
        if target is not None:
            conn.execute(
                "UPDATE core_memory SET last_verified_at = ? WHERE rowid = ?",
                (now, target["rowid"]),
            )
        conn.commit()
        if target is None:
            return {"block_key": block_key, "user_id": scope.user_id, "bank_id": scope.bank_id, "status": "not_found"}
    except Exception as e:
        # 🔴缺陷#13（v20.0pre 负向对照时发现的**新**缺陷，不在审计清单里）：
        # 这一句 rollback 看着多余，其实是必需品，别当成噪音删掉。
        #   · sqlite3 默认 isolation_level 会在 DML 前隐式 BEGIN；
        #   · 上面那句写失败时，事务是**开着**的；
        #   · finally 里的 conn.close() 是 no-op —— 见 utils._ConnProxy
        #     「close() 变 no-op，防止线程本地连接被意外关闭」，
        #     这条线程本地连接会活到进程结束。
        # 三条凑齐 = 一次失败的写入把这条连接永久卡在开着的写事务里，
        # 之后这个库上每一个写入方都要先等满锁超时、再收 database is locked。
        # 症状是「CoreMemory 从此写不进去了」，而日志里只有第一次那条报错。
        try:
            conn.rollback()
        except Exception:
            pass    # 回滚本身失败没关系，原始异常比它重要，绝不能被它顶掉
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


def _is_stale(last_verified: str, block_key: str | None = None) -> bool:
    """判断 block 是否超过其生效阈值未验证。

    v20.1 WP-D2：带 block_key 走分级阈值；不带（老调用方）退回全局兜底
    STALENESS_DAYS —— 签名向后兼容，判据升级。
    """
    threshold = staleness_threshold_days(block_key) if block_key else STALENESS_DAYS
    if not last_verified:
        return True
    try:
        verified_dt = datetime.fromisoformat(last_verified)
        return datetime.now() - verified_dt > timedelta(days=threshold)
    except (ValueError, TypeError):
        return True


#: 核心记忆在检索索引里的分类标记。用它可以把这些块单独捞出来／单独降权，
#: 也让 `/search` 的结果能自证「这条来自核心记忆」而不是普通记忆。
CORE_INDEX_CATEGORY = "core_memory"


def core_index_id(block_key: str) -> str:
    """核心记忆块在索引里的稳定主键。

    用固定前缀 + block_key，而不是随机 id：核心记忆是**可覆盖**的，同一个 block
    改十次也只该在索引里占一条。随机 id 会让索引里堆十条内容各异的同名块，
    召回时随机命中其中一条旧的 —— 那比搜不到更糟。
    """
    return f"core::{block_key}"


def _index_core_block(block_key: str, content: str, user_id: str, bank_id: str) -> bool:
    """把一块核心记忆写进检索索引；成功返回 True。

    v20 P0-6。**失败不抛** —— 正本在 core_memory 表里，索引只是副本；
    但失败要留痕（走 P1-8 的特性账本），否则又是一次「绿灯亮着、活没干」。
    """
    try:
        from ducky.text_fts import _index_memory
        _index_memory(core_index_id(block_key), content,
                      user_id=user_id, category=CORE_INDEX_CATEGORY, bank_id=bank_id)
        return True
    except Exception as exc:
        try:
            from ducky.failure_ledger import feature_failed
            feature_failed("core_memory_index", exc)
        except Exception:
            pass
        logger.debug("核心记忆索引写入跳过 %s: %s", block_key, exc)
        return False


# ── 向量索引（v20.1 WP-D1）──────────────────────────────────────────
# P0-6 把核心记忆接进了 FTS，但向量召回池里仍然没有它 —— 语义近义的问法
# （不含 block 原文关键词）依旧召不回。这里把同一份内容写进 mem0 的向量库，
# payload 按 mem0 的结果装配契约铺（data 必填、user_id 提升、其余进 metadata），
# 于是读侧零改动：bank 复筛认 metadata.bank_id，打分器的 reliability 维度认
# metadata.reliability —— 核心记忆天然吃满可靠性权重。

#: 开关（env 显式，默认开）。关闭 = WP-D1 的回滚方式：新写不再进向量池。
_CORE_VECTOR_ENV = "AIDUMEI_CORE_VECTOR_INDEX"


def is_core_vector_index_enabled() -> bool:
    """无效值报错点名（由 _vector_index_core_block 捕获并记账），不静默回退。"""
    raw = os.environ.get(_CORE_VECTOR_ENV)
    if raw is None:
        return True
    v = raw.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ValueError(
        f"{_CORE_VECTOR_ENV} 值无效: {raw!r}（接受 1/0/true/false/yes/no/on/off）"
    )


def core_vector_point_id(block_key: str, user_id: str, bank_id: str) -> str:
    """核心记忆块在向量库里的稳定点位 id。

    与 FTS 侧 `core_index_id` 同一纪律：确定性 id，同一块改十次只占一个点
    （Qdrant 同 id upsert 覆盖）。随机 id 会让向量库堆十个内容各异的同名块，
    召回时随机命中一条旧的 —— 比搜不到更糟。id 带全三元组：不同域的同名块
    各占各的点，互不覆盖。
    """
    import uuid
    return str(uuid.uuid5(uuid.NAMESPACE_URL,
                          f"aidumei:core::{block_key}::{user_id}::{bank_id}"))


def _vector_index_core_block(block_key: str, content: str,
                             user_id: str, bank_id: str) -> bool:
    """把一块核心记忆写进向量索引；成功返回 True。

    失败不抛 —— 正本在 core_memory 表、FTS 是第二副本、向量是第三副本，
    任何一个副本失败都不许拖垮写入，但都要留痕（特性账本，与 FTS 腿分开
    记名：core_memory_vector_index —— 两条腿坏法不同，修法也不同）。
    """
    try:
        if not is_core_vector_index_enabled():
            return False
        import hashlib
        from ducky.mem0_runtime import get_memory
        mem = get_memory()
        vec = mem.embedding_model.embed(content, "add")
        now = datetime.now().isoformat()
        # payload 契约对齐 mem0 结果装配：data → 结果的 memory 字段（缺了整条
        # 被丢）；user_id 被提升并参与过滤；其余键落 metadata —— bank_id 供
        # 域复筛，reliability 供打分器，memory_class/core_block_key 供溯源。
        payload = {
            "data": content,
            "hash": hashlib.md5(content.encode("utf-8")).hexdigest(),
            "created_at": now,
            "updated_at": now,
            "user_id": user_id,
            "bank_id": bank_id,
            "category": CORE_INDEX_CATEGORY,
            "memory_class": "core",
            "core_block_key": block_key,
            "reliability": 1.0,
            "recorded_at": now,
        }
        _pid = core_vector_point_id(block_key, user_id, bank_id)
        mem.vector_store.insert(
            vectors=[vec],
            payloads=[payload],
            ids=[_pid],
        )
        # v20.2 自动挡：核心块同 id 补本地副本（软失败进欠账不拖垮云腿）——
        # 降挡时核心记忆照样可被语义召回。
        try:
            from ducky.dual_index import upsert_local
            upsert_local(_pid, content, payload)
        except Exception as _le:
            logger.debug("核心块本地副本跳过 %s: %s", block_key, _le)
        return True
    except Exception as exc:
        try:
            from ducky.failure_ledger import feature_failed
            feature_failed("core_memory_vector_index", exc)
        except Exception:
            pass
        logger.debug("核心记忆向量索引跳过 %s: %s", block_key, exc)
        return False


def audit_core_replicas(user_id: str = DEFAULT_USER_ID,
                        bank_id: str = DEFAULT_BANK_ID) -> dict:
    """三副本对账（v20.1 整改轮 R-16 · 外审 w P1-② 机制 + y 覆盖度建议）。

    以**正本**（core_memory 表）为基准，逐块核对 FTS 第二副本与向量第三
    副本是否在场。三腿写入都是「失败不抛、只记账」的软失败设计 —— 没有
    对账，缺腿的块从此静默检索不到而绿灯依旧。

    纪律两条：
      · **占位块必须排除**——占位文本不进索引是设计行为（backfill 同款
        判据），把它算成缺腿会制造新的告警疲劳（外审 w 的例证正是栽在
        这里：把 default 占位影子行当成了缺腿块）；
      · 只观测不自愈——自动重索引留给显式回填工具（dry-run/apply 纪律），
        巡检的职责是让缺腿可见，不是悄悄改状态。

    返回 {"checked": n, "gaps": [{block, fts, vector}], "vector_checked": bool}。
    向量腿核对需 mem0 就绪；不可用时 vector_checked=False 且不冒充齐全。
    """
    scope = make_scope(user_id, bank_id)
    placeholders = {(v or "").strip() for v in DEFAULT_BLOCKS.values()}
    # 对账按**精确归属**取行，不走读侧放宽：影子行（他域存量经放宽被
    # 看见）的索引归属其真实 owner 域，按本域核对必然「缺」——那是假缺，
    # 外审 w 的例证正是栽在这里。
    _ensure_table()
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT block_key_raw, content FROM core_memory "
            "WHERE user_id=? AND bank_id=?",
            (scope.user_id, scope.bank_id),
        ).fetchall()
    finally:
        conn.close()

    mem = None
    vector_checked = True
    try:
        from ducky.mem0_runtime import get_memory
        mem = get_memory()
    except Exception:
        vector_checked = False

    checked = 0
    gaps: list[dict] = []
    for row in rows:
        key = row["block_key_raw"]
        body = (row["content"] or "").strip()
        if not key or not body or body in placeholders:
            continue
        checked += 1
        fts_ok = False
        try:
            from ducky.utils import get_text_conn
            tconn = get_text_conn()
            row = tconn.execute(
                "SELECT 1 FROM memories WHERE id=? AND user_id=? AND bank_id=?",
                (core_index_id(key), scope.user_id, scope.bank_id),
            ).fetchone()
            fts_ok = row is not None
        except Exception:
            fts_ok = False
        vec_ok = None
        if vector_checked and mem is not None:
            try:
                point = mem.vector_store.get(
                    core_vector_point_id(key, scope.user_id, scope.bank_id))
                vec_ok = point is not None
            except Exception:
                vec_ok = None
                vector_checked = False
        # v20.2 验收门槛 2：对账巡检覆盖**本地腿**（自动挡第四副本）。
        # None = 本地库尚未建（备胎未启用/未回填），不算缺腿——缺的定义
        # 是「库在而块不在」；False 才进 gaps。
        local_ok = None
        if mem is not None:
            try:
                from ducky.dual_index import LOCAL_COLLECTION
                client = mem.vector_store.client
                if LOCAL_COLLECTION in {c.name for c in client.get_collections().collections}:
                    got = client.retrieve(LOCAL_COLLECTION, ids=[
                        core_vector_point_id(key, scope.user_id, scope.bank_id)])
                    local_ok = bool(got)
            except Exception:
                local_ok = None
        if not fts_ok or vec_ok is False or local_ok is False:
            gaps.append({"block": key, "fts": fts_ok, "vector": vec_ok,
                         "local_vector": local_ok})
    return {"checked": checked, "gaps": gaps, "vector_checked": vector_checked}


def backfill_core_vectors(user_id: str | None = None,
                          bank_id: str | None = None,
                          apply: bool = False) -> dict:
    """把**存量**核心记忆块补进向量索引。默认 dry-run，`apply=True` 才写。

    ⚠️ **这个函数不会被自动调用，生产执行须单独停点获维护者批准**（v20.0.1
    审计登记原文：存量回填会改变生产可见边界，属数据决策不是代码决策）。
    与 FTS 侧 `backfill_core_index` 同一纪律，另加 dry-run 档：先报会动什么，
    再决定动不动。占位文本一律跳过 —— 语义召回一条「（尚未填写）」毫无价值。
    """
    conn = _get_conn()
    try:
        if user_id is not None or bank_id is not None:
            scopes = [(user_id or DEFAULT_USER_ID, bank_id or DEFAULT_BANK_ID)]
        else:
            scopes = [(r["user_id"], r["bank_id"]) for r in conn.execute(
                "SELECT DISTINCT user_id, bank_id FROM core_memory").fetchall()]
    finally:
        conn.close()

    placeholders = {(v or "").strip() for v in DEFAULT_BLOCKS.values()}
    would, skipped, failed = [], [], []
    for uid, bid in scopes:
        blocks = get_all_blocks(user_id=uid, bank_id=bid)
        for key, b in blocks.items():
            body = (b.get("content") or "").strip()
            tag = f"{uid}/{bid}/{key}"
            if not body or body in placeholders:
                skipped.append(tag)
                continue
            if not apply:
                would.append(tag)
                continue
            (would if _vector_index_core_block(key, body, uid, bid)
             else failed).append(tag)
    logger.info("核心记忆向量回填（apply=%s）：%s %d，跳过 %d，失败 %d",
                apply, "已写入" if apply else "将写入", len(would),
                len(skipped), len(failed))
    return {"apply": apply, "scopes": len(scopes),
            ("indexed" if apply else "would_index"): would,
            "skipped": skipped, "failed": failed}


def backfill_core_index(user_id: str = DEFAULT_USER_ID,
                        bank_id: str = DEFAULT_BANK_ID) -> dict:
    """把**存量**核心记忆块补进索引。

    ⚠️ **这个函数不会被自动调用。** P0-6 落地时部署方明确要求：机制先上，存量
    回填等单独点头再做 —— 因为回填等于把已有的核心记忆内容写进检索索引，
    那是一次改变「什么东西可被搜到」边界的动作，属于数据决策，不是代码决策。
    所以它只在被显式调用时才动，且返回逐块结果供调用方核对。
    """
    blocks = get_all_blocks(user_id=user_id, bank_id=bank_id)
    done, skipped, failed = [], [], []
    placeholders = {(v or "").strip() for v in DEFAULT_BLOCKS.values()}
    for key, b in blocks.items():
        body = (b.get("content") or "").strip()
        if not body or body in placeholders:
            skipped.append(key)          # 占位文本不进索引：搜到「（尚未填写）」毫无价值
            continue
        (done if _index_core_block(key, body, user_id, bank_id) else failed).append(key)
    logger.info("核心记忆回填索引：成功 %d，跳过（占位/空）%d，失败 %d",
                len(done), len(skipped), len(failed))
    return {"indexed": done, "skipped": skipped, "failed": failed}


def _age_days(ts: str) -> float | None:
    """距今多少天；解析不了返回 None（而不是 0 —— 0 会被读成「刚更新过」）。"""
    if not ts:
        return None
    try:
        return (datetime.now() - datetime.fromisoformat(ts)).total_seconds() / 86400.0
    except (ValueError, TypeError):
        return None


def staleness_status(
    user_id: str = DEFAULT_USER_ID,
    bank_id: str = DEFAULT_BANK_ID,
) -> dict:
    """核心记忆的陈旧度 —— 供 `/health` 使用（v20 P0-4）。

    用户视角审计 🔴-2：三块核心记忆的 `updated_at` 全部停在同一天，约 28 天没动过，
    而 `core_current_project` 里写的还是三个版本以前的项目状态。用户问「我们现在在做
    什么」，拿回来的是一个月前的答案。

    **这比「核心记忆失明」更隐蔽：东西在，只是旧的。** 失明会零召回，很快被发现；
    陈旧会给出一个语气自信、内容过期的答案，没有任何信号。

    `_is_stale()` 和 `inject_context()` 里的 ⚠️ 标注早就存在，但那条信息只出现在
    **注入给模型的上下文里** —— 运维面（`/health`）完全看不到。也就是说：没有任何
    自动化手段能发现「核心记忆一个月没更新」，只能靠人正好去读一次注入内容。
    这个函数把同一个判据端到运维面上。

    返回字段：
      · `blocks`            —— 有内容的 block 数
      · `stale_blocks`      —— 其中超期的个数
      · `stale`             —— 是否有任一块超期（`/health` 的布尔判据）
      · `oldest_age_days`   —— 最旧那块距今多少天（保留一位小数；无法判定为 None）
      · `threshold_days`    —— 判据阈值，和注入面用的是同一个常量
      · `unparsable_blocks` —— 时间戳解析不了的块数（这些一律按超期算，见 `_is_stale`）
      · `unfilled_blocks`   —— 内容仍是出厂占位文本的块数（**不计入 stale**）
    """
    blocks = get_all_blocks(user_id=user_id, bank_id=bank_id)
    nonempty = [(k, b) for k, b in blocks.items() if (b.get("content") or "").strip()]
    # 「从来没填过」和「填过但很旧」是两件不同的事，要分开报。
    #
    # 变异轮发现的：`DEFAULT_BLOCKS` 播种的是「（尚未填写）…」这样的**占位文本**，
    # 不是空串。所以只按「内容非空」筛，一个从未填写的部署在 30 天后会被报成
    # 「3/3 块超过 30 天未更新」—— 而真相是「一次都没填过」。两者需要的动作完全
    # 不同（前者去填，后者去核对），合成一条告警等于两条都失去可行动性。
    #
    # 判据取自 `DEFAULT_BLOCKS` 本身，不硬编码那句前缀：播种文本一改，这里自动跟着改。
    placeholders = {(v or "").strip() for v in DEFAULT_BLOCKS.values()}
    filled = [(k, b) for k, b in nonempty
              if (b.get("content") or "").strip() not in placeholders]
    unfilled_n = len(nonempty) - len(filled)
    stale_n = 0
    unparsable = 0
    oldest: float | None = None
    for key, b in filled:
        ts = b.get("last_verified_at") or ""
        # v20.1 WP-D2：按块分级判据 —— 注入面（inject_context）用同一个函数。
        if _is_stale(ts, key):
            stale_n += 1
        age = _age_days(ts)
        if age is None:
            unparsable += 1
        elif oldest is None or age > oldest:
            oldest = age
    return {
        "blocks": len(filled),
        "stale_blocks": stale_n,
        "stale": stale_n > 0,
        "oldest_age_days": round(oldest, 1) if oldest is not None else None,
        # 全局兜底常量按旧名保留（老读者不断），分级生效值单列一份 ——
        # 生效值问函数不问配置文件。
        "threshold_days": STALENESS_DAYS,
        "threshold_days_by_block": {
            k: staleness_threshold_days(k) for k in BLOCK_KEYS
        },
        "unparsable_blocks": unparsable,
        # 仍是出厂占位文本的块数：不计入 stale，单独报
        "unfilled_blocks": unfilled_n,
    }


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
            # v20.1 WP-D2：注入面与运维面（staleness_status）用同一个分级判据。
            stale = _is_stale(b.get("last_verified_at", ""), key)
            if stale:
                stale_count += 1
                lines.append(f"{label}: {b['content']} [⚠️ {staleness_threshold_days(key)}天+未验证]")
            else:
                lines.append(f"{label}: {b['content']}")
    if stale_count:
        lines.append(f"⚠️ {stale_count} 个 block 超过各自阈值未验证，建议通过 API 更新或 verify")
    return "\n".join(lines) if len(lines) > 1 else ""


if __name__ == "__main__":
    init_core_memory()
    print("=== 全部 block ===")
    print(json.dumps(get_all_blocks(), ensure_ascii=False, indent=2))
    print("\n=== 注入上下文 ===")
    print(inject_context())
