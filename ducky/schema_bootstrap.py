"""
ducky.schema_bootstrap — 核心表的首次建表（Aegis v14）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

为什么需要这个模块
    v13 及更早版本里，`facts` / `entities` / `fact_entities` 三张核心表
    是在部署时手工建的，代码里只有各功能模块自己的附属表 DDL。
    结果是：全新克隆的仓库启动后，所有依赖 facts 表的功能直接
    `no such table: facts`。v14 Aegis 把「开箱可部署」当作硬指标，
    因此把核心表 DDL 收进代码，作为唯一真相源。

安全承诺
    · 全部 CREATE TABLE / INDEX IF NOT EXISTS，对既有库是 no-op
    · 不做任何 DROP / 改类型 / 删数据
    · 单次进程内只跑一次（幂等 + 线程安全）
    · 任何异常只记日志不抛，主服务照常启动（降级而非崩溃）

列集说明
    facts 表的列集与 v13 生产库完全一致，包含历史各版本累积的字段
    （trust 反馈、L0/L1/L2 分层、Chronos 双时间轴、Pantheon 联邦）。
    这样新库一次建全，老库靠 federation.schema 的 ADD COLUMN 迁移补齐，
    两条路径最终收敛到同一 schema。
"""
from __future__ import annotations

import logging
import threading

from ducky.utils import DEFAULT_AGENT_ID, DEFAULT_USER_ID, get_facts_conn

logger = logging.getLogger("aiduMEM.SchemaBootstrap")

_FACTS_DDL = f"""
CREATE TABLE IF NOT EXISTS facts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    category         TEXT NOT NULL DEFAULT 'general',
    fact_key         TEXT NOT NULL,
    fact_value       TEXT NOT NULL,
    source           TEXT DEFAULT '{DEFAULT_USER_ID}',
    confidence       INTEGER DEFAULT 100,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    -- 信任反馈（v1 Phase 3A）
    trust_score      REAL DEFAULT 0.5,
    helpful_count    INTEGER DEFAULT 0,
    unhelpful_count  INTEGER DEFAULT 0,
    retrieval_count  INTEGER DEFAULT 0,
    last_accessed_at TIMESTAMP,
    -- 归档（v3 半衰期）
    archived         INTEGER DEFAULT 0,
    archived_at      TIMESTAMP,
    -- L0/L1/L2 分层（v1 Phase 2.1）
    summary          TEXT,
    overview         TEXT,
    level            TEXT DEFAULT 'I',
    peer             TEXT DEFAULT 'user',
    preference_score REAL DEFAULT 0.0,
    -- Chronos 双时间轴（v12）
    expires_at       TEXT,
    valid_from       TEXT,
    valid_to         TEXT,
    -- Pantheon 联邦（v13）
    agent_id         TEXT DEFAULT '{DEFAULT_AGENT_ID}',
    profile          TEXT DEFAULT 'default',
    memory_tier      TEXT DEFAULT 'semantic',
    recorded_at      TIMESTAMP,
    tags             TEXT DEFAULT '',
    decay_at         TEXT,
    shared           INTEGER DEFAULT 1,
    -- Mímir 借鉴：敏感级别分档（v17）
    -- internal: 授权范围内可用 | confidential: 限制 owner/外发 | restricted: 本地仅限
    sensitivity      TEXT DEFAULT 'internal',
    -- aiduMEI v18.3 多模态与 Obsidian 双链支持
    media_url        TEXT,
    vision_caption   TEXT
)
"""

_ENTITIES_DDL = """
CREATE TABLE IF NOT EXISTS entities (
    entity_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    entity_type TEXT DEFAULT 'unknown',
    aliases     TEXT DEFAULT '',
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_FACT_ENTITIES_DDL = """
CREATE TABLE IF NOT EXISTS fact_entities (
    fact_id   INTEGER,
    entity_id INTEGER,
    PRIMARY KEY (fact_id, entity_id),
    FOREIGN KEY (fact_id)   REFERENCES facts(id),
    FOREIGN KEY (entity_id) REFERENCES entities(entity_id)
)
"""

# 变更事件账本（借鉴 Mímir 事件账本设计，v17）
_FACT_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS fact_events (
    event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type   TEXT NOT NULL,
    category     TEXT DEFAULT '',
    fact_key     TEXT DEFAULT '',
    new_value    TEXT DEFAULT '',
    affected_ids TEXT DEFAULT '[]',
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
"""

_INDEXES = (
    "CREATE INDEX        IF NOT EXISTS idx_facts_category ON facts(category)",
    "CREATE INDEX        IF NOT EXISTS idx_facts_key      ON facts(fact_key)",
    # 🔴3：唯一约束按 agent 隔离，避免联邦跨 Agent 同 (category,fact_key) 静默互覆盖。
    # 全新库直接建成三列版；存量库由 federation.schema._rebuild_agent_scoped_unique_index 升级。
    # 注：facts 表在 federation 迁移前可能尚无 agent_id 列，此处 CREATE 若失败会被下方 try 吞掉，
    # 由联邦迁移阶段（agent_id 就位后）重建，最终一致。
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_unique   ON facts(agent_id, category, fact_key)",
    "CREATE INDEX        IF NOT EXISTS idx_entities_name  ON entities(name)",
    "CREATE INDEX        IF NOT EXISTS idx_fevents_type   ON fact_events(event_type)",
    "CREATE INDEX        IF NOT EXISTS idx_fevents_cat    ON fact_events(category)",
)

_lock = threading.Lock()
_done = False

CURRENT_SCHEMA_VERSION = 2  # v18.3: 添加 media_url, vision_caption


def apply_migrations(conn) -> None:
    """基于 user_version 实现秒级增量补丁迁移（无损升级 v18.3）"""
    try:
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]

        # 对于老的、之前没有使用版本控制的 v18.2 及以前的库，版本号通常是 0。
        # 第一版版本控制我们将其基线定义为版本 1。
        if user_version == 0:
            # 标记存量完整库为版本 1（即缺少 media_url 和 vision_caption 的阶段）
            logger.info("检测到老版本数据库，初始基准定为 Version 1")
            user_version = 1
            conn.execute("PRAGMA user_version = 1")
            conn.commit()

        # 版本迁移流：Version 1 -> Version 2
        if user_version < 2:
            logger.info("执行数据库增量升级：v1 -> v2 (增加多模态字段)")
            # SQLite 的 ALTER TABLE ADD COLUMN 是近乎 O(1) 的超快操作
            try:
                conn.execute("ALTER TABLE facts ADD COLUMN media_url TEXT;")
            except Exception as e:
                logger.debug(f"字段 media_url 已存在或跳过: {e}")

            try:
                conn.execute("ALTER TABLE facts ADD COLUMN vision_caption TEXT;")
            except Exception as e:
                logger.debug(f"字段 vision_caption 已存在或跳过: {e}")

            conn.execute("PRAGMA user_version = 2")
            conn.commit()
            logger.info("数据库秒级增量升级 v2 成功 ✅")
            user_version = 2

        # 未来的版本升级可以直接在下面追加: if user_version < 3: ...

    except Exception as exc:
        logger.error("数据库增量补丁执行异常 (服务继续启动): %s", exc)


def ensure_core_schema(force: bool = False) -> dict:
    """幂等建表。返回本次实际新建的表名列表。"""
    global _done
    with _lock:
        if _done and not force:
            return {"status": "ok", "skipped": True, "created_tables": []}

        created: list[str] = []
        conn = get_facts_conn()
        try:
            for name, ddl in (
                ("facts", _FACTS_DDL),
                ("entities", _ENTITIES_DDL),
                ("fact_entities", _FACT_ENTITIES_DDL),
                ("fact_events", _FACT_EVENTS_DDL),
            ):
                existed = conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
                    (name,),
                ).fetchone()[0]
                conn.execute(ddl)
                if not existed:
                    created.append(name)

            for stmt in _INDEXES:
                try:
                    conn.execute(stmt)
                except Exception as exc:  # 老库上可能已有同名非唯一索引
                    logger.debug("索引跳过 (%s): %s", stmt.split()[-1], exc)

            # 执行 schema 版本增量迁移补丁
            apply_migrations(conn)

            conn.commit()
            _done = True
        except Exception as exc:
            logger.error("核心 schema 建表异常（服务继续启动）: %s", exc)
            return {"status": "error", "detail": str(exc), "created_tables": created}

        if created:
            logger.info("🏗️ 核心 schema 初始化完成 · 新建表=%s", created)
        return {"status": "ok", "skipped": False, "created_tables": created}
