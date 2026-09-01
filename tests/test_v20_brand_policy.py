"""v20.0 品牌 VI 策略守卫 —— 把「哪些字该改、哪些键不许动」写成可执行的规则。

目标是「适配各种新老客户」「部署起来无风险和差异感」。落成三条规则：

  1. **人读到的字** → 当前品牌名 aiduMEI。
     判据是「运行时会露脸」或「用户会打开来读/编辑」：print/echo、注入进对话的文本、
     `--help` 输出、`systemctl status` 的 Description、写进用户 crontab 的注释、
     MCP 工具说明，以及 README / 集成指南 / `.env.example` 一类样例文件。
  2. **机器认的键** → 一个都不动。
     logger 名、`/health` 的 service 字段、线程名、`AIDUMEM_*` 环境变量、
     文件名/目录名/包名、拿去匹配存量数据的查询串与 `INSERT OR IGNORE` 种子值。
     改这些**不会报错**，只会静默失配 —— 老客户的 .env 与生产侧监控当场失灵，
     而失败与成功从外面看一模一样。
  3. **历史记录** → 只新增，不改写。CHANGELOG、version.py 旧条目、白皮书、
     README 的「品牌演进」那句话里必须继续留着旧名，否则演进史就被抹平成
     「从来如此」。

源码里的 docstring 与代码注释**刻意不算露脸**：它们不是 UI，改了零收益，而每动一行
都要在生产切换前重新核对一遍。这不是本轮新拍的判断，v19.4.2 的决策 D2 已经这么定过
（见 api_server.py 里 FastAPI 那段注释）。所以本仓里仍有几百处 aiduMEM 是有意留下的，
不是漏改 —— 数目与理由记在 CHANGELOG v20.0 条目里。

为什么单独立一个文件：v19.4.2 那条用户可见面守卫的射程只有
`frontend/**/*.{html,js,css,json}`，frontend 之外两个方向都无人看守 ——
一个值在两个方向上都能无声翻转，等于这个值没有守卫。v20 把射程补齐，并且把
「不该动的那一侧」也钉住：数量对不上就红，而不是等生产告警自己安静下去。
"""

import os
import re
import subprocess

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# 大小写敏感：这样 AIDUMEM_URL（大写键名）不会被误判成品牌残留，
# 而 aiduMEM / duMem（品牌写法）一个都跑不掉。
_LEGACY = re.compile(r"aiduMEM|duMem")
_LOGGER = re.compile(r'getLogger\(f?"aiduMEM')
_ENV_KEY = re.compile(r"\bAIDUMEM_[A-Z0-9_]+\b")


def _read(rel):
    with open(os.path.join(_REPO_ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _readable(rel):
    """能当文本读的受版本控制文件（跳过二进制与已删除项）。"""
    if not os.path.isfile(os.path.join(_REPO_ROOT, rel)):
        return False
    try:
        _read(rel)
        return True
    except (UnicodeDecodeError, OSError):
        return False


# 源码清单**不走 git**。理由是实测出来的，不是洁癖：
# 生产那台机器是**拷文件部署**的，仓里的 .git 停在旧提交（实测 231 条索引 vs
# 磁盘上 282 个文件）。那种状态下 `git ls-files` 不报错，只少报 —— 51 个 v20
# 新增文件一个都不在清单里，下面每条守卫照常变绿，却根本没查到新增的那一面。
# 而在 sdist/tar 解出来的目录里（客户拿到的就是这个）它直接 128 报错。
# 「跑不通」看得见，「少查」看不见 —— 所以按目录白名单走文件系统，谁都不依赖。
_SKIP_DIRS = frozenset({
    ".git", ".idea", ".vscode", ".mypy_cache", ".ruff_cache", ".pytest_cache",
    "__pycache__", "venv", ".venv", "env", "node_modules", "dist", "build",
    ".eggs", "data", "backups", ".upgrade-artifacts", "logs", "htmlcov", "tests",
})
# 生产目录里混着未受版本控制的东西：真 .env、库文件、日志、备份包。
# 它们既不该被品牌守卫扫（会误红），更不该被断言消息打印出来（会泄密）。
_SKIP_SUFFIXES = (
    ".pyc", ".pyo", ".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3",
    ".log", ".tar", ".gz", ".tgz", ".zip", ".whl", ".bak", ".swp", ".pem", ".key",
)


def _is_secret_env(name):
    """真 .env 一律跳过；`.env.example` 是要发给客户的样例，必须留下。"""
    return (name == ".env" or name.startswith(".env.")) and not name.endswith(".example")


def _source_files():
    """随包发布的非测试文本文件（按目录走盘，与 git 无关）。

    扣掉 tests/ 是因为测试里带占位键名（例如断言前缀行为用的 AIDUMEM_SOME_OTHER），
    把它们算进冻结集只会让守卫在无关的测试改动上变红 —— 一条经常误红的守卫，
    最后会被人养成「见红就改数字」的习惯，那就等于没有守卫。
    真正会发给客户的配置只从源码来。
    """
    found = []
    for cur, dirs, files in os.walk(_REPO_ROOT):
        dirs[:] = sorted(
            d for d in dirs
            if d not in _SKIP_DIRS and not d.startswith("venv-") and not d.startswith("backup-")
        )
        for name in sorted(files):
            if name.endswith(_SKIP_SUFFIXES) or _is_secret_env(name):
                continue
            rel = os.path.relpath(os.path.join(cur, name), _REPO_ROOT)
            if _readable(rel):
                found.append(rel)
    return found


# 「代码面」= 真的会被执行的文件，再扣掉 ducky/version.py。
# version.py 后缀是 .py，但内容是逐版本的流水账；CHANGELOG.md 同理。
# 这两个地方恰恰是「记录本次改动」时会原样引用 getLogger("aiduMEM 这串字符的地方，
# 而一处引用并不是一处调用点。若把它们算进来，写清楚自己干了什么反而会让守卫变红，
# 下一个人学到的就是「见红就把 85 改成 87」—— 处数守卫的意义当场归零。
# 所以：数调用点只数代码，数品牌残留（下面各面）仍然覆盖全仓。
_RECORD_FILES = {"ducky/version.py", "CHANGELOG.md"}
_CODE_SUFFIXES = (".py", ".sh", ".service", ".js", ".mjs")


def _code_files():
    return [
        f
        for f in _source_files()
        if f.endswith(_CODE_SUFFIXES) and f not in _RECORD_FILES
    ]


# ══ 一、机器契约：logger 名的处数被钉住 ════════════════════════════════
# 生产侧日志采集按 aiduMEM.* / aiduMEM-v* 过滤。一次「顺手清理品牌残留」的全局替换
# 会把这些一起改掉：服务照常起、日志照常写，只是再也进不了采集管道。
# 数字放在这里，少一处就红。
_LOGGER_SITES = 93
# 累积记账（每次变动都要写明来路，否则这个数字迟早变成没人敢动的常数）：
#   · v20.2 自动挡: +3（gear/local_embed/dual_index）
#   · v20.2.1 外审 R1: +1（rate_guard）
#   · v20.2.3 三档可选: +1（ducky/engine_mode.py）

# ══ 二、机器契约：AIDUMEM_* 环境变量冻结集 ══════════════════════════════
# 这些键名已经写在客户的 .env 里。改前缀不会报错，只会让配置静默回落到默认值 ——
# AIDUMEM_API_TOKEN 是鉴权、AIDUMEM_STRICT_TENANT 是 v20 的银行隔离开关、
# AIDUMEM_LEGACY_USER_IDS 曾经就这样炸过一次（见 CHANGELOG v19 段）。
# 新增环境变量请用 AIDUMEI_ 前缀（仓里已有 5 个），不要往这张表里加。
_FROZEN_ENV_KEYS = {
    "AIDUMEM_ALLOW_INSECURE_PUBLIC",
    "AIDUMEM_API_BASE",
    "AIDUMEM_API_PORT",
    "AIDUMEM_API_TOKEN",
    "AIDUMEM_BACKUP_ROOT",
    "AIDUMEM_BODY",
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
    "AIDUMEM_HOOK_QUIET",
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
    "AIDUMEM_MAX_SIZE",
    "AIDUMEM_MIN_HISTORY",
    "AIDUMEM_MSG",
    "AIDUMEM_NEW_SESSION_MAX",
    "AIDUMEM_PATH",
    "AIDUMEM_PERSONA_ENABLED",
    "AIDUMEM_PERSONA_MAX_MEMORIES",
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
    "AIDUMEM_SEARCH_LIMIT",
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
    "AIDUMEM_TIMEOUT",
    "AIDUMEM_TYPE_CLASSIFY_ENABLED",
    "AIDUMEM_UI_PASSWORD",
    "AIDUMEM_UPSTREAM",
    "AIDUMEM_URL",
    "AIDUMEM_USER_ID",
    "AIDUMEM_VECTOR_BACKEND",
}

# ══ 二之二、机器契约：高危键必须还在「被读取」 ═══════════════════════════
# 上面那条只比对键名的**集合**，有个洞：如果只在读取处改名、而 .env.example 或
# README 里仍提到这个键，集合不变，守卫照旧绿 —— 但运行时已经读不到值了。
# 对「读不到就静默回落到默认值、且后果严重」的键，再钉一层：它必须仍然
# 出现在某个 os.environ.get / os.getenv 里。
# (键, 读不到会发生什么)
_CRITICAL_ENV_READS = [
    ("AIDUMEM_API_TOKEN", "读不到 = 视作未设令牌，鉴权那一层直接敞开"),
    ("AIDUMEM_UI_PASSWORD", "读不到 = 界面口令回落到内置默认值"),
    ("AIDUMEM_STRICT_TENANT", "读不到 = 严格租户关闭，v20 的银行隔离被静默降级"),
    ("AIDUMEM_LEGACY_USER_IDS", "读不到 = 老身份不再映射，历史记忆当场召不回（v19 出过一次）"),
    ("AIDUMEM_DATA_DIR", "读不到 = 数据目录换地方，等于换了一个空库"),
    ("AIDUMEM_ROUTER_DB_PATH", "读不到 = 路由用量写去了别处"),
    ("AIDUMEM_SQLITE_VEC_PATH", "读不到 = 向量库换地方，检索全空但不报错"),
]


# ══ 三、露脸面：整份文件里不许再有旧品牌名 ══════════════════════════════
# 用户会打开来读或编辑的纯文档 / 样例文件。这些文件里出现的 AIDUMEM_* 是大写键名，
# 与大小写敏感的 aiduMEM 不冲突，所以可以要求「整份归零」。
_CLEAN_FILES = [
    "ARCHITECTURE.md",
    ".env.example",
    "requirements.txt",
    "requirements-dev.txt",
    "mem0_config_local.json.example",
    "integrations/INTEGRATION_GUIDE.md",
    "integrations/config.yaml.snippet",
    "integrations/cursor-hook/README.md",
    "integrations/cursor-hook/cursor-aidumem.mdc",
    "integrations/hermes-plugin/aidumem/plugin.yaml",
]

# ══ 四、露脸面：源码里逐条点名的运行时输出 ══════════════════════════════
# 这些文件的 docstring / 注释仍是旧名（刻意），所以只能逐条钉，不能整份归零。
# (文件, 必须出现, 必须不出现, 为什么它算露脸)
_RUNTIME_OUT = [
    (
        "integrations/hermes-plugin/aidumem/__init__.py",
        "# aiduMEI",
        "# aiduMEM",
        "写进宿主 system prompt 的小节标题，用户在对话里直接读到",
    ),
    (
        "integrations/hermes-plugin/aidumem/__init__.py",
        "[aiduMEI 记忆检索]",
        "[aiduMEM 记忆检索]",
        "注入进本轮上下文的检索块标题",
    ),
    (
        "integrations/hermes-plugin/aidumem/__init__.py",
        "Check aiduMEI health",
        "Check aiduMEM health",
        "MCP 工具说明，会发给宿主 Agent 展示",
    ),
    (
        "integrations/hermes-plugin/aidumem/__init__.py",
        "Stored in aiduMEI.",
        "Stored in aiduMEM.",
        "工具返回值，直接回到对话里",
    ),
    (
        "integrations/hermes-plugin/aidumem/__init__.py",
        '"aiduMEI unreachable"',
        '"aiduMEM unreachable"',
        "服务不可达时用户看到的报错",
    ),
    (
        "integrations/cursor-hook/aidumem-on-save.sh",
        "✅ aiduMEI: ",
        "✅ aiduMEM: ",
        "保存钩子 echo 给用户看的成功提示",
    ),
    (
        "integrations/cursor-hook/claude-code-hook.py",
        "aiduMEI × Claude Code Hook CLI",
        "aiduMEM × Claude Code Hook CLI",
        "argparse description，即 --help 的输出",
    ),
    (
        "api_server.py",
        "✅ aiduMEI v%s",
        "✅ aiduMEM v%s",
        "启动横幅，运维在 journalctl 第一眼看到的那行",
    ),
    (
        "mcp_server.py",
        "添加记忆到 aiduMEI（",
        "添加记忆到 aiduMEM（",
        "MCP 工具 docstring = 发给宿主的工具说明",
    ),
    (
        "mem0_sync.py",
        "MEMORY.md → aiduMEI 同步引擎",
        "MEMORY.md → aiduMEM 同步引擎",
        "argparse description，即 --help 的输出",
    ),
    (
        "deploy/aidumem-api.service",
        "Description=aiduMEI Memory Engine",
        "Description=aiduMEM Memory Engine",
        "systemctl status 显示的服务描述",
    ),
    (
        "scripts/update_crontab.sh",
        "# aiduMEI:${name}|owner=${owner}|failure=${failure}",
        "# aiduMEM:${name}|owner=${owner}|failure=${failure}",
        "注释头的模板行 —— 实际写进用户 crontab 的产出是 `# aiduMEI:health_check|…`",
    ),
    (
        "scripts/health_check.py",
        "aiduMEI 健康检查",
        "aiduMEM 健康检查",
        "print 给运维读的那一行",
    ),
    (
        "integrations/aidumem-inject.sh",
        "['[aiduMEI Recall]']",
        "[aiduMEM Recall]",
        "注入进下一轮对话的前缀，用户直接读到",
    ),
]

# ══ 五、历史记录：必须继续留着旧名 ══════════════════════════════════════
_HISTORY_KEEPS_LEGACY = [
    ("CHANGELOG.md", "变更史：抹掉旧名等于篡改历史"),
    ("ducky/version.py", "版本条目：旧条目只增不改"),
    ("README.md", "「品牌演进 aiduMEM → aiduMEI」那句话，改了演进史就被抹平"),
    ("docs/aiduMEM-v10-Synapse-Design.md", "有日期的设计文档，属历史存档"),
    ("aiduMEM-v11-Hyperion-Whitepaper.md", "白皮书：署了版本与代号的历史文本"),
    ("aiduMEM-v9.3-Aletheia-Whitepaper.md", "白皮书，同上"),
]
# 注：README_EN.md 不在这张表里 —— 英文版没有那句「品牌演进」，
# 它里面的 AIDUMEM_* 全是大写键名，属机器契约那一侧，由环境变量冻结集看着。


def test_guard_tables_are_not_empty_and_point_at_real_files():
    """先证明这些表还指着真东西 —— 空集不算通过。"""
    assert _CLEAN_FILES, "露脸文档表被清空了"
    assert _RUNTIME_OUT, "运行时输出表被清空了"
    assert _HISTORY_KEEPS_LEGACY, "历史文件表被清空了"
    assert _CRITICAL_ENV_READS, "高危键读取点表被清空了"
    assert {k for k, _ in _CRITICAL_ENV_READS} <= _FROZEN_ENV_KEYS, (
        "高危键表里有键不在冻结集里 —— 两张表对不上，说明其中一张已经过期"
    )
    assert len(_FROZEN_ENV_KEYS) == 73, (
        "冻结集条数从 73 变成了 %d —— 请确认这是有意增删，"
        "而不是被一次批量改名带过去的。" % len(_FROZEN_ENV_KEYS)
    )
    rels = (
        set(_CLEAN_FILES)
        | {r[0] for r in _RUNTIME_OUT}
        | {r[0] for r in _HISTORY_KEEPS_LEGACY}
    )
    missing = [r for r in sorted(rels) if not os.path.isfile(os.path.join(_REPO_ROOT, r))]
    assert not missing, "守卫表指向了不存在的文件（改名/搬家没同步）：" + ", ".join(missing)
    assert _source_files(), "源码文件清单是空的（走盘没扫到东西），这一轮什么都没查"
    assert _code_files(), "代码面清单是空的，logger 处数那一条会变成 0 == 85 的空转"
    stale = [r for r in sorted(_RECORD_FILES) if not os.path.isfile(os.path.join(_REPO_ROOT, r))]
    assert not stale, (
        "流水账文件被改名/搬家了，_RECORD_FILES 里的排除项已经排除不到任何东西："
        + ", ".join(stale)
    )


def test_source_file_list_has_no_blind_spot_versus_git():
    """走盘清单不许漏掉任何一个受版本控制的源码文件。

    走盘换掉 `git ls-files` 解决了「在客户机器上跑不起来 / 少查」的问题，代价是
    目录白名单会过期：以后新开一个顶层目录、或往 _SKIP_DIRS 里多塞一个名字，
    守卫的射程就静静地缩一圈 —— 又是一次「看不见的少查」。所以在**有 git 的地方**
    （开发机、CI）反过来验一次：git 认的、磁盘上还在的，走盘必须一个不少。

    只验这一个方向。反方向（走盘扫到 git 不认的）在生产机上必然成立且无害 ——
    那台机器的索引本来就旧 51 个文件 —— 拿它当红线只会在用户机器上误红。
    """
    try:
        tracked = subprocess.run(
            ["git", "ls-files"], cwd=_REPO_ROOT,
            capture_output=True, text=True, check=True,
        ).stdout.split()
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("不在 git 工作区里（sdist/拷贝部署），没有可比对的基准")
    expected = {
        f for f in tracked
        if f and not f.startswith("tests/")
        and os.path.basename(f) not in {".env"}
        and not f.endswith(_SKIP_SUFFIXES)
        and _readable(f)
    }
    blind = sorted(expected - set(_source_files()))
    assert not blind, (
        "这些文件受版本控制、磁盘上也在，但走盘清单里没有 —— "
        "品牌守卫对它们完全失明：\n  " + "\n  ".join(blind)
    )


def test_logger_contract_site_count_is_pinned():
    """logger 名的处数必须一处不少 —— 少了就是被顺手清理过。"""
    total = sum(len(_LOGGER.findall(_read(f))) for f in _code_files())
    assert total == _LOGGER_SITES, (
        'getLogger("aiduMEM 的处数从 %d 变成了 %d。\n'
        "  这是机器契约：生产侧日志采集按 aiduMEM.* 过滤，改名后服务照常起、"
        "日志照常写，只是再也进不了采集管道。\n"
        "  若确实要迁移，须先改生产侧采集规则，再同步这个数字并记进 CHANGELOG。"
        % (_LOGGER_SITES, total)
    )


def test_env_key_set_is_frozen_and_new_vars_use_current_prefix():
    """AIDUMEM_* 键名集合必须与冻结集逐字相等。

    多出来 → 新变量用错了前缀（该用 AIDUMEI_）；
    少了 → 既有键被改名，所有老客户的 .env 在升级后静默失配。
    两个方向都要红，因为两个方向都不会自己报错。
    """
    found = set()
    for rel in _source_files():
        found |= set(_ENV_KEY.findall(_read(rel)))
    added = sorted(found - _FROZEN_ENV_KEYS - _TEST_ONLY_ENV_KEYS)
    removed = sorted(_FROZEN_ENV_KEYS - found)
    assert not added, (
        "新增了 AIDUMEM_* 前缀的变量：%s\n"
        "  新变量请用 AIDUMEI_ 前缀。旧前缀是为兼容既有部署冻结的，不是当前命名。" % added
    )
    assert not removed, (
        "以下 AIDUMEM_* 键名消失了：%s\n"
        "  客户的 .env 里已经写着这些键。键不匹配不会报错，只会静默回落到默认值 —— "
        "鉴权、租户隔离、身份映射都在这条路上出过事。" % removed
    )
    for key in _TEST_ONLY_ENV_KEYS:
        locations = {rel for rel in _source_files() if key in _read(rel)}
        assert locations <= {"conftest.py"}, (
            f"测试专用环境变量 {key} 扩散到了非 conftest.py 文件：{sorted(locations)}"
        )


# 测试框架自己的显式逃生门不是部署配置，不应被误当成客户的旧键；
# 它必须仍然只出现在根 conftest.py，不能扩散到产品代码或模板。
_TEST_ONLY_ENV_KEYS = {"AIDUMEM_TEST_ALLOW_REAL_DATA_DIR"}

@pytest.mark.parametrize(
    ("key", "consequence"), _CRITICAL_ENV_READS, ids=[k for k, _ in _CRITICAL_ENV_READS]
)
def test_critical_env_keys_are_still_read_from_environment(key, consequence):
    """高危键必须仍有真实读取点，而不只是在文档里被提到。

    只改读取处、不改文档，键名集合看起来一点没变 —— 这正是「静默失配」最会走的那条路。
    """
    pat = re.compile(r"(?:os\.getenv|environ\.get)\(\s*[\"']%s[\"']" % re.escape(key))
    sites = [rel for rel in _source_files() if pat.search(_read(rel))]
    assert sites, (
        "%s 在源码里已经没有读取点了。\n"
        "  后果：%s\n"
        "  客户的 .env 里写的还是这个键名；改读取处不会报错，只会静默拿默认值。" % (key, consequence)
    )


@pytest.mark.parametrize("rel", _CLEAN_FILES, ids=_CLEAN_FILES)
def test_user_facing_docs_carry_no_legacy_brand(rel):
    """用户会打开来读/编辑的文档与样例：整份不许再有旧品牌名。"""
    hits = [
        "%s:%d: %s" % (rel, i, ln.strip()[:80])
        for i, ln in enumerate(_read(rel).splitlines(), 1)
        if _LEGACY.search(ln)
    ]
    assert not hits, "用户会读到的文件里还留着旧品牌名：\n  " + "\n  ".join(hits)


@pytest.mark.parametrize(
    ("rel", "expected", "forbidden", "why"),
    _RUNTIME_OUT,
    ids=["%s::%s" % (r[0].rsplit("/", 1)[-1], r[1][:24]) for r in _RUNTIME_OUT],
)
def test_runtime_output_uses_current_brand(rel, expected, forbidden, why):
    """运行时露脸的字必须是当前品牌名（同一份文件里的注释/docstring 刻意仍是旧名）。"""
    src = _read(rel)
    assert expected in src, "%s 的运行时输出不是当前品牌名（%s）\n  期望包含：%s" % (
        rel,
        why,
        expected,
    )
    assert forbidden not in src, (
        "%s 又出现了旧品牌名 %s（%s）。\n"
        "  若是被一次全局替换改回去的：同一次替换很可能也动了机器契约，"
        "请一并看本文件的 logger 计数与环境变量冻结集两条。" % (rel, forbidden, why)
    )


@pytest.mark.parametrize(
    ("rel", "why"), _HISTORY_KEEPS_LEGACY, ids=[r[0] for r in _HISTORY_KEEPS_LEGACY]
)
def test_history_still_carries_legacy_brand(rel, why):
    """历史记录只新增不改写 —— 旧名必须还在。"""
    assert _LEGACY.search(_read(rel)), (
        "%s 里的旧品牌名被抹掉了（%s）。\n"
        "  品牌统一改的是「现在给人看的字」，不是把过去改成从来如此。" % (rel, why)
    )
