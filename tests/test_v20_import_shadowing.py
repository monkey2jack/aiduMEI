# -*- coding: utf-8 -*-
"""v20.0 守卫②：禁止「函数内 import 的名字与模块级 import 的名字同名」。

**这条守卫冻结的是一次真实事故的形状，不是一次代码大扫除。**

缺陷机制（Python 作用域规则，不是本项目的 bug）：一个名字只要在函数体里
被 ``import`` 过一次，它在**整个函数体**内就是局部名 —— 包括那行 import
**之前**的所有位置。于是模块级明明有 ``import json``，函数里靠后又写了一遍
``import json``，那么函数开头处的 ``json.dumps(...)`` 会抛
``UnboundLocalError``，而不是用模块级那个。静态看代码只会觉得「重复了一下，
无所谓」；跑起来才知道整条路径是死的。

已发生的事故（v19.4.3 → v20.0 之间，P0-2）：``ducky/hot/add.py`` 里多写了一行
函数内 ``import json``，``/add`` 接口连续 **13 分钟**（21:30:27 → 21:43:09）
返回 **195 次 HTTP 500**，靠 21:44:23 重启服务恢复。部署后 ``systemctl
is-active`` 一直是 active —— 进程活着不等于接口活着，所以这类缺陷不会被
「看进程」发现，只会被真实请求发现。

**射程内当前实测 0 处命中，这是有意的：**``ducky/`` 已经干净了（P0-2 的那行
已删）。这条守卫因此是**回归冻结**，防的是「同样的形状第三次长回来」，
不是在清扫活体缺陷。写清楚这一点，是为了让下一个人不要因为「它从来没红过」
而以为它没用 —— 它保护的是一次 13 分钟的线上事故不再发生。

**为什么这行不「微不足道」：**上一版的变更记录（``ducky/version.py`` 第 32 条、
``CHANGELOG.md`` 同一条）把删掉这行称作「本身微不足道」，理由是它只是个重复
import。那句话写下的时候，13 分钟、195 次 500 的日志已经躺在同一台机器上了 ——
**证伪证据一直在自己仓里，只是没人把两件事对上。**历史条目不改（只增不改是
铁律），但判断要在这里更正：在 Python 里，函数内重复 import 一个模块级已有的
名字，从来不是微不足道。

**判据本身踩过一次坑，记在这里：**第一版收集器用 ``ast.walk()`` 找模块级
import，结果它会钻进顶层 ``try:`` 里嵌着的 ``def`` —— 于是
``integrations/cursor-hook/claude-code-hook.py`` 里那个函数内 import 被算成了
「它自己的模块级绑定」，凭空多出一处命中。那个 import 是该名字**唯一**的绑定
（外层 try/except 就是为「文件被拷出仓库」准备的兜底），删掉会真的把兜底打断。
按未修正的判据建守卫，等于让守卫去红正确的代码 —— 假红灯骗人一辈子。
所以：**收集某一作用域内的 import，只能沿 if/try/with/for 的 body 走，
绝不下潜 FunctionDef/ClassDef/Lambda。**下面 ``_scope_local_imports`` 是唯一
一份实现，模块级和函数级共用它（换个 body 起点而已），不给「抄成两半」留口子。
"""

import ast
import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

_SKIP_PARTS = frozenset({
    "__pycache__", ".venv", "venv", "node_modules", ".git",
    "build", "dist", ".eggs", ".pytest_cache",
})

# 射程地板。实测射程内 207 个 .py（ducky 112 / tests 70 / scripts 9 /
# benchmarks 7 / 仓根 6 / integrations 2 / frontend 1）。给下限而不是等号，
# 是因为新增文件天天有，而「悄悄收窄」看不见：谁往 _SKIP_PARTS 多塞一项、
# 或把 rglob 换成某个子目录，守卫照样全绿，实际什么都没查。少查看不见，
# 所以给它一条看得见的地板。
_FILE_COUNT_FLOOR = 180

# 作用域节点：进到这里面，名字就是**另一个**作用域的事了，一律不下潜。
_SCOPED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _is_type_checking_test(node):
    """``if TYPE_CHECKING:`` / ``if typing.TYPE_CHECKING:``？

    这种块里的 import 在运行时**根本不绑定**，函数里再 import 同名是合法且
    必需的写法。仓里当前零处（实测），但这是标准写法，迟早会有 —— 先堵上，
    否则守卫会去红完全正确的代码。
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id == "TYPE_CHECKING":
            return True
        if isinstance(sub, ast.Attribute) and sub.attr == "TYPE_CHECKING":
            return True
    return False


def _scope_local_imports(body):
    """本作用域内的 import 语句。

    含顶层 ``if`` / ``try`` / ``with`` / ``for`` 里的（那些不开新作用域），
    但**不钻进 def/class/lambda** —— 钻进去就会把「函数内 import」当成模块级
    绑定，也就是本守卫要判的那个东西，判据当场自噬（见模块 docstring）。
    """
    out, stack = [], list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, _SCOPED):
            continue                                    # 关键：不下潜
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            out.append(node)
            continue
        if isinstance(node, ast.If) and _is_type_checking_test(node.test):
            stack.extend(n for n in node.orelse if isinstance(n, ast.AST))
            continue                                    # 只跳 body，else 是真会跑的
        for field in ("body", "orelse", "finalbody", "handlers"):
            value = getattr(node, field, None)
            if isinstance(value, list):
                stack.extend(n for n in value if isinstance(n, ast.AST))
    return out


def _bound_names(node):
    """一条 import 语句在当前作用域里**绑定**了哪些名字。

    ``import a.b.c`` 绑的是 ``a``；``import a.b as x`` 绑的是 ``x``；
    ``from m import *`` 绑什么静态不可知，只能放过。
    """
    names = set()
    if isinstance(node, ast.Import):
        for alias in node.names:
            names.add(alias.asname or alias.name.split(".")[0])
    elif isinstance(node, ast.ImportFrom):
        for alias in node.names:
            if alias.name != "*":
                names.add(alias.asname or alias.name)
    return names


def shadowing_sites(source):
    """返回 [(行号, 函数名, 被遮蔽的名字)]，按行号排序。

    对每一个函数作用域（含嵌套函数、方法），只看**它自己那一层**的 import，
    与模块级绑定的名字求交集。交集非空 = 该函数体内那个名字全程是局部名。
    """
    tree = ast.parse(source)
    module_names = set()
    for node in _scope_local_imports(tree.body):
        module_names |= _bound_names(node)

    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for imp in _scope_local_imports(node.body):
            for name in sorted(_bound_names(imp) & module_names):
                hits.append((imp.lineno, node.name, name))
    return sorted(hits)


def _iter_py_files():
    return sorted(
        p for p in _REPO_ROOT.rglob("*.py")
        if not (_SKIP_PARTS & set(p.relative_to(_REPO_ROOT).parts))
    )


def test_no_function_local_import_shadows_module_level_import():
    """全仓：函数内不得 import 一个模块级已经 import 过的名字。

    红了怎么修：**删掉函数内那一行**（模块级已经有了，函数里那行只会把名字
    变成局部名）。除非模块级那个绑定是有条件的 —— 那就该把模块级那行改成
    无条件，或者给函数内那行换个别名，而不是把这条守卫的射程改小。
    """
    offenders = []
    for path in _iter_py_files():
        rel = path.relative_to(_REPO_ROOT).as_posix()
        try:
            hits = shadowing_sites(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:                       # 语法坏了是另一条轴的事
            raise AssertionError(f"{rel} 无法解析：{exc}") from exc
        offenders += [f"{rel}:{line} 函数 {func}() 遮蔽了模块级的 {name}"
                      for line, func, name in hits]
    assert not offenders, (
        "函数内 import 遮蔽了模块级同名绑定（该名字在整个函数体内都是局部名，"
        "在那行 import 之前使用会抛 UnboundLocalError —— P0-2 就是这么炸的，"
        "/add 连续 13 分钟 195 次 500）：\n  " + "\n  ".join(offenders))


def test_shadow_classifier_bites_and_does_not_false_fire():
    """判据自证：该咬的咬得住，不该咬的一口都不咬。

    护栏自己不许有能力改变结果 —— 这条把三条轴钉在合成源码上，与仓里
    有没有对应形状无关，谁改判据都得先在这里红。
    """
    bites = shadowing_sites(
        "import json\n"
        "def f():\n"
        "    payload = json.dumps({})   # ← 这一行运行时就抛 UnboundLocalError\n"
        "    import json\n"
        "    return payload\n")
    assert bites == [(4, "f", "json")], f"该咬没咬住：{bites}"

    # 轴二：顶层 try 里嵌 def，函数内 import 是该名字**唯一**的绑定。
    # 这是 integrations/cursor-hook/claude-code-hook.py 的真实形状，
    # 用 ast.walk 收集模块级 import 就会在这里凭空造出一处命中。
    nested = shadowing_sites(
        "try:\n"
        "    from pkg.utils import helper\n"
        "    def wrap(key):\n"
        "        from pkg.utils import env_or_env_file\n"
        "        return env_or_env_file(key)\n"
        "except Exception:\n"
        "    def wrap(key):\n"
        "        return ''\n")
    assert nested == [], f"误咬了 try 里嵌的函数内 import：{nested}"

    # 轴三：if TYPE_CHECKING 块里的 import 运行时不绑定，函数里再 import 合法。
    type_checking = shadowing_sites(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    import sqlite3\n"
        "def g():\n"
        "    import sqlite3\n"
        "    return sqlite3\n")
    assert type_checking == [], f"误咬了 TYPE_CHECKING 块：{type_checking}"

    # 轴三反面：同一个 if，条件不是 TYPE_CHECKING 时照咬（别把整个 if 都放过）。
    conditional = shadowing_sites(
        "import os\n"
        "if os.name == 'nt':\n"
        "    import sqlite3\n"
        "def g():\n"
        "    import sqlite3\n"
        "    return sqlite3\n")
    assert conditional == [(5, "g", "sqlite3")], f"普通 if 不该被放过：{conditional}"


def test_shadow_guard_reach_is_not_silently_narrowed():
    """射程不许悄悄变小，且射程里确实有那两个形状。

    「守卫全绿」有两种成因：真干净，或者根本没查到。这条把两者分开。
    """
    files = _iter_py_files()
    assert len(files) >= _FILE_COUNT_FLOOR, (
        f"射程只剩 {len(files)} 个 .py（地板 {_FILE_COUNT_FLOOR}，实测基线 207）——"
        "排除表或遍历起点被收窄了，守卫会继续全绿但什么都没查")

    rels = {p.relative_to(_REPO_ROOT).as_posix() for p in files}
    for must in ("ducky/hot/add.py",                     # P0-2 案发现场
                 "tests/test_federation.py",             # 本轮清掉 2 处的地方
                 "integrations/cursor-hook/claude-code-hook.py"):
        assert must in rels, f"{must} 不在射程内 —— 这个位置必须被查"

    # 活体负向对照：那个「顶层 try 里嵌 def」的形状必须**仍然在仓里**，
    # 否则上面 test_no_... 的全绿就只是「刚好没有这种形状」，
    # 证明不了判据不下潜。形状没了就该改成合成用例兜底，而不是默默失去覆盖。
    hook = (_REPO_ROOT / "integrations/cursor-hook/claude-code-hook.py")
    tree = ast.parse(hook.read_text(encoding="utf-8"))
    nested_defs = [n for top in tree.body if isinstance(top, ast.Try)
                   for n in ast.walk(top)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and _scope_local_imports(n.body)]
    assert nested_defs, (
        "claude-code-hook.py 里「顶层 try 内嵌 def 且 def 内有 import」的形状不见了 ——"
        "它是判据「不下潜」的活体对照，形状消失就等于失去这条覆盖")
