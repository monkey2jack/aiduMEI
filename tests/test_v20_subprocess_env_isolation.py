# -*- coding: utf-8 -*-
"""v20.0 护栏：测试起子进程时，数据落点必须钉死，不许就地写树。

**这道护栏冻结的是一次真实事故的形状。**

``tests/test_v19_4_2_identity_env_fallback.py`` 里的 ``_empty_env()`` 故意造一个
近乎空的环境 —— 这是对的，它测的就是「网关/cron 用空环境拉起子进程时，身份还认不认」。
但它顺带漏掉了 ``AIDUMEM_DATA_DIR``，而 ``ducky/utils.py`` 里 DATA_DIR 是 import
时算的、算不出就退到 ``BASE_DIR/data`` 并**当场 makedirs 建库**。于是：

    子进程继承 pytest 的 cwd → 一 import ducky 就在 cwd 里建出
    facts.db / salience.db / workspace.db 三个库。

在沙箱里这无害（写进沙箱自己）。可 cwd 若是生产部署树，这三个库就直接开在它的
**活数据目录**里了 —— 一次「只读的测试」变成了对生产库的写操作。

发现它靠的不是推理，是实测：清掉沙箱的 ``data/`` → 单跑那个文件 → ``data/`` 带三个
库回来了；再清掉 → 跑一个无关文件 → 没回来。**负向对照是把嫌疑钉到具体文件上的
唯一办法。**

同一轮还纠正了我自己一句话的分量：跑测前后比「生产数据目录的路径集合」，只能证明
**没增删**，证明不了**内容没被改**。路径集合相等不是内容相等。

判据取「**显式给 PATH 的字典字面量，就是手搓的进程环境**」—— 你只会在从零造 env
的时候才去写 PATH。这个签名跟具体哪个文件、哪个 helper 无关，是机制盲的。
"""

import ast
import pathlib

_TESTS_DIR = pathlib.Path(__file__).resolve().parent
_SUBPROCESS_FUNCS = {"run", "Popen", "check_output", "check_call", "call"}


def _test_files():
    return sorted(p for p in _TESTS_DIR.glob("test_*.py"))


def _dict_keys(node):
    """字典字面量的常量键集合；``**xxx`` 展开的键在 AST 里是 None。"""
    return {k.value for k in node.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)}


def _has_splat(node):
    return any(k is None for k in node.keys)


def _is_handbuilt_env(node):
    """手搓进程环境的签名：显式写了 PATH，且**没有**摊 ``**os.environ``。

    只有从零造 env 的时候才会去写 PATH。摊了 os.environ 的写法是安全的另一种 ——
    它跟跑测进程同环境，跑测进程本来就在哪写哪（这正是规则二认的写法），所以豁免。
    这条豁免不是让路：它是这道护栏第一次跑就自己咬出来的误伤 —— 仓里有两处
    ``{**os.environ, ..., "PATH": ...}``，被当成手搓 env 报了假红灯。
    """
    return (isinstance(node, ast.Dict)
            and "PATH" in _dict_keys(node)
            and not _has_splat(node))


def _handbuilt_env_offenders(tree, label):
    """规则一：手搓的 env 字典必须钉 AIDUMEM_DATA_DIR。"""
    bad = []
    for node in ast.walk(tree):
        if _is_handbuilt_env(node) and "AIDUMEM_DATA_DIR" not in _dict_keys(node):
            bad.append(f"{label}:{node.lineno}")
    return bad


def _subprocess_env_offenders(tree, label):
    """规则二：subprocess.*(env={字面量}) 要么摊 os.environ，要么钉 AIDUMEM_DATA_DIR。"""
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if not (isinstance(f, ast.Attribute) and f.attr in _SUBPROCESS_FUNCS
                and isinstance(f.value, ast.Name) and f.value.id == "subprocess"):
            continue
        for kw in node.keywords:
            if kw.arg != "env" or not isinstance(kw.value, ast.Dict):
                continue
            if _has_splat(kw.value):
                continue
            if "AIDUMEM_DATA_DIR" not in _dict_keys(kw.value):
                bad.append(f"{label}:{kw.value.lineno}")
    return bad


def _scan(rule):
    hits = []
    for path in _test_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hits.extend(rule(tree, path.name))
    return hits


def test_handbuilt_subprocess_env_pins_data_dir():
    """手搓的进程环境不许漏掉数据落点 —— 漏了就会就地建库。"""
    bad = _scan(_handbuilt_env_offenders)
    assert not bad, (
        "以下位置手搓了进程环境（显式给了 PATH）却没钉 AIDUMEM_DATA_DIR：\n  "
        + "\n  ".join(bad)
        + "\n子进程一 import ducky 就会在 cwd 里建 facts/salience/workspace 三个库；"
          "cwd 若是生产部署树，就是对活数据目录的写操作。"
          "把 AIDUMEM_DATA_DIR（和 AIDUMEM_LOG_DIR）指到 tmp_path 即可 —— "
          "这与身份类用例要测的东西无关，不会削弱断言。"
    )


def test_subprocess_env_literals_inherit_or_pin():
    """直接内联给 subprocess 的 env 字面量，同样要么继承要么钉。"""
    bad = _scan(_subprocess_env_offenders)
    assert not bad, (
        "以下 subprocess 调用内联了 env 字面量，既没摊 **os.environ 也没钉 "
        "AIDUMEM_DATA_DIR：\n  " + "\n  ".join(bad)
    )


def test_census_reach_is_not_silently_vacuous():
    """普查必须真的扫到东西 —— 否则哪天判据写错了，也是一片绿。

    两条规则各自的「本该命中的样本数」在这里点名：仓里确实存在显式给 PATH 的
    手搓 env（``_empty_env``），也确实存在内联 env 字面量的 subprocess 调用。
    若这两个计数掉到 0，说明扫描器瞎了，而不是仓里变干净了。
    """
    path_dicts = 0
    inline_envs = 0
    for path in _test_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if _is_handbuilt_env(node):
                path_dicts += 1
            if isinstance(node, ast.Call):
                f = node.func
                if (isinstance(f, ast.Attribute) and f.attr in _SUBPROCESS_FUNCS
                        and isinstance(f.value, ast.Name) and f.value.id == "subprocess"):
                    for kw in node.keywords:
                        if kw.arg == "env" and isinstance(kw.value, ast.Dict):
                            inline_envs += 1
    assert path_dicts >= 1, "一处手搓 env（给 PATH 且不摊 os.environ）都没扫到 —— 扫描器瞎了"
    assert inline_envs >= 1, "一处内联 env 字面量都没扫到 —— 扫描器瞎了"


def test_both_classifiers_bite_on_synthetic_samples():
    """负向对照：喂人造坏样本，两条规则都必须咬；喂好样本，都必须放过。"""
    bad_handbuilt = '''
import subprocess
def f(tmp_path):
    env = {"PATH": "/usr/bin", "HOME": "/nowhere"}
    subprocess.run(["true"], env=env)
'''
    good_handbuilt = '''
import subprocess
def f(tmp_path):
    env = {"PATH": "/usr/bin", "AIDUMEM_DATA_DIR": str(tmp_path)}
    subprocess.run(["true"], env=env)
'''
    bad_inline = '''
import subprocess
subprocess.run(["true"], env={"FOO": "1"})
'''
    good_inline_splat = '''
import os, subprocess
subprocess.run(["true"], env={**os.environ, "FOO": "1"})
'''
    good_inline_pinned = '''
import subprocess
subprocess.run(["true"], env={"AIDUMEM_DATA_DIR": "/tmp/x"})
'''
    assert _handbuilt_env_offenders(ast.parse(bad_handbuilt), "样本") , \
        "规则一放过了漏钉数据落点的手搓 env —— 判据是恒真的，白护栏"
    good_splat_with_path = '''
import os, subprocess
subprocess.run(["true"], env={**os.environ, "PATH": os.environ.get("PATH", "")})
'''
    assert not _handbuilt_env_offenders(ast.parse(good_handbuilt), "样本"), \
        "规则一误伤了已经钉好的手搓 env"
    assert not _handbuilt_env_offenders(ast.parse(good_splat_with_path), "样本"), \
        "规则一误伤了『摊 os.environ 又冗余写 PATH』的写法 —— 这正是它第一次跑时的误伤"
    assert _subprocess_env_offenders(ast.parse(bad_inline), "样本"), \
        "规则二放过了裸 env 字面量 —— 判据是恒真的，白护栏"
    assert not _subprocess_env_offenders(ast.parse(good_inline_splat), "样本"), \
        "规则二误伤了摊 os.environ 的写法"
    assert not _subprocess_env_offenders(ast.parse(good_inline_pinned), "样本"), \
        "规则二误伤了显式钉好的写法"


# ---------------------------------------------------------------- 规则三：家目录
# 跑测不许写进真实 $HOME。v20.0 生产机踏勘发现 backup_gate 那七条用例一直在
# `~/.aidumem_test_backups` 下建目录 —— 它自清、也命了名，但它在沙箱外，
# 「沙箱隔离」这句话对它本来就是假的。理由本身是正当的（备份门禁铁律拒绝 /tmp
# 系备份根），所以判据不是「禁止碰 home」，而是「碰 home 必须可被环境变量改道」。
#
# 判据一律走 AST，不走字符串：这条规则的第一版数的是 `"AIDUMEM_" in 源码段`，
# 结果撤掉改道口、只留下提到它的 docstring，护栏照样放行 —— 数提及不是数位点。


_ENV_PREFIXES = ("AIDUMEM_", "AIDUMEI_")


def _is_environ(node):
    """认得 os.environ 与裸 environ"""
    return (isinstance(node, ast.Attribute) and node.attr == "environ") or (
        isinstance(node, ast.Name) and node.id == "environ"
    )


def _first_str_arg(call):
    if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
        return call.args[0].value
    return None


def _refs_home(node):
    """函数体里是否真的取了家目录（Path.home() / expanduser / environ["HOME"]）"""
    for n in ast.walk(node):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            if n.func.attr in {"home", "expanduser"}:
                return True
            if n.func.attr == "get" and _is_environ(n.func.value) and _first_str_arg(n) == "HOME":
                return True
        if isinstance(n, ast.Subscript) and _is_environ(n.value):
            if isinstance(n.slice, ast.Constant) and n.slice.value == "HOME":
                return True
    return False


def _reads_aidumem_env(node):
    """函数体里是否真的读了某个 AIDUMEM_/AIDUMEI_ 环境变量（注释里提到不算）

    两个前缀都认：AIDUMEM_ 是为兼容既有部署冻结的旧前缀，AIDUMEI_ 是当前命名。
    """
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            attr = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
            if attr == "getenv" or (attr == "get" and isinstance(f, ast.Attribute) and _is_environ(f.value)):
                s = _first_str_arg(n)
                if s and s.startswith(_ENV_PREFIXES):
                    return True
        if isinstance(n, ast.Subscript) and _is_environ(n.value):
            if isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str):
                if n.slice.value.startswith(_ENV_PREFIXES):
                    return True
    return False


def _home_offenders(tree, label):
    """返回「真取了家目录、却没读任何 AIDUMEM_/AIDUMEI_ 改道项」的函数名"""
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if _refs_home(node) and not _reads_aidumem_env(node):
            bad.append(f"{label}::{node.name}")
    return bad


def test_home_writes_are_env_overridable():
    """任何摸家目录的测试辅助函数，都必须留一个 AIDUMEI_* 改道口"""
    offenders = []
    for path in _test_files():
        offenders += _home_offenders(ast.parse(path.read_text(encoding="utf-8")), path.name)
    assert not offenders, (
        "这些函数把跑测产物写进真实 $HOME，且无法用环境变量改道：\n  "
        + "\n  ".join(offenders)
        + "\n沙箱跑测时它们会越界写到用户家里 —— 请读一个 AIDUMEI_* 覆盖项。"
    )


def test_home_census_reach_is_not_silently_vacuous():
    """普查必须真的看见了家目录引用，否则规则三是空转的"""
    seen = [
        f"{path.name}::{node.name}"
        for path in _test_files()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _refs_home(node)
    ]
    assert seen, (
        "全测试目录里一处家目录引用都没数到 —— 规则三恒真，是白护栏。"
        "要么写法变了（比如改用 os.path.expandvars），要么 _refs_home 该更新了。"
    )


def test_home_classifier_bites_on_synthetic_samples():
    """负向对照：裸 home 必被咬；只在注释里提改道口的，也必须被咬"""
    bad_home = """
import pathlib
def helper(tmp_path):
    return pathlib.Path.home() / ".stuff" / tmp_path.name
"""
    bad_comment_only = """
import pathlib
def helper(tmp_path):
    \"\"\"根位置可用 AIDUMEI_TEST_BACKUP_HOME 覆盖。\"\"\"
    # os.environ.get("AIDUMEI_TEST_BACKUP_HOME") 早年在这里
    return pathlib.Path.home() / ".stuff" / tmp_path.name
"""
    good_home = """
import os, pathlib
def helper(tmp_path):
    base = os.environ.get("AIDUMEI_TEST_BACKUP_HOME")
    home = pathlib.Path(base) if base else pathlib.Path.home()
    return home / ".stuff" / tmp_path.name
"""
    good_expanduser_pinned = """
import os
def helper():
    return os.path.expanduser(os.environ.get("AIDUMEI_TEST_BACKUP_HOME", "~"))
"""
    no_home = """
def helper(tmp_path):
    return tmp_path / "data"
"""
    assert _home_offenders(ast.parse(bad_home), "样本"), \
        "规则三放过了裸 Path.home() —— 判据恒假，白护栏"
    assert _home_offenders(ast.parse(bad_comment_only), "样本"), \
        "规则三被注释和 docstring 骗过去了 —— 这正是它第一版的漏洞：数提及不是数位点"
    assert not _home_offenders(ast.parse(good_home), "样本"), \
        "规则三误伤了留了改道口的写法"
    assert not _home_offenders(ast.parse(good_expanduser_pinned), "样本"), \
        "规则三误伤了 expanduser + 改道口的写法"
    assert not _home_offenders(ast.parse(no_home), "样本"), \
        "规则三误伤了压根不碰 home 的写法"
