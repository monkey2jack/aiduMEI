"""v20.0 S3③：把「旧品牌名出现在人读得到的字符串里」做成 AST 级普查守卫。

为什么不能再加一行清单就完事
────────────────────────────
`tests/test_v20_brand_visible_surface.py` 的 docstring 自己招认过病根：
`_user_visible_files()` 的射程是 `frontend/**` 加 `manifest.json`，
「集合静默排除了 frontend 之外**所有**给人看的字」。它把两处补进了清单 ——
补的是**已经知道的那两处**。清单钉不住第三处，因为清单只有人想起来才会长。

这条守卫换判据：射程是**仓里每个 .py 的每个非 docstring 字符串字面量**，
默认全部在管；刻意保留的旧名逐条列白名单，条条写理由。方向反过来了 ——
从「列出该改的」变成「列出不该改的」，漏一处的后果从「静默放过」变成「红灯」。

第一次跑就抓到 5 处清单从没射到的真残留（都已在本轮修掉）：

  1. `tests/integration_e2e_lifecycle.py:92`  `print("🧪 aiduMEM 端到端集成测试…")`
  2. 同上 :215                                `print("🎉 全部通过，aiduMEM 端到端 OK")`
  3. `tests/integration_smoke_api.py:228`     `print("🧪 aiduMEM API 烟雾测试")`
  4. `tests/perf_baseline.py:100`             `print("📊 aiduMEM 性能基线（…")`
  5. `ducky/federation/schema.py`             agents.description = "aiduMEM local primary agent"

前 4 处与 `scripts/health_check.py` 那句「🧠 aiduMEM 健康检查」是同一类 —— 那句在
v19.4.2 被认定为「露脸」并钉住了，这 4 句只是住在 `tests/` 里，于是躲过了所有清单。
住在哪个目录不改变它是 `print` 给人看的这件事。

第 5 处是真正给人看的**数据**：`GET /federation/agents` 的读路径是
`SELECT a.*`，`description` 原样回给调用方。它的修法额外记一笔 ——
seed 用的是 `INSERT OR IGNORE`，**对存量行无效**。只改 seed 等于「新装机才修好，
老用户看到的还是旧名」，正是审计骂过的「卖点没到生产」。所以配了一条按精确旧值
等值匹配的回填。这也是本文件白名单里 `_SEED_DESCRIPTION_LEGACY` 那条的来历：
旧名必须在代码里留一份，否则回填无从匹配。

判据（哪些旧名是刻意留的）
──────────────────────────
按品牌 VI：机器认的键一个都不动，人读到的名字才改。可机械判定的两种机器键形状：

  * **logger 点分名** `^aiduMEM(\\.[A-Za-z0-9_]+)+$` —— 生产日志采集按 `aiduMEM.*` 过滤；
  * **连字符机器 id** `^aiduMEM-[A-Za-z0-9_.\\-]*$` —— `/health` 的 `service=aiduMEM-v*`
    （生产监控按它匹配，v19.4.2 有人改过一次，告警从那一刻起安静失配）、
    线程名 `aiduMEM-fts-backfill` 等、以及历史白皮书文件名 `aiduMEM-v11-…md`。

  ⚠️ 这条形状规则**顺带**收了历史文档文件名，比「机器 id」宽。是有意接受的：
  那些文件名同样一个字都不能改，红它们只会制造噪音。代价是「新加一个
  `aiduMEM-` 开头的人读文案」不会被抓 —— 记在这里，别以为它是全覆盖。

docstring 一律不在射程内：`ducky/version.py:441` 定过「各模块 docstring 里的
aiduMEM 一律不动 —— 机器契约与历史内部名，生产监控按其匹配」。

白名单必须条条有用
──────────────────
`test_every_exemption_is_still_earning_its_place` 会反过来断言：每条白名单都仍然
命中至少一处真实字面量。否则白名单会长成坟场 —— 早年豁免的位点改掉了、豁免条目
还留着，下一次同类残留落在那个位置就被无声放过。豁免过期即报废，必须删。
"""

import ast
import pathlib
import re

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

_SKIP_PARTS = frozenset({
    "__pycache__", ".venv", "venv", "node_modules", ".git",
    "build", "dist", ".eggs", ".pytest_cache",
})

# 实测射程 209 个 .py（tests 72 / ducky 112 / scripts 9 / benchmarks 7 /
# <root> 6 / integrations 2 / frontend 1）。取 180 作地板：射程被谁悄悄收窄了
# 会红在这里，而不是让「命中 0 处」冒充干净 —— 少查看不见。
_FILE_COUNT_FLOOR = 180

_LEGACY_MARKS = ("aiduMEM", "duMem", "DuMem")

_LOGGER_NAME_RE = re.compile(r"^aiduMEM(\.[A-Za-z0-9_]+)+$")
_HYPHEN_ID_RE = re.compile(r"^aiduMEM-[A-Za-z0-9_.\-]*$")

# ── 按文件豁免：这些文件的正业就是谈品牌本身 ────────────────────────────
# 它们的字面量是**判据与报错文案**，不是产品文案。列在这里而不是靠
# 「跳过 tests/ 目录」—— 因为 tests/ 里恰恰有 4 处真残留（见 docstring）。
_BRAND_DISCOURSE_FILES = {
    "tests/test_v19_4_2_brand_surface.py": "v19.4.2 品牌门面守卫，字面量是它的判据",
    "tests/test_v20_brand_policy.py": "v20 品牌政策守卫，内含各面清单与报错文案",
    "tests/test_v20_brand_visible_surface.py": "v20 可见面守卫，正反两向清单都在里面",
    "tests/test_v20_brand_literal_census.py": "本文件；白名单与合成用例里必然带旧名",
}

# ── 按（文件, 精确字面量）豁免：一次性值，逐条写清为什么必须留旧名 ──────
_ALLOWED_LITERALS = {
    ("ducky/federation/schema.py", "aiduMEM local primary agent"):
        "_SEED_DESCRIPTION_LEGACY：存量行回填的等值匹配针，删了老库就改不动",
    ("scripts/consolidator.py", "aiduMEM 记忆 升级 配置"):
        "拿去检索**存量记忆正文**的 query 词；存量记忆里写的是旧名，改了掉召回",
    ("tests/test_federation.py", "今天决定把 aiduMEM 升到联邦架构"):
        "模拟一条老记忆的 fixture 文本，老记忆本就该带老名",
}


def _docstring_constant_ids(tree):
    """所有 docstring 的 Constant 节点 id —— docstring 按既有决策不在射程内。"""
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                ids.add(id(body[0].value))
    return ids


def _is_machine_key(value):
    """可机械判定的机器键形状；判据见模块 docstring。"""
    if _LOGGER_NAME_RE.match(value) or _HYPHEN_ID_RE.match(value):
        return True
    # 同时含新旧两代 = 全代际正则 / 演进史叙述，改一半只会得到废话
    if "aiduMEI" in value and "aiduMEM" in value:
        return True
    return False


def legacy_literals(source):
    """(行号, 字面量) —— 非 docstring 且含旧品牌名的字符串字面量。"""
    tree = ast.parse(source)
    skip = _docstring_constant_ids(tree)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in skip:
            continue
        if any(mark in node.value for mark in _LEGACY_MARKS):
            out.append((node.lineno, node.value))
    return sorted(out)


def _iter_py_files():
    return sorted(
        p for p in _REPO_ROOT.rglob("*.py")
        if not (_SKIP_PARTS & set(p.relative_to(_REPO_ROOT).parts))
    )


def _offenders():
    """全仓普查：既非机器键、又不在白名单里的旧名字面量。"""
    bad = []
    for path in _iter_py_files():
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if rel in _BRAND_DISCOURSE_FILES:
            continue
        try:
            lits = legacy_literals(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            raise AssertionError(f"{rel} 无法解析：{exc}") from exc
        for lineno, value in lits:
            if _is_machine_key(value):
                continue
            if (rel, value) in _ALLOWED_LITERALS:
                continue
            bad.append((rel, lineno, value))
    return bad


def test_no_legacy_brand_literal_in_human_readable_position():
    """旧品牌名不得出现在人读得到的字符串字面量里。"""
    bad = _offenders()
    detail = "\n  ".join(f"{r}:{n}  {v!r}" for r, n, v in bad)
    assert not bad, (
        "发现旧品牌名出现在人读得到的字符串里：\n  " + detail + "\n\n"
        "按品牌 VI：人读到的名字（print 输出、界面文案、报错提示、回给 API 调用方的\n"
        "数据字段）用 aiduMEI；机器认的键（logger 点分名、aiduMEM-* 机器 id、\n"
        "AIDUMEM_* 环境变量、docstring）一个都不动。\n"
        "若这一处确属刻意保留，往 _ALLOWED_LITERALS 加一条并写清理由；\n"
        "不要改判据，也不要把整个文件塞进 _BRAND_DISCOURSE_FILES。"
    )


@pytest.mark.parametrize(
    "axis, source, expect_hit",
    [
        ("人读文案", 'msg = "aiduMEM 已就绪"\n', True),
        ("logger 点分名", 'log = getLogger("aiduMEM.hot.add")\n', False),
        ("连字符机器 id", 't = Thread(name="aiduMEM-fts-backfill")\n', False),
        ("service 前缀 f-string", 'svc = f"aiduMEM-v{ver}"\n', False),
        ("模块 docstring", '"""aiduMEM Checkpoint — 5 段会话快照"""\n', False),
        ("函数 docstring", 'def f():\n    """委托给 aiduMEM-v7 混合召回"""\n    return 1\n', False),
        ("全代际正则", 'R = re.compile(r"(aiduMEI|aiduMEM|duMem)")\n', False),
        ("人读文案含 duMem", 'print("欢迎使用 duMem")\n', True),
    ],
)
def test_classifier_bites_only_on_human_readable(axis, source, expect_hit):
    """判据的正反两向：该红的红，不该红的一处都不许红。"""
    lits = legacy_literals(source)
    hit = [v for _, v in lits if not _is_machine_key(v)]
    assert bool(hit) is expect_hit, f"轴「{axis}」判据走偏：命中={hit}"


def test_every_exemption_is_still_earning_its_place():
    """白名单条条必须仍然命中；过期豁免即报废，必须删。

    否则白名单会长成坟场 —— 位点早改掉了、豁免还留着，下一处同类残留
    落在那个位置就被无声放过。
    """
    stale_files = [
        rel for rel in _BRAND_DISCOURSE_FILES
        if not (_REPO_ROOT / rel).exists()
        or not legacy_literals((_REPO_ROOT / rel).read_text(encoding="utf-8"))
    ]
    assert not stale_files, (
        f"这些文件已不含旧品牌名字面量，按文件豁免已过期，请从 "
        f"_BRAND_DISCOURSE_FILES 删除：{stale_files}"
    )

    stale_literals = []
    for (rel, value), _why in _ALLOWED_LITERALS.items():
        path = _REPO_ROOT / rel
        if not path.exists():
            stale_literals.append((rel, value, "文件不存在"))
            continue
        found = [v for _, v in legacy_literals(path.read_text(encoding="utf-8")) if v == value]
        if not found:
            stale_literals.append((rel, value, "该字面量已不在文件里"))
    assert not stale_literals, (
        "这些白名单条目已经过期（豁免了不存在的东西），请删除：\n  "
        + "\n  ".join(f"{r}  {v!r}  —— {why}" for r, v, why in stale_literals)
    )


def test_census_reach_is_not_silently_narrowed():
    """射程别被悄悄收窄，且几个关键位点必须真的在射程内。"""
    files = _iter_py_files()
    assert len(files) >= _FILE_COUNT_FLOOR, (
        f"普查射程只剩 {len(files)} 个 .py（地板 {_FILE_COUNT_FLOOR}）。"
        "射程缩了会让「命中 0 处」冒充干净。"
    )
    rels = {p.relative_to(_REPO_ROOT).as_posix() for p in files}
    for must in (
        "ducky/federation/schema.py",     # 回给调用方的 description
        "tests/integration_smoke_api.py",  # 住在 tests/ 里的 print banner
        "scripts/health_check.py",         # v19.4.2 认定过的「露脸」文件
        "api_server.py",                   # 仓根文件也在射程内
    ):
        assert must in rels, f"{must} 不在普查射程内，守卫的全绿没有意义"

    # 活体负向对照：机器键必须**确实还存在**于仓里。若哪天 logger 名全被改成
    # 新品牌（那会打断生产日志采集），这条会红 —— 而不是让「机器键分支从未
    # 被走到」的判据带着一句空话继续全绿。
    machine = [
        v for p in files
        for _, v in legacy_literals(p.read_text(encoding="utf-8"))
        if _is_machine_key(v)
    ]
    assert len(machine) >= 50, (
        f"仓里可机械判定的旧名机器键只剩 {len(machine)} 处（预期 ≥50）。"
        "logger 点分名 / aiduMEM-* 机器 id 是刻意保留的生产契约，"
        "大面积消失说明有人把机器键当品牌残留清了。"
    )
