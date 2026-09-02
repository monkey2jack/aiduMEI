"""跳过轴普查（v20.0 新增）。

## 为什么要有这个文件

v20.0 之前，README 里写着一句绝对话：

    | 完整环境 | **833 全绿**（Hermes 源码在场时 12 个跳过项全部执行并通过，生产实跑核验） |

它错了，而且是**被它自己引用的那次生产实跑**证伪的：部署树上宿主明明在场，
跑出来是 `832 passed, 1 skipped`。根因不是数字手滑，是**模型缺了三条轴** ——
当时以为「跳过」只有宿主 Hermes 源码这一条成因，于是把「装上宿主」等同于「全绿」。
实际有八条互不相干的轴（v20.0 跑分管线带进来三条：LoCoMo 数据集、`regex`、`numpy`；
`.gitignore` 守卫又带进来第八条：`git` 可执行文件），两台机器各缺一条，
`833` 从来没有任何一台机器跑出来过。

第八条那条轴顺带示范了一件事：**换轴不等于消轴**。`test_v20_gitignore_guard.py` 本来
要靠「本仓有 `.git`」才跑得动（生产沙箱和 sdist 都没有，那是必然跳过），改成 `/tmp`
一次性仓之后，依赖降级成「本机有 `git` 命令」—— 概率小了两个数量级，但它还是一条轴，
还是得登记。轴不登记，README 那句「全轴齐备」就是拿一个不完整的模型算出来的数。

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
import importlib.util
import io
import pathlib
import re
import shutil
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

# ── 跳过轴登记表（v20.1 整改轮扩到十条：mem0 基座轴随补丁层 importorskip 进场） ────────────────────────────────────────────────────────
# scope="file"     整份文件被跳过（模块级 pytestmark / 类级 skipIf）
# scope="callsite" 只跳所在的那个测试函数
# doc_zh / doc_en  README 里那张轴表的行首标签，用来把文档数字钉到实测值上
# match            可选：同一个文件里可以住着好几条互不相干的轴（LoCoMo 那份
#                  就住着三条），这个正则对跳过点所在的**原始行**做匹配，
#                  把「轴」从「按文件」拆成「按原因」。不给就认领全文件。
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
    {
        "key": "locomo_dataset",
        "file": "test_v20_locomo_official.py",
        "scope": "callsite",
        "match": r"locomo10\.json",
        "doc_zh": "LoCoMo 数据集已就位",
        "doc_en": "LoCoMo dataset present",
    },
    {
        "key": "bench_dep_regex",
        "file": "test_v20_locomo_official.py",
        "scope": "callsite",
        "match": r'importorskip\("regex"\)',
        "doc_zh": "`regex` 已安装",
        "doc_en": "`regex` installed",
    },
    {
        "key": "bench_dep_numpy",
        "file": "test_v20_locomo_official.py",
        "scope": "callsite",
        "match": r'importorskip\("numpy"\)',
        "doc_zh": "`numpy` 已安装",
        "doc_en": "`numpy` installed",
    },
    {
        # v20：nltk 此前**一处未声明**，靠「本机碰巧装过」才跑得通 —— 生产实机
        # 实测 13 条用例集体变红，而那些红看着和真缺陷一模一样。现在与同族的
        # regex/numpy 一个待遇：缺它是跳过，不是失败。
        "key": "bench_dep_nltk",
        # 这条轴**跨两个文件** —— 官方 F1 的用例分布在打分实现与跑分管线两处。
        # 登记表原先是一轴一文件；硬拆成两条轴会把「同一个依赖」说成两件事，
        # 所以这里扩成 files 列表，`file` 仍兼容单文件写法。
        "files": ["test_v20_locomo_official.py", "test_v20_benchmarks.py"],
        "scope": "callsite",
        "match": r'importorskip\("nltk"',   # 单行形态：匹配是按**原始行**做的
        "doc_zh": "`nltk` 已安装",
        "doc_en": "`nltk` installed",
    },
    {
        # v20.1 整改轮（R-12 附带）：补丁层疗法测试要真实 mem0 基座在场 ——
        # 此前缺 mem0 是 20 条 ERROR（看着和真缺陷一模一样），现在是跳过。
        # 开发机与生产沙箱都装了 mem0，所以这条轴平时隐形；它真正门控的是
        # 「只装 aidumei 不装 mem0ai」的下游消费环境。
        "key": "mem0_base",
        "file": "test_v20_mem0_patch_layer.py",
        "scope": "file",
        "doc_zh": "`mem0ai` 已安装",
        "doc_en": "`mem0ai` installed",
    },
    {
        # v20.2 自动挡：备胎真模型测试要 fastembed + 模型文件在场 ——
        # 缺依赖是跳过不是失败（与 mem0 轴同款；模型未部署时用例内
        # 二次 skip，轴登记按依赖可导入性计）。
        "key": "fastembed_local",
        "file": "test_v20_2_autoshift.py",
        "scope": "callsite",
        # 同一用例两个跳过点同属本轴：依赖缺失（importorskip）与
        # 模型未就绪（用例内 skip）——都是「备胎不在场」。
        # v20.3.1：跳过文案改成能自曝真因（缓存目录 + 是否设了 env），
        # 锚串跟着换；这类「文案改了锚串没跟上」正是这条守卫存在的意义。
        "match": r'importorskip\("fastembed"|本地嵌入模型未就绪',
        "doc_zh": "`fastembed` 已安装",
        "doc_en": "`fastembed` installed",
    },
    {
        # v20.2.5：第十二条轴。ruff 只在 dev extra 里，生产 venv 不装 lint
        # 工具 —— 沙箱用生产 venv 跑套件，于是那两条 lint 守卫在那里跳过。
        # **第一版不是跳过而是静默返回「无命中」**，守卫因此永远绿；
        # 沙箱实测把它抓出来了，改成 find_spec 探测 + 显式 skip。
        "key": "ruff_installed",
        "file": "test_v20_2_5_audit_remediation.py",
        "scope": "callsite",
        "match": r'pytest\.skip\("ruff 不可用',
        "doc_zh": "`ruff` 已安装",
        "doc_en": "`ruff` installed",
    },
    {
        "key": "git_binary",
        "file": "test_v20_gitignore_guard.py",
        "scope": "file",
        "doc_zh": "`git` 可执行文件在场",
        "doc_en": "`git` executable present",
    },
    {
        # v20.3 外审整改轮：生产 venv 不装 [mcp] optional extra。直接 import
        # mcp_server 会在启动阶段连带 import fastmcp；缺依赖按轴跳过，不是失败。
        "key": "mcp_extra",
        "file": "test_first_run_experience.py",
        "scope": "callsite",
        "match": r'importorskip\("mcp_server"\)',
        "doc_zh": "`mcp` extra 已安装",
        "doc_en": "`mcp` extra installed",
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


def _matched_sites(fname, match=None):
    """这条轴认领了该文件里哪些跳过点（行号集合）。`match=None` 表示认领全部。

    🔴v20.0：`match` 必须对**原始行**匹配，不能用 `_code_only_lines` 的结果 ——
    那里字符串字面量已被抹白，而「哪条轴」恰恰写在字面量里
    （`importorskip("regex")` 里的 `regex`、`pytest.skip("…locomo10.json")`）。
    跳过点本身仍由抹白后的代码认定，所以「在一句话里提到它」照旧不算数。
    """
    sites = _skip_sites(fname)
    if match is None:
        return set(sites)
    raw = pathlib.Path(_TESTS_DIR, fname).read_text(encoding="utf-8").split("\n")
    return {s for s in sites if re.search(match, raw[s - 1])}


def _callsite_gated_cases(fname, match=None):
    """调用点跳过：数「会被这条轴门控的测试函数」有几个（AST 实测，不数行）。

    v20.2.5 起支持**辅助函数间接门控**：跳过语句写在共用辅助函数里（`_ruff()`
    就是这样）时，直接按行号归属去数会得到 0 —— 而实际被门控的用例明明有两条。
    那种 0 会让「每条轴至少门控一条用例」那条守卫亮红，报错却指向「位点搬家」，
    **看起来像轴废了，其实是数法不认得间接调用**。所以这里做一次不动点传播：
    含跳过点的函数是门控源，调用了门控函数的函数也被门控，直到不再增长。
    """
    path = pathlib.Path(_TESTS_DIR, fname)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    sites = _matched_sites(fname, match)

    funcs = {}      # 函数名 → (起, 止)
    calls = {}      # 函数名 → 它调用的名字集合
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        lo, hi = node.lineno, getattr(node, "end_lineno", node.lineno)
        funcs[node.name] = (lo, hi)
        names = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                fn = sub.func
                if isinstance(fn, ast.Name):
                    names.add(fn.id)
                elif isinstance(fn, ast.Attribute):
                    names.add(fn.attr)
        calls[node.name] = names

    # 门控源：函数体内直接含本轴的跳过点。嵌套函数会让内外层同时命中，
    # 不影响结论（内层必然被外层调用或包含）。
    gated = {name for name, (lo, hi) in funcs.items()
             if any(lo <= s <= hi for s in sites)}
    while True:                      # 不动点：调用了门控函数的也算门控
        grown = {name for name, names in calls.items() if names & gated}
        if grown <= gated:
            break
        gated |= grown
    return len({n for n in gated if n.startswith("test_")})


@functools.lru_cache(maxsize=1)
def _measured_axis_counts():
    """每条轴实际门控了几条用例 —— 全部现场测，登记表里不放数字。"""
    out = {}
    for axis in _AXES:
        files = axis.get("files") or [axis["file"]]
        if axis["scope"] == "file":
            out[axis["key"]] = sum(_cases_in_file(f) for f in files)
        else:
            out[axis["key"]] = sum(
                _callsite_gated_cases(f, axis.get("match")) for f in files)
    return out


def test_skip_axis_census_matches_registry():
    """tests/ 里出现跳过机制的文件，必须与登记表完全一致。

    多一个 → 有人又加了一条轴却没写进 README，「全绿」的含义又变了没人知道；
    少一个 → 某条轴被删了，README 上那一行成了描述不存在之物的死文案。
    两个方向都必须红。
    """
    found = {p.name for p in sorted(_TESTS_DIR.glob("*.py"))
             if any(_SKIP_MECHANISM.search(ln) for ln in _code_only_lines(str(p)))}
    registered = {f for axis in _AXES for f in (axis.get("files") or [axis["file"]])}

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


def test_every_skip_site_is_claimed_by_exactly_one_axis():
    """每一个跳过点都必须被**恰好一条**轴认领。

    🔴v20.0：允许一个文件登记多条轴（各带 `match`）之后，冒出了一个新的藏身处 ——
    `test_skip_axis_census_matches_registry` 只比**文件名集合**，所以往一个已登记
    的文件里再加一个跳过点，它一声不响。而那正是这次踩到的坑的形状：
    以为 `test_v20_locomo_official.py` 只有一条轴，实际住着三条。
    这条守卫把每个跳过点点名分配到轴上：没人认领要红，两条轴抢一个也要红。
    """
    for fname in sorted({f for axis in _AXES for f in (axis.get("files") or [axis["file"]])}):
        sites = _skip_sites(fname)
        assert sites, f"{fname} 登记在册，却一个跳过点都找不到 —— 登记表是死文案"
        owners = {}
        for axis in _AXES:
            if fname not in (axis.get("files") or [axis["file"]]):
                continue
            for site in _matched_sites(fname, axis.get("match")):
                owners.setdefault(site, []).append(axis["key"])

        orphan = sorted(s for s in sites if s not in owners)
        assert not orphan, (
            f"{fname} 第 {orphan} 行的跳过点没有任何一条轴认领 —— "
            "这个文件已经在登记表里，文件名比对逮不到它，"
            "README 轴表的门控条数会连带算错。请给它登记一条轴（带 `match`）"
        )
        shared = {s: ks for s, ks in owners.items() if len(ks) > 1}
        assert not shared, (
            f"{fname} 这些跳过点被多条轴同时认领：{shared} —— "
            "`match` 写得太宽，门控条数会被重复计入"
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


def test_all_axes_number_is_measured_and_attributed():
    """「全轴齐备」那个数字现在是实测值了，但**必须带出处**。

    这条守卫的前身要求 README 写「推导值，从未实测」—— 因为更早的一版谎称
    「生产实跑核验」，而它恰恰被自己引用的那次生产实跑当场证伪。

    2026-08-24 该数字**真的被跑出来了**：生产实机九轴同时齐备、全量 0 跳过。
    于是原来那条守卫的字面要求（必须写「从未实测」）本身变成了假话，
    所以它被**反转**而不是删除 —— 守的东西一个字没变：
    **绝对措辞必须经得起自己引用的那次测量。**

    反转后的判据有三条，缺一条都能让这一行重新变得不可证伪：
      ① 不许再写「从未实测」—— 它现在是假的；
      ② 宣称实测就必须**带日期**，否则「实测过」是一句无法复核的话
         （谁测的、哪天测的、什么环境，读者一个都对不上）；
      ③ 那句被证伪过的旧等式（「装上宿主」＝「全绿」）永远不许回来 ——
         装宿主只满足九条轴里的**一条**，这个错和数字是否实测无关。
    """
    import re as _re
    zh = _read_doc("README.md")
    en = _read_doc("README_EN.md")

    # ① 旧**宣称**必须退场 —— 但**引述旧措辞留证据**不算再犯。
    #
    # 这条区分是从原守卫继承下来的（它当年写着：「负向对照必须只盯宣称，
    # 不能连引述旧假话一起禁掉」）。所以这里禁的是当年那句**加粗的宣称原文**，
    # 不是「从未实测」这四个字本身 —— 后者现在出现在「在此之前标注为……」
    # 这样的历史交代里，那是资产不是负债。裸词匹配会把两者一起杀掉。
    assert "**推导值，从未实测**" not in zh, (
        "README.md 还在宣称「推导值，从未实测」—— 2026-08-24 生产实机九轴齐备、"
        "0 跳过，这个数字已经被真跑出来了，继续这么宣称就是新的假话"
    )
    assert "**derived, never measured**" not in en, (
        "README_EN.md 还在宣称 derived, never measured"
    )

    # ③ 被证伪过的旧等式永不回归（与实测无关，独立成条）
    assert "全绿**（Hermes 源码在场时" not in zh, (
        "README.md 又出现了被证伪的旧措辞：把「装上宿主」等同于「全绿」——"
        "宿主只是九条轴里的一条"
    )

    # ② 宣称实测就要带日期，且必须落在「全轴齐备」自己那一行
    for doc, label, name in ((zh, "全轴齐备", "README.md"),
                             (en, "All axes present", "README_EN.md")):
        row = [ln for ln in doc.split("\n") if ln.lstrip().startswith(f"| {label} |")]
        assert len(row) == 1, f"{name} 里「{label}」表格行找不到或不唯一：{len(row)} 行"
        assert _re.search(r"20\d\d-\d\d-\d\d", row[0]), (
            f"{name} 的「{label}」那一行宣称了实测却没写日期 —— "
            "「实测过」不带出处就是一句无法复核的话，和推导值一样不可证伪"
        )


def test_every_registered_skip_axis_has_a_probe():
    """每条登记的跳过轴都必须有探测器 —— 少一个，齐备判定就永远说不出「齐备」。

    **本函数的前身是一条绊线**（`test_no_machine_here_satisfies_every_axis`）：
    它在「本机九轴齐备」时故意 `pytest.fail`，用来保全「我们手上没有那样一台机器」
    这句话 —— 让它变成一条会随环境失效的断言，而不是 README 里一句无人复核的话。

    2026-08-24 那条绊线**按设计亮红了**：生产实机九轴同时齐备，全量 0 跳过。
    绊线自己的报错原文就是「这是好事：现在可以真跑一次全量，把 README 里
    「推导值，从未实测」改成实测值，并删掉这条断言」。照办 —— 绊线已拆，
    README 的那一行换成带日期的实测值，由
    `test_all_axes_number_is_measured_and_attributed` 接着守。

    **保留下来的是探测器完备性这一半，它和绊线是两件事。**
    轴从四条长到九条时若不补探测器，`len(present)` 永远小于 `len(_AXES)`，
    任何基于「齐备与否」的判断都会静默失真 —— 这个风险和绊线在不在无关。
    """
    present = []
    # ⚠️ 探测器必须问**闸门本身**，不许自己另写一套判据。
    #
    # 原先这里硬查一个写死的路径 `/hermes/hermes-agent/agent/memory_provider.py`。
    # v20 生产实机踩到：真实闸门（test_hermes_plugin._is_host）已经收紧成「三个
    # 模块都得在」，而探测器还在按老判据点头 —— 于是它报「hermes_host 齐备」，
    # 同一轮里那条轴却实实在在门控掉了 12 条用例。**判据与被判之物不是同一个射程**，
    # 结论就必然自相矛盾。现在直接读闸门解析出来的 HOST。
    try:
        import test_hermes_plugin as _hp
        if _hp.HOST is not None:
            present.append("hermes_host")
    except Exception:
        pass  # 导不进来 = 这条轴无从判定，按不齐备处理（宁可少报齐备）
    if (_REPO_ROOT / ".git").exists():
        present.append("git_worktree")
    if (_REPO_ROOT / "scripts" / "backup_gate.sh").exists() and \
            not sys.platform.startswith("win"):
        present.append("backup_gate_posix")
    for mod, key in (("qdrant_client", "qdrant_client"),
                     ("regex", "bench_dep_regex"),
                     ("numpy", "bench_dep_numpy"),
                     ("nltk", "bench_dep_nltk"),
                     ("mem0", "mem0_base"),
                     ("fastembed", "fastembed_local")):
        try:
            __import__(mod)
        except ImportError:
            continue
        present.append(key)
    from benchmarks import download as _bdl      # 走产品自己的解析器，不另立门户
    if pathlib.Path(_bdl.data_dir(), "locomo10.json").exists():
        present.append("locomo_dataset")

    if shutil.which("git") is not None:
        present.append("git_binary")

    try:
        # 问闸门自己的判据（`ruff_available`），不在这里另写一次 find_spec ——
        # 上面那段注释记的就是「判据与被判之物射程不同」踩出来的坑。
        import test_v20_2_5_audit_remediation as _rem
        if _rem.ruff_available():
            present.append("ruff_installed")
    except Exception:
        pass  # 导不进来 = 无从判定，按不齐备处理（宁可少报齐备）

    try:
        # 问产品真正 import 的那个模块路径，不问同名巧合。
        # 上一版这里写的是 `import fastmcp` —— 那是 PyPI 上另一个独立项目
        # （fastmcp 包），而 mcp_server.py:153 用的是 `mcp.server.fastmcp`。
        # 后果不是"少报一条轴"这么简单：装了 mcp 也永远报缺席，这条轴
        # 于是**在任何机器上都不可能齐备** —— 探测器接错了线，比没接更坏，
        # 因为它看起来在工作。用 find_spec 而非真 import：判路径存在性，
        # 不执行 mcp_server（它会建 FastMCP 实例，有副作用）。
        if importlib.util.find_spec("mcp.server.fastmcp") is not None:
            present.append("mcp_extra")
    except (ImportError, ValueError):
        pass

    probed = {"hermes_host", "git_worktree", "backup_gate_posix", "qdrant_client",
              "bench_dep_regex", "bench_dep_numpy", "bench_dep_nltk",
                  "locomo_dataset", "git_binary", "mem0_base", "fastembed_local",
                  "ruff_installed", "mcp_extra"}
    unprobed = {axis["key"] for axis in _AXES} - probed
    assert not unprobed, (
        f"这些轴没有探测器：{sorted(unprobed)} —— 少一个探测器，"
        "下面那句「本机不齐备」就永远成立，这条证据保全变成白护栏"
    )

    # ★ 这里刻意**不再**对 len(present) == len(_AXES) 做任何断言。
    #   绊线已于 2026-08-24 按设计触发并拆除（理由见 docstring）。
    #   `present` 仍然算出来，是因为它就是探测器完备性的证据本身：
    #   下面这条断言保证每条轴都被探过，而不是保证探出来是什么结果。
    assert isinstance(present, list), "present 应是探测结果列表"
