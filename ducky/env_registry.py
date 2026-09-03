"""ducky.env_registry — 环境变量名单一真相源（v20.3.2 正式版 · 用户审计 H / GLM F-3）。

**为什么要有这个文件**：本仓环境变量分两套前缀 —— `AIDUMEM_`（冻结兼容的旧前缀）与
`AIDUMEI_`（当前前缀），共 90+ 个，分界没有规律：数据目录是 `AIDUMEM_DATA_DIR`，
引擎挡位是 `AIDUMEI_ENGINE_MODE`。用户凭直觉打字，写错一个字母 —— **不报错、不警告、
按默认值跑**，然后某天发现数据存错了地方。

这不是假想：作者本人在同一个版本里把 `AIDUMEM_DATA_DIR` 写成 `AIDUMEI_DATA_DIR` **踩了两次**
（CHANGELOG 原话「这是同一个坑在本文件里第二次踩」），测试库因此写进了仓库自己的 data/。
至今没有任何守卫拦它。

**做法**：
  · `KNOWN_ENV_VARS` 由 tests/test_v20_3_2_release_env_registry.py 从源码 AST 字符串常量
    自动抽取并断言与本表**完全相等** —— 新增变量忘登记、登记了不存在的名字，都红。
  · 启动时 `warn_unknown_env_vars()` 扫 `os.environ` 里所有 `AIDUME[IM]_*`，不在表里的
    **打 WARNING 并给出最接近的正确名**（difflib）。读世界（真实环境），不读文档。
  · **不做前缀别名回落**（读新前缀名时回落到旧前缀同名）—— 那会让双前缀永久化。
"""
from __future__ import annotations

import difflib
import logging
import os
import re

logger = logging.getLogger("aiduMEM.env_registry")

_PREFIX_RE = re.compile(r"^AIDUME[IM]_[A-Z0-9_]+$")

KNOWN_ENV_VARS: frozenset[str] = frozenset((
    "AIDUMEI_CORE_STALENESS_DAYS",
    "AIDUMEI_CORE_VECTOR_INDEX",
    "AIDUMEI_ENGINE_MODE",
    "AIDUMEI_FACTS_WATERMARK",
    "AIDUMEI_GEAR_COOLDOWN_SEC",
    "AIDUMEI_GEAR_PROBE_INTERVAL_SEC",
    "AIDUMEI_GEAR_RECOVER_SUCCESSES",
    "AIDUMEI_GEAR_TRIP_FAILURES",
    "AIDUMEI_LLM_GEAR_COOLDOWN_SEC",
    "AIDUMEI_LLM_GEAR_RECOVER_SUCCESSES",
    "AIDUMEI_LLM_GEAR_TRIP_FAILURES",
    "AIDUMEI_LOCAL_EMBED_CACHE",
    "AIDUMEI_LOGIN_FAILURES_PER_MIN",
    "AIDUMEI_LOGIN_MAX_TRACKED_IPS",
    "AIDUMEI_PATTERN_EXTRACT",
    "AIDUMEI_PENDING_WARN_LEVEL",
    "AIDUMEI_PORT",
    "AIDUMEI_RATE_ADD_PER_MIN",
    "AIDUMEI_RATE_DELETE_ALL_PER_MIN",
    "AIDUMEI_RECALL_EVIDENCE_GATE",
    "AIDUMEI_RECALL_MIN_HYBRID",
    "AIDUMEI_RECALL_VERDICT_THRESHOLD",
    "AIDUMEI_SALIENCE_FLOOR",
    "AIDUMEI_SALIENCE_HALF_LIFE_DAYS",
    "AIDUMEI_SCAN_WORDLIST",
    "AIDUMEI_SCAN_WORDS",
    "AIDUMEI_TRUST_PROXY",
    "AIDUMEI_TRUSTED_HOSTS",
    "AIDUMEI_TYPE_DECAY",
    "AIDUMEM_ALLOW_INSECURE_PUBLIC",
    "AIDUMEM_API_BASE",
    "AIDUMEM_API_PORT",
    "AIDUMEM_API_TOKEN",
    "AIDUMEM_BACKUP_ROOT",
    "AIDUMEM_CONFIG_FILE",
    "AIDUMEM_CONFIG_READONLY",
    "AIDUMEM_CONSOLIDATION_INTERVAL_HOURS",
    "AIDUMEM_COOKIE_SECURE",
    "AIDUMEM_DATA_DIR",
    "AIDUMEM_DATE_KEYWORDS",
    "AIDUMEM_DEFAULT_AGENT_ID",
    "AIDUMEM_DEFAULT_AGENT_NAME",
    "AIDUMEM_DEFAULT_USER_ID",
    "AIDUMEM_ENTITY_KEYWORDS",
    "AIDUMEM_ENV_FILE",
    "AIDUMEM_HOME",
    "AIDUMEM_HOST",
    "AIDUMEM_HOST_ERROR_LOG",
    "AIDUMEM_HOST_LAST_ID",
    "AIDUMEM_HOST_MEMORY_MD",
    "AIDUMEM_HOST_STATE_DB",
    "AIDUMEM_INJECTION_GUARD_MODE",
    "AIDUMEM_L0_CATEGORIES",
    "AIDUMEM_L1_PREFIXES",
    "AIDUMEM_LEGACY_USER_IDS",
    "AIDUMEM_LLM_PUBLIC_BASE",
    "AIDUMEM_LLM_TUNNEL_BASE",
    "AIDUMEM_LOG_DIR",
    "AIDUMEM_MAX_MEMORY_CHARS",
    "AIDUMEM_PERSONA_ENABLED",
    "AIDUMEM_PUBLIC_DOCS",
    "AIDUMEM_PYTHON",
    "AIDUMEM_RECALL_SCORE_FLOOR",
    "AIDUMEM_RECENCY_LAMBDA",
    "AIDUMEM_REFINE_ENABLED",
    "AIDUMEM_REFLECT_ENABLED",
    "AIDUMEM_REFLECT_INTERVAL_HOURS",
    "AIDUMEM_REFLECT_ON_SESSION_END",
    "AIDUMEM_RERANK_WEIGHT",
    "AIDUMEM_ROUTER_DB_PATH",
    "AIDUMEM_ROUTER_KEY_SUFFIX",
    "AIDUMEM_ROUTER_MODELS",
    "AIDUMEM_ROUTER_SSH_HOSTS",
    "AIDUMEM_ROUTER_SSH_KEY",
    "AIDUMEM_ROUTER_SSH_STRICT",
    "AIDUMEM_ROUTER_USAGE_ENABLED",
    "AIDUMEM_SELF_EDIT_ENABLED",
    "AIDUMEM_SERVER_KEYWORDS",
    "AIDUMEM_SERVICE",
    "AIDUMEM_SESSION_TTL_SECONDS",
    "AIDUMEM_SIGMOIDAL_TEMP",
    "AIDUMEM_SKILL_GROWTH_ENABLED",
    "AIDUMEM_SQLITE_VEC_EXTENSION",
    "AIDUMEM_SQLITE_VEC_PATH",
    "AIDUMEM_STRICT_TENANT",
    "AIDUMEM_SYNC_STATE",
    "AIDUMEM_TYPE_CLASSIFY_ENABLED",
    "AIDUMEM_UI_PASSWORD",
    "AIDUMEM_UPSTREAM",
    "AIDUMEM_VECTOR_BACKEND",
))


# 运行期拼出来的名字：源码里只出现**前缀**（以 `_` 结尾的字符串常量），完整名由
# 业务键拼接（core_memory.py：AIDUMEI_CORE_STALENESS_DAYS_ + 块名大写）。
# 这类前缀不是变量名，单独登记；is_known_env_name 对它做前缀匹配。
DYNAMIC_ENV_PREFIXES: frozenset[str] = frozenset((
    "AIDUMEI_CORE_STALENESS_DAYS_",
))


def is_known_env_name(name: str) -> bool:
    """完整名在表里，或以某个已登记的动态前缀开头且后面还有内容。"""
    if name in KNOWN_ENV_VARS:
        return True
    return any(name.startswith(p) and len(name) > len(p) for p in DYNAMIC_ENV_PREFIXES)


def _scan_source(root) -> tuple[frozenset[str], frozenset[str]]:
    """从源码 AST **字符串常量**抽 AIDUME?_ 变量名（守卫与生成器共用同一实现）。

    只看 AST 常量 → 注释天然排除；再排除 docstring（模块/函数/类首条 Expr 常量）；
    排除 ducky/version.py（版本叙事，里面故意写着踩过的错名）。
    shell 脚本与 deploy/ 单元文件按正则抽（部署面读的变量也算已知）。
    """
    import ast
    import pathlib as _pl
    root = _pl.Path(root)
    rx = re.compile(r"AIDUME[IM]_[A-Z0-9_]+")
    found: set[str] = set()
    py_files = [p for p in (root / "ducky").rglob("*.py") if p.name != "version.py"]
    py_files += [root / "api_server.py", root / "mcp_server.py", root / "mem0_sync.py"]
    py_files += list((root / "scripts").glob("*.py"))
    for f in py_files:
        if not f.exists():
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        doc_ids: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                        and isinstance(first.value.value, str):
                    doc_ids.add(id(first.value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in doc_ids:
                found.update(rx.findall(node.value))
    for f in list((root / "scripts").glob("*.sh")) + list((root / "deploy").rglob("*")):
        if f.is_file():
            try:
                found.update(rx.findall(f.read_text(encoding="utf-8", errors="ignore")))
            except Exception:
                pass
    names = frozenset(n for n in found if not n.endswith("_"))
    prefixes = frozenset(n for n in found if n.endswith("_"))
    return names, prefixes


def extract_env_names_from_source(root) -> frozenset[str]:
    """源码真正读的**完整**变量名集合（守卫断言 == KNOWN_ENV_VARS）。"""
    return _scan_source(root)[0]


def extract_dynamic_prefixes_from_source(root) -> frozenset[str]:
    """源码里以 `_` 结尾的前缀常量集合（守卫断言 == DYNAMIC_ENV_PREFIXES）。"""
    return _scan_source(root)[1]


def unknown_env_vars(environ=None) -> dict[str, list[str]]:
    """返回 {未知变量名: [最接近的已知名, ...]}。纯函数，供守卫与启动共用。"""
    env = os.environ if environ is None else environ
    out: dict[str, list[str]] = {}
    for key in env:
        if _PREFIX_RE.match(key) and not is_known_env_name(key):
            out[key] = difflib.get_close_matches(key, KNOWN_ENV_VARS, n=3, cutoff=0.6)
    return out


def warn_unknown_env_vars(environ=None) -> dict[str, list[str]]:
    """启动期出声：设了本系统不读的 AIDUME?_ 变量 = 大概率是拼错了前缀或名字。"""
    found = unknown_env_vars(environ)
    for key, near in found.items():
        hint = f"；你是不是想设 {near[0]}？" if near else ""
        logger.warning(
            "⚠️ 环境变量 %s 不在本系统的已知清单里，**不会被读取**（会静默按默认值跑）%s",
            key, hint,
        )
    return found
