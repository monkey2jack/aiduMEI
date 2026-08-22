"""跳过轴普查（v20.0 新增）。

## 为什么要有这个文件

v20.0 之前，README 里写着一句绝对话：

    | 完整环境 | **833 全绿**（Hermes 源码在场时 12 个跳过项全部执行并通过，生产实跑核验） |

它错了，而且是**被它自己引用的那次生产实跑**证伪的：部署树上宿主明明在场，
跑出来是 `832 passed, 1 skipped`。根因不是数字手滑，是**模型缺了三条轴** ——
当时以为「跳过」只有宿主 Hermes 源码这一条成因，于是把「装上宿主」等同于「全绿」。
实际有四条互不相干的轴，两台机器各缺一条，`833` 从来没有任何一台机器跑出来过。

更难堪的是：证伪它的记录**早就躺在自己仓库里**。CHANGELOG 和 version.py 白纸黑字
写着「生产 1 skipped」，README 同期宣称「0 skipped」，两句话隔着几百行互相矛盾，
跨了好几个版本一次没红过 —— 因为文档数字守卫的射程覆盖 README↔README_EN，
**从没覆盖 README↔CHANGELOG**。守卫自己 docstring 点名的病（「守卫的射程小于
缺陷的分布」），第二次长在守卫自己身上。

## 这个文件守什么

1. **轴的完整性**：普查 tests/ 里所有跳过机制，文件集合必须与下面的登记表逐一对上。
   有人加了第五条轴而没登记 → 当场红。
2. **每条轴的门控条数是活测的**：整份跳过的轴用 `--collect-only` 数，
   调用点跳过的轴用 AST 数所在测试函数。README 上的 12/1/7/1 必须与实测相等。
3. **推导值必须自曝**：`833` 只能以「推导值 / 从未实测」的措辞出现，
   一旦有人把它写回「生产实跑核验」，这里立刻红。

普查用**机制盲**的正则，不是按文件名猜 —— 上一版栽的第二个跟头就在这：
`grep 'pytest.skip|skipif'` 漏掉了 `unittest.skipIf`，而那正是 12 条主力跳过的写法。
"""

import ast
import functools
import io
import pathlib
import re
import subprocess
import sys
import tokenize

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
_TESTS_DIR = _REPO_ROOT / "tests"

# 机制盲的普查正则：把「让用例不执行」的所有写法都网进来，不预设它长什么样。
_SKIP_MECHANISM = re.compile(
    r"pytest\.skip\(|pytest\.importorskip\(|pytest\.mark\.skipif|"
    r"unittest\.skipIf|@skipIf|skipif\("
)

# ── 四条轴的登记表 ────────────────────────────────────────────────────────
# scope="file"     整份文件被跳过（模块级 pytestmark / 类级 skipIf）
# scope="callsite" 只跳所在的那个测试函数
# doc_zh / doc_en  README 里那张轴表的行首标签，用来把文档数字钉到实测值上
_AXES = (
    {
        "key": "hermes_host",
        "file": "test_hermes_plugin.py",
        "scope": "file",
        "doc_zh": "宿主 Hermes 源码",
        "doc_en": "Host Hermes source",
    },
    {
        "key": "git_worktree",
        "file": "test_v20_brand_policy.py",
        "scope": "callsite",
        "doc_zh": "git 工作区",
        "doc_en": "git worktree",
    },
    {
        "key": "backup_gate_posix",
        "file": "test_v19_4_1_backup_gate.py",
        "scope": "file",
        "doc_zh": "`scripts/backup_gate.sh` + POSIX shell",
        "doc_en": "`scripts/backup_gate.sh` + POSIX shell",
    },
    {
        "key": "qdrant_client",
        "file": "test_v20_vector_bank_contract.py",
        "scope": "callsite",
        "doc_zh": "`qdrant_client` 已安装",
        "doc_en": "`qdrant_client` installed",
    },
)


def _read_doc(name):
    return pathlib.Path(_REPO_ROOT, name).read_text(encoding="utf-8")


@functools.lru_cache(maxsize=1)
def _collect_lines():
    """跑一次真收集，返回 `文件名::用例` 那些行。"""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q",
         "-p", "no:cacheprovider"],
        cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=600,
    )
    lines = [ln.strip() for ln in proc.stdout.splitlines() if "::" in ln]
    assert lines, f"收集不到任何用例，pytest 退出码 {proc.returncode}"
    return tuple(lines)


def _cases_in_file(fname):
    prefix = f"tests/{fname}::"
    return len([ln for ln in _collect_lines() if ln.startswith(prefix)])


@functools.lru_cache(maxsize=128)
def _code_only_lines(path_str):
    """把字符串字面量和注释抹成等长空白，只留真正会执行的代码。行号不变。

    🔴v20.0：不这么做，普查第一件事就是扫到**它自己** —— 本文件的
    `_SKIP_MECHANISM` 正则里逐字写着各种跳过写法，纯文本扫描会把
    「描述跳过机制的字符串」误判成「这个文件真的跳过了」。

    抹掉字面量之后仍然是**机制盲**的：任何写法的跳过语句照样被正则逮住，
    只是「在一句话里提到它」不再算数。行号必须原样保留 ——
    调用点那条轴要靠行号跟 AST 的函数区间对齐。
    """
    src = pathlib.Path(path_str).read_text(encoding="utf-8")
    lines = src.split("\n")
    blank = {tokenize.STRING, tokenize.COMMENT}
    fstr_mid = getattr(tokenize, "FSTRING_MIDDLE", None)   # 3.12 起 f-string 单独分词
    if fstr_mid is not None:
        blank.add(fstr_mid)
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return lines            # 词法都过不了：宁可多报，绝不漏报
    for tok in toks:
        if tok.type not in blank:
            continue
        (r1, c1), (r2, c2) = tok.start, tok.end
        for r in range(r1, r2 + 1):
            i = r - 1
            if i >= len(lines):
                break
            lo = c1 if r == r1 else 0
            hi = c2 if r == r2 else len(lines[i])
            lines[i] = lines[i][:lo] + " " * (hi - lo) + lines[i][hi:]
    return lines


def _skip_sites(fname):
    """返回该文件里所有跳过机制的行号（只看会执行的代码）。"""
    lines = _code_only_lines(str(pathlib.Path(_TESTS_DIR, fname)))
    return [i for i, ln in enumerate(lines, 1)
            if _SKIP_MECHANISM.search(ln)]


def _callsite_gated_cases(fname):
    """调用点跳过：数「包含跳过语句的测试函数」有几个（AST 实测，不数行）。"""
    path = pathlib.Path(_TESTS_DIR, fname)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    sites = set(_skip_sites(fname))
    gated = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("test_"):
                continue
            lo, hi = node.lineno, getattr(node, "end_lineno", node.lineno)
            if any(lo <= s <= hi for s in sites):
                gated.add(node.name)
    return len(gated)


@functools.lru_cache(maxsize=1)
def _measured_axis_counts():
    """每条轴实际门控了几条用例 —— 全部现场测，登记表里不放数字。"""
    out = {}
    for axis in _AXES:
        if axis["scope"] == "file":
            out[axis["key"]] = _cases_in_file(axis["file"])
        else:
            out[axis["key"]] = _callsite_gated_cases(axis["file"])
    return out


def test_skip_axis_census_matches_registry():
    """tests/ 里出现跳过机制的文件，必须与登记表完全一致。

    多一个 → 有人加了第五条轴却没写进 README，「全绿」的含义又变了没人知道；
    少一个 → 某条轴被删了，README 上那一行成了描述不存在之物的死文案。
    两个方向都必须红。
    """
    found = {p.name for p in sorted(_TESTS_DIR.glob("*.py"))
             if any(_SKIP_MECHANISM.search(ln) for ln in _code_only_lines(str(p)))}
    registered = {axis["file"] for axis in _AXES}

    unregistered = found - registered
    assert not unregistered, (
        f"发现未登记的跳过轴：{sorted(unregistered)} —— "
        "跳过轴一变，README 里「全轴齐备」那一行的前提就变了，必须同步登记并改文档"
    )
    vanished = registered - found
    assert not vanished, (
        f"登记表里这些轴在源码里已经找不到了：{sorted(vanished)} —— "
        "README 上对应的行成了死文案"
    )


def test_every_axis_actually_gates_at_least_one_case():
    """登记的每条轴都必须真的门控着用例，否则它就是一行自我安慰的注释。"""
    counts = _measured_axis_counts()
    for axis in _AXES:
        assert counts[axis["key"]] > 0, (
            f"轴「{axis['doc_zh']}」实测门控 0 条用例 —— 登记表与现实脱节"
        )


def test_readme_axis_table_numbers_match_measurement():
    """两份 README 里那张轴表的门控条数，必须等于现场实测值。

    这是全篇的要害：上一版正是因为 `0` 是硬编码的、没人现场测，
    才让一句被证伪的话在文档里躺了好几个版本。
    """
    counts = _measured_axis_counts()
    for axis in _AXES:
        for fname, label_key in (("README.md", "doc_zh"),
                                 ("README_EN.md", "doc_en")):
            label = re.escape(axis[label_key])
            m = re.search(rf"\|\s*{label}\s*\|\s*(\d+)\s*\|", _read_doc(fname))
            assert m, (
                f"{fname} 的跳过轴表里找不到「{axis[label_key]}」这一行 —— "
                "轴表被删或措辞被改，守卫失去着力点"
            )
            got = int(m.group(1))
            assert got == counts[axis["key"]], (
                f"{fname}「{axis[label_key]}」写着门控 {got} 条，"
                f"实测 {counts[axis['key']]} 条 —— 文档与代码脱节（宣称即承诺铁律）"
            )


def test_all_axes_number_is_labelled_as_derived_not_measured():
    """「全轴齐备 833 全绿」必须明说是推导值，不许再宣称生产核验过。"""
    zh = _read_doc("README.md")
    assert "从未实测" in zh, (
        "README.md 未标注「全轴齐备」那个数字是推导值 —— "
        "上一版就是把它写成「生产实跑核验」才被自己引用的那次实跑证伪的"
    )
    assert "全绿**（Hermes 源码在场时" not in zh, (
        "README.md 又出现了被证伪的旧措辞：把「装上宿主」等同于「全绿」"
    )
    en = _read_doc("README_EN.md")
    assert "never measured" in en, "README_EN.md 未标注该数字是推导值"
    # 负向对照必须只盯**宣称**，不能连**引述旧假话**一起禁掉：
    # 正文里逐字引用当年那句 "verified on production" 是留证据，不是再犯。
    # 所以对照收窄到表格里「全轴齐备」自己那一行。
    for doc, label, bad in (
        (zh, "全轴齐备", ("生产实测", "实跑核验", "生产核验")),
        (en, "All axes present", ("verified on production", "measured on production")),
    ):
        row = [ln for ln in doc.split("\n") if ln.lstrip().startswith(f"| {label} |")]
        assert len(row) == 1, f"文档里「{label}」表格行找不到或不唯一：{len(row)} 行"
        for word in bad:
            assert word not in row[0], (
                f"「{label}」那一行又宣称「{word}」—— "
                "这个数字从来没有被任何一台机器跑出来过"
            )


def test_no_machine_here_satisfies_all_four_axes():
    """自证：本机也不是那台「四轴齐备」的机器，全绿数字依旧只能是推导的。

    这条不是行为测试，是**证据保全** —— 它把「我们手上没有那样一台机器」
    这句话变成一条会随环境变化而失效的断言，而不是 README 里一句无人复核的话。
    真有一天四轴齐备了，这条会红，那时才有资格把「推导值」改成「实测」。
    """
    present = []
    if pathlib.Path("/hermes/hermes-agent/agent/memory_provider.py").exists():
        present.append("hermes_host")
    if (_REPO_ROOT / ".git").exists():
        present.append("git_worktree")
    if (_REPO_ROOT / "scripts" / "backup_gate.sh").exists() and \
            not sys.platform.startswith("win"):
        present.append("backup_gate_posix")
    try:
        __import__("qdrant_client")
        present.append("qdrant_client")
    except ImportError:
        pass

    if len(present) == len(_AXES):
        pytest.fail(
            "本机四条跳过轴全部齐备 —— 这是好事：现在可以真跑一次全量，"
            "把 README 里「推导值，从未实测」改成实测值，并删掉这条断言。"
            f"（齐备的轴：{present}）"
        )
