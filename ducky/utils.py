"""
ducky.utils — 思想引擎工具函数（去重合并，全局共享）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
此前分散在 hybrid_recall、memory_ignition、memory_workspace、
memory_persistence、memory_jlens、memory_broadcast、recall_funnel。
v8 重构统一入口 — 改一处，全局生效。
"""

import math
import datetime as dt
import logging

logger = logging.getLogger("aiduMEM.utils")

# ═══════════════════════════════════════════════
# 文本相似度（词级 Jaccard）
# ═══════════════════════════════════════════════

def quick_sim(a: str, b: str) -> float:
    """快速词级 Jaccard 相似度 — workspace / persistence / broadcast / jlens 共用"""
    if not a or not b:
        return 0.0
    ta = set(a.lower().split())
    tb = set(b.lower().split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ═══════════════════════════════════════════════
# Bigram Tokenize + Jaccard（Ignition 引擎）
# ═══════════════════════════════════════════════

_STOP_WORDS = {
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
    "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着",
    "没有", "看", "好", "自己", "这", "他", "她", "它", "没", "把", "从",
    "呢", "吧", "啊", "哦", "哈", "嗯", "哇", "嘿",
}


def tokenize(text: str) -> set:
    """Bigram tokenize + 去停用词，用于 Ignition 语义粗筛"""
    clean = text.lower().strip()
    bigrams = {clean[i:i+2] for i in range(len(clean)-1)}
    words = set(clean.split())
    return bigrams | (words - _STOP_WORDS)


def jaccard_sim(a: str, b: str) -> float:
    """Bigram 级 Jaccard 相似度 — Ignition 引擎专用"""
    ta, tb = tokenize(a), tokenize(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ═══════════════════════════════════════════════
# 分数归一化
# ═══════════════════════════════════════════════

def normalize_score(score: float | None) -> float:
    """mem0 Qdrant score → [0, 1] 归一化。Score 越高代表相关度越高"""
    if score is None:
        return 0.5
    if score <= 0:
        return 0.0
    # Qdrant 余弦相似度正常在 0.0~1.0，如超出则截断到 [0, 1]
    return max(0.0, min(1.0, float(score)))


# ═══════════════════════════════════════════════
# 时间解析
# ═══════════════════════════════════════════════

def parse_iso_timestamp(ts_str: str) -> float:
    """ISO 时间戳 → Unix timestamp"""
    ts_clean = ts_str.replace("+00:00", "Z").replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(ts_clean).timestamp()
    except Exception:
        return 0.0


# ═══════════════════════════════════════════════
# 数据库连接工厂 (Hyperion v11.1 · 线程本地复用)
# ═══════════════════════════════════════════════
import os
import sqlite3

# 安装根目录 — 单一真源。默认取本文件的上两级（即仓库根），
# 也可用环境变量覆盖以支持任意部署路径：
#     export AIDUMEM_HOME=/opt/aidumem
BASE_DIR = os.environ.get("AIDUMEM_HOME") or os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))
)
DATA_DIR = os.environ.get("AIDUMEM_DATA_DIR") or os.path.join(BASE_DIR, "data")
LOG_DIR  = os.environ.get("AIDUMEM_LOG_DIR")  or os.path.join(BASE_DIR, "logs")

# 首次运行（全新克隆 / 换了 AIDUMEM_DATA_DIR）时目录还不存在，
# SQLite 不会自建父目录，这里补上，保证「克隆即跑」。
for _d in (DATA_DIR, LOG_DIR):
    try:
        os.makedirs(_d, exist_ok=True)
    except OSError:
        pass  # 只读环境下静默降级，由后续实际读写抛出更明确的错误

FACTS_DB      = os.path.join(DATA_DIR, "facts.db")
OBS_DB        = os.path.join(DATA_DIR, "observations.db")
SCENES_DB     = os.path.join(DATA_DIR, "scenes.db")
TEXT_FTS_DB   = os.path.join(DATA_DIR, "text_fts.db")
SALIENCE_DB   = os.path.join(DATA_DIR, "salience.db")

# 合并任务互斥锁（v19.4.1 补齐）：scripts/consolidator.py 与服务内的
# 后台合并循环共用同一个文件锁防双跑。该常量此前从未定义，
# consolidator 在 _with_file_lock 里 `from ducky.utils import CONSOLIDATOR_LOCK`
# 直接 ImportError —— 与 memory_salience 门面缺口是同一类静默故障。
CONSOLIDATOR_LOCK = os.path.join(DATA_DIR, "consolidator.lock")


# ═══════════════════════════════════════════════
# .env 兜底加载与 API 凭据（v19.4.1 · P0-1 配套）
# ═══════════════════════════════════════════════
#
# 为什么需要这个（v19.4.1 部署体检发现的定时炸弹）：
#     服务进程靠 systemd `EnvironmentFile=/opt/aidumem/.env` 拿到
#     AIDUMEM_API_TOKEN，但 **cron 不会加载 .env** —— 它的环境几乎是空的。
#     于是鉴权门禁一开启，所有 cron 任务（consolidator 每日 2:30、
#     健康检查等）在下一次触发时会集体 401。
#     这类失败还特别安静：脚本失败只写日志，没有人被通知。
#
#     解法不是让运维记得去改 crontab（那是靠自觉），而是让脚本自己
#     从仓库根的 .env 兜底读取凭据 —— 与服务端读同一份文件，单一真相源。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env_file(path: str | None = None, *, override: bool = False) -> int:
    """把 .env 里的键值补进 os.environ。返回实际注入的条数。

    · 只做最简解析（KEY=VALUE、# 注释、可选引号），不引入 dotenv 依赖；
    · 默认 **不覆盖** 已存在的环境变量（显式传入的优先级最高）；
    · 文件不存在或读失败一律静默返回 0，绝不影响调用方主流程。
    """
    target = path or os.environ.get("AIDUMEM_ENV_FILE") or os.path.join(_REPO_ROOT, ".env")
    if not os.path.isfile(target):
        return 0
    injected = 0
    try:
        with open(target, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if not key:
                    continue
                if override or key not in os.environ:
                    os.environ[key] = value
                    injected += 1
    except Exception as exc:
        logger.debug("load_env_file 跳过 (%s): %s", target, exc)
        return 0
    return injected


def api_auth_headers() -> dict:
    """构造调用本服务 REST 接口的鉴权头（单一真相源）。

    取值顺序：环境变量 → 仓库根 .env 兜底。未配置 token 时返回空 dict，
    行为与门禁未启用时完全一致（本机零配置可用）。
    """
    token = os.environ.get("AIDUMEM_API_TOKEN", "").strip()
    if not token:
        load_env_file()
        token = os.environ.get("AIDUMEM_API_TOKEN", "").strip()
    return {"Authorization": f"Bearer {token}"} if token else {}

# ═══════════════════════════════════════════════
# 默认身份标识 — 单一真源
# ═══════════════════════════════════════════════
# 记忆按 user_id 分区、按 agent_id 联邦。部署方可以用环境变量
# 换成自己的标识，源码里不写任何真实人名或昵称。
#   AIDUMEM_DEFAULT_USER_ID   单用户部署时的默认 user_id
#   AIDUMEM_DEFAULT_AGENT_ID  本机主 Agent 的联邦标识
DEFAULT_USER_ID  = os.environ.get("AIDUMEM_DEFAULT_USER_ID", "default")
DEFAULT_AGENT_ID = os.environ.get("AIDUMEM_DEFAULT_AGENT_ID", "local")

# 线程本地连接缓存，每个线程+数据库路径只建一次连接
import threading
_thread_local = threading.local()


class _ConnProxy:
    """SQLite 连接代理：close() 变 no-op，防止线程本地连接被意外关闭"""
    __slots__ = ('_conn',)

    def __init__(self, conn: sqlite3.Connection):
        object.__setattr__(self, '_conn', conn)

    def close(self):
        """no-op：线程本地连接不可关闭"""
        pass

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        if name == '_conn':
            object.__setattr__(self, name, value)
        else:
            setattr(self._conn, name, value)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass  # 不关闭


def _get_thread_conn(db_path: str) -> sqlite3.Connection:
    """获取当前线程的复用连接，首次调用时建立

    统一设 row_factory=sqlite3.Row：所有调用方都能按列名取值（row['category']），
    同时 Row 支持整数索引（row[0]）与迭代解包，向后完全兼容 tuple 用法。
    ⚠️ 别再在调用方手动设 row_factory——连接是线程复用的，close() 是 no-op，
    局部赋值会永久污染同线程后续所有调用方（v12.0.1 修的就是这个跨模块隐式依赖）。
    """
    cache_key = f"conn_{db_path}"
    real_conn = getattr(_thread_local, cache_key, None)
    if real_conn is not None:
        try:
            real_conn.execute("SELECT 1")  # 健康检查
            return _ConnProxy(real_conn)
        except Exception:
            # 连接已断开，重新创建
            try:
                real_conn.close()
            except Exception as e:
                logger.debug(f"_get_thread_conn: suppressed exception: {e}")
    # 新建连接
    real_conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10.0)
    real_conn.row_factory = sqlite3.Row
    real_conn.execute("PRAGMA journal_mode=WAL")
    real_conn.execute("PRAGMA busy_timeout=10000")
    real_conn.execute("PRAGMA cache_size=-500")  # 500KB/conn (default -2000=2MB)
    # 🔴9：注册 REGEXP 函数。SQLite 不原生支持 REGEXP，conflict_resolver 的
    # `fact_value REGEXP ?` 此前必抛 "no such function" 被吞，热路径空转。
    try:
        real_conn.create_function("REGEXP", 2, _sqlite_regexp, deterministic=True)
    except Exception:
        real_conn.create_function("REGEXP", 2, _sqlite_regexp)
    setattr(_thread_local, cache_key, real_conn)
    return _ConnProxy(real_conn)


def _sqlite_regexp(pattern, value) -> int:
    """SQLite REGEXP 实现：大小写不敏感，NULL/异常安全返回 0。"""
    if value is None or pattern is None:
        return 0
    try:
        import re as _re
        return 1 if _re.search(str(pattern), str(value), _re.IGNORECASE) else 0
    except Exception:
        return 0

def get_facts_conn():
    return _get_thread_conn(FACTS_DB)

def get_obs_conn():
    return _get_thread_conn(OBS_DB)

def get_scenes_conn():
    return _get_thread_conn(SCENES_DB)

def get_text_conn():
    return _get_thread_conn(TEXT_FTS_DB)

def get_salience_conn():
    return _get_thread_conn(SALIENCE_DB)


def ensure_evolution_tables():
    """在 facts.db 里创建 Lethe 版知识演化追踪表和记忆生命周期表"""
    try:
        conn = get_facts_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memory_states (
                memory_id TEXT PRIMARY KEY,
                state TEXT NOT NULL DEFAULT 'active',
                reason TEXT DEFAULT '',
                source TEXT DEFAULT 'system',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS knowledge_evolution (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                relation_type TEXT NOT NULL,
                confidence REAL DEFAULT 0.5,
                reason TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug(f"ensure_evolution_tables skip: {e}")

# 自动运行确保表就绪
ensure_evolution_tables()


