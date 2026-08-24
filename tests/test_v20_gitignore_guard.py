"""v20.0pre `.gitignore` 忽略规则守卫 —— 「什么东西永远进不来仓库」这条防线，问解析者，不问文本。

## 这份守卫冻结的是什么

`.gitignore` 是**秘密面的第一道闸门**：一条规则写漏，`.env.prod` 这种人手起名的
配置文件就会被 `git add -A` 顺手带进仓库，然后跟着 sdist、跟着 GitHub、跟着 PyPI
一路走完七个公开面。这类事故的特征是**当场无声**：`git status` 不会喊，测试不会红，
CI 不会拦，只有别人 clone 下来才看得见。

v20.0pre 给 `.gitignore` 补了三段（`.env.*` 通配、私钥/证书形状、脱敏词表、SQLite 库）。
补完当时是靠 `git check-ignore -v` 人肉双向验的 —— 而人肉验过的东西，下一次改动不会
自动再验一遍。这份文件把那次人肉验证焊成断言。

## 为什么不直接在本仓问 git

本仓在生产沙箱里**没有 `.git`**（白名单拷贝部署），在 sdist 里也没有。如果直接
`git check-ignore` 本仓，这份守卫在那两种环境下会整份退化成 skip —— 而
「忽略规则写对了没有」压根不是本机有没有 `.git` 的函数，**它是这份文本自身的性质**。

所以改成：建一个 `/tmp` 下的一次性空仓，只把本仓这份 `.gitignore` 拷进去，
在那儿问 git。规则照旧由 git 自己解析（铁律 13：配置写了不等于配置生效，问解析者），
而守卫在开发机、生产沙箱、sdist 里都照跑 —— **只要本机有 `git` 可执行文件**。

这一步换掉的是「本仓有没有 `.git` 目录」这条轴（生产沙箱、sdist 都没有，那是**必然**
跳过），换成「本机有没有 `git` 命令」这条轴（开发机、生产机、CI 都有，那是**几乎不会**
跳过）。换掉不等于消掉：下面第 69 行仍然挂着 `skipif`，这份文件仍然欠一条 skip 轴，
已按名登记在 `tests/test_v20_skip_axis_census.py`。写「零 skip」是句假话 ——
这份守卫存在的意义就是不许留这种话。

一次性仓还顺手解决一个更阴的问题：`git check-ignore` 会叠加**全局** gitignore
（`core.excludesFile`）和 `.git/info/exclude`。如果本机全局 gitignore 里恰好有
`*.db`，那么就算本仓这份文件把 `*.db` 删了，探针依然会答「已忽略」——
**一条绿灯，来自别人机器上不存在的配置。** 所以每次调用都带
`GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_SYSTEM=/dev/null`，把答案的来源掐死在
我们自己这份文本上。`test_oracle_only_reflects_our_own_file` 会把这件事验出来。

## 三向对照（铁律 14：双向可复现才叫可证伪）

1. 该忽略的忽略了 —— `test_secret_shaped_paths_are_ignored`
2. 不该忽略的没忽略 —— `test_shipped_files_are_not_ignored`
3. 把规则抽掉，断言必须翻面 —— `test_oracle_is_falsifiable`

少了第三条，前两条就是一对可能永远为真的空护栏：一个把什么都答「已忽略」的探针
能让第 1 条全绿，一个压根没读到规则的探针能让第 2 条全绿。

## 为什么走盘而不是 `git ls-files`

跟 `tests/test_v20_brand_policy.py` 同一个理由，而且这里更狠：生产机那棵树的 git
索引停在旧 commit（231 条索引 vs 磁盘 282 个文件），`git ls-files` 在那儿**不报错，
只少报** —— 拿它当「该发布的文件清单」会把 51 个 v20 文件悄悄漏在射程外。走盘不会。

代价是走盘会撞见一堆本来就不该发布的东西（`.venv/`、`data/`、`*.egg-info/`）。
处理办法**不是**给它们开目录级豁免（铁律 12 严禁），而是：剪掉的每一个目录，都由
`test_pruned_zones_are_provably_ignored` 逐个证明它确实被这份 `.gitignore` 忽略。

🔴 这里第一版翻过一次车，值得留着：当时剪枝表和证明表**是同一张手写表**（判据 →
代表路径），我以为「同一张表」就等于「射程相等」。不等于。判据那栏写的是前缀
`data*`，代表路径那栏给的是 `data_real/facts.db` —— 而这条路径命中的是
`.gitignore:32 data_real/` 这条**单独**规则。真正让 `data_mock/`（28 个文件）和
`data_smoke/`（3 个文件）被忽略的是 `:34 data_*/`。把 `:34` 删掉，实测这份守卫
**六条全绿**，而那 31 个文件已经悄悄变成「剪了但没被忽略」—— 一次货真价实的
目录级豁免，藏在一张自认为对称的表里。

所以现在改成**由构造保证相等**：`_walk_shipped_files()` 除了文件清单，还返回它
**实际剪掉**的相对路径集合，证明那一步照着这个集合逐个探。剪了什么就证什么，
判据宽一寸也逃不掉。教训写在这儿：**判据和证明「是同一张表」不等于「是同一个射程」，
唯一靠得住的是让证明从代码实际干了什么里长出来。**

注意 `node_modules` **不在**剪枝表里：本仓全盘没有 `package.json`，`frontend/` 是
裸 HTML/CSS/JS，这条规则今天的爆炸半径是 0，所以没往 `.gitignore` 里加。
真哪天有人引进了 node 依赖，走盘会撞上一堆 `.js`，这份守卫直接红 —— 把
「要不要忽略它」这个决定推到人面前，而不是替他提前拍板。
"""

import functools
import os
import shutil
import subprocess

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GITIGNORE = os.path.join(_REPO_ROOT, ".gitignore")

# 一次性仓要 git 可执行文件。这是本文件唯一的跳过轴，已登记在
# tests/test_v20_skip_axis_census.py，README 的跳过轴表格里也有对应一行。
pytestmark = pytest.mark.skipif(
    shutil.which("git") is None,
    reason="本机没有 git 可执行文件，建不出问规则用的一次性仓",
)

# 把答案来源掐死在我们自己这份文本上：不许全局/系统 gitignore 混进来充当绿灯。
_ISOLATED_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def _gitignore_text():
    with open(_GITIGNORE, "r", encoding="utf-8") as fh:
        return fh.read()


@functools.lru_cache(maxsize=4)
def _oracle(gitignore_text):
    """建一个一次性空仓，装上给定的忽略规则文本，返回仓目录。

    按文本内容缓存：同一份规则只建一次仓；`test_oracle_is_falsifiable` 传进去的
    「被抽掉一条规则的变体」自然拿到另一个仓，两边互不污染。
    """
    import atexit
    import tempfile

    tmp = tempfile.mkdtemp(prefix="aidumem_gitignore_oracle_")
    atexit.register(shutil.rmtree, tmp, True)
    subprocess.run(["git", "init", "-q", tmp], check=True, env=_ISOLATED_ENV)
    with open(os.path.join(tmp, ".gitignore"), "w", encoding="utf-8") as fh:
        fh.write(gitignore_text)
    # fresh init 的 .git/info/exclude 只有注释模板，但不赌它 —— 直接清空。
    with open(os.path.join(tmp, ".git", "info", "exclude"), "w", encoding="utf-8") as fh:
        fh.write("")
    return tmp


def _ignored(paths, gitignore_text=None):
    """问 git：这批相对路径里，哪些会被忽略？返回被忽略的那个子集。

    `git check-ignore` 的退出码：0 = 至少命中一条，1 = 一条都没命中，>1 = 它自己坏了。
    把 1 当错误会让「干净」看起来像「故障」；把 >1 当 1 会让故障看起来像干净 ——
    所以三种情况分开处理，>1 直接抛。
    """
    paths = list(paths)
    if not paths:
        return set()
    tmp = _oracle(gitignore_text if gitignore_text is not None else _gitignore_text())
    proc = subprocess.run(
        ["git", "check-ignore", "--stdin"],
        cwd=tmp, input="\n".join(paths),
        capture_output=True, text=True, env=_ISOLATED_ENV,
    )
    if proc.returncode > 1:
        raise AssertionError(
            f"git check-ignore 自己坏了（退出码 {proc.returncode}）："
            f"{proc.stderr.strip()[:300]}\n"
            "—— 这不是「没命中」，别当干净放过去。"
        )
    return {line for line in proc.stdout.split("\n") if line}


# ── 该被挡住的：每一条都写明「为什么它必须进不来」 ──────────────────────────
_MUST_BE_IGNORED = (
    (".env", "主配置，装着网关地址和 token"),
    (".env.local", "人手起名的本地覆盖"),
    (".env.prod", "v20.0pre 补的正是这条：`.env` 一条盖不住 `.env.prod`"),
    (".env.bak", "改配置时顺手留的副本，内容和正本一样敏感"),
    ("secrets.json", "名字就写着 secrets"),
    ("id_ed25519", "生产机 SSH 私钥，本仓的部署凭证就是它"),
    ("id_rsa", "另一种私钥命名"),
    ("server.pem", "证书/私钥容器"),
    ("client.key", "私钥"),
    ("bundle.p12", "证书+私钥打包"),
    ("scan_words.txt", "脱敏词表本身就是要藏的信息（铁律 0：词表一律外置）"),
    ("project.scanwords", "词表的另一种命名"),
    ("facts.db", "记忆库正本，装着部署方的真实记忆内容"),
    ("mem0_config_local.json", "本地配置，含真实 base_url / key"),
    ("uv.lock", "锁文件按仓规不入库"),
    ("notes.bak", "临时副本"),
    ("data/facts.db", "默认数据落点"),
    ("logs/api.log", "运行日志会带上真实 user_id 和查询内容"),
)

# ── 必须发得出去的：一条规则写宽了，先在这里绊倒 ──────────────────────────
_MUST_SHIP = (
    (".env.example", "示例配置，靠它别人才知道要配什么；`!.env.example` 就是为它写的"),
    ("mem0_config_local.json.example", "同上，示例必须发"),
    ("README.md", "门面"),
    ("README_EN.md", "门面"),
    ("CHANGELOG.md", "全部历史"),
    ("pyproject.toml", "包元数据"),
    ("requirements.txt", "依赖声明（`uv.lock` 不入库，这份必须入）"),
    ("Dockerfile", "构建入口"),
    ("LICENSE", "许可证"),
    (".gitignore", "本守卫的被测对象自己"),
    ("ducky/version.py", "源码"),
    ("ducky/mem0_patches.py", "v20.0pre 新增的补丁层源码"),
    ("scripts/release_scan.py", "脱敏扫描器"),
    ("scripts/backup_gate.sh", "备份闸门"),
    ("tests/test_v20_gitignore_guard.py", "本文件"),
    ("benchmarks/data_manifest.json", "manifest 是承诺，必须发（`benchmarks/runs/` 才是产物）"),
    ("assets/aidumem-banner.jpg", "图片面也要发得出去"),
    ("frontend/index.html", "前端资产"),
    ("docs/ARCHITECTURE.md", "文档"),
)

# ── 走盘剪掉的目录 ────────────────────────────────────────────────────────────
# 剪枝判据分三种形状。每一种都配一个**具体**代表路径，由
# `test_pruned_zones_are_provably_ignored` 证明它确实被这份 `.gitignore` 忽略。
#
# 🔴 v20.0pre 第一版这里踩了一次「判据比证明宽」：判据写的是前缀 `data*`，代表路径
#    却给了 `data_real/facts.db` —— 而那条路径是被 `.gitignore:32 data_real/` 单独
#    挡住的。真正让 `data_mock/`、`data_smoke/` 被忽略的是 `:34 data_*/`。也就是说，
#    把 `data_*/` 那行删掉，`data_mock/` 会立刻变成「剪了但没被忽略」，而这个代表路径
#    照旧全绿。**判据的射程必须等于证明的射程，宽一寸就是一次目录级豁免。**
#    所以下面除了静态代表路径，还会拿「本次走盘真正剪掉的每一个目录」逐个再证一遍
#    （见 `_walk_shipped_files` 的第二个返回值），把射程焊死在实际发生的剪枝上。
_PRUNE_NAMES = {
    ".venv": ".venv/lib/python3.12/site-packages/x.py",
    "venv": "venv/lib/x.py",
    "env": "env/lib/x.py",
    "__pycache__": "__pycache__/x.pyc",
    ".pytest_cache": ".pytest_cache/v/cache/lastfailed",
    ".mypy_cache": ".mypy_cache/x.json",
    ".ruff_cache": ".ruff_cache/x.txt",
    "htmlcov": "htmlcov/index.html",
    ".idea": ".idea/workspace.xml",
    ".vscode": ".vscode/settings.json",
    "logs": "logs/api.log",
    "chroma_data": "chroma_data/chroma.sqlite3",
    "backups": "backups/b.tar",
    ".local": ".local/x.json",
    ".deps": ".deps/x.json",
    "exports": "exports/x.json",
    "dist": "dist/aidumei-20.0.whl",
    "build": "build/lib/ducky/x.py",
    ".eggs": ".eggs/x.egg",
}
# 前缀判据：`data`、`data_real`、`data_mock`、`data_smoke` … 代表路径必须打在
# **通配那条规则**上（`data_*/`），不能拿被单独规则挡住的名字充数。
_PRUNE_PREFIXES = {"data": "data_zzz_probe/x.json"}
# 后缀判据
_PRUNE_SUFFIXES = {".egg-info": "aidumei.egg-info/PKG-INFO"}
# 整条相对路径判据（`runs` 只在 benchmarks 下被忽略，不许按裸目录名到处剪）
_PRUNE_RELPATHS = {"benchmarks/runs": "benchmarks/runs/run-1.json"}
# `.git` 单列：git 从来不跟踪自己的元数据目录，这跟 `.gitignore` 写了什么无关，
# 所以它进不了上面那些「由本文件证明被忽略」的表 —— 硬塞进去只会得到一条必红的断言。
_VCS_DIR = ".git"

# 故意**不**剪 `node_modules`：本仓全盘没有 `package.json`，`frontend/` 是裸
# HTML/CSS/JS，所以没往 `.gitignore` 里加这条规则（爆炸半径实测为 0）。真哪天有人
# 引进 node 依赖，走盘会撞上一堆 `.js`，这份守卫直接红 —— 把「要不要忽略它」这个
# 决定推到人面前，而不是替他提前拍板。同理 `legacy/`、`archive/` 也不剪：
# `.gitignore:54` 挡的是 `legacy/archive/` 这一条路径，不是这两个裸目录名。

_SHIP_SUFFIX = (".py", ".md", ".txt", ".toml", ".json", ".yml", ".yaml", ".sh",
                ".html", ".css", ".js", ".in", ".example", ".cfg", ".ini")
_SHIP_NAMES = {"Dockerfile", "LICENSE", "MANIFEST.in", ".gitignore", ".dockerignore"}


def _is_pruned(dirname, relpath):
    if dirname == _VCS_DIR or relpath in _PRUNE_RELPATHS or dirname in _PRUNE_NAMES:
        return True
    if any(dirname.startswith(p) for p in _PRUNE_PREFIXES):
        return True
    return any(dirname.endswith(s) for s in _PRUNE_SUFFIXES)


def _walk_shipped_files():
    """走盘收「长得像该发布的文件」，同时**记下真正剪掉了哪些目录**。

    不问 git 索引：生产机那棵树的索引停在旧 commit，少报 51 个文件，
    拿它当「该发布的清单」会把 v20 的新文件整批漏在射程外。

    第二个返回值是射程焊条 —— 剪了什么就得证明什么，一个不落。
    """
    out, pruned = [], set()
    for dirpath, dirnames, filenames in os.walk(_REPO_ROOT):
        rel_dir = os.path.relpath(dirpath, _REPO_ROOT)
        rel_dir = "" if rel_dir == "." else rel_dir
        keep = []
        for d in dirnames:
            rel = os.path.join(rel_dir, d) if rel_dir else d
            if _is_pruned(d, rel):
                if d != _VCS_DIR:
                    pruned.add(rel)
            else:
                keep.append(d)
        dirnames[:] = keep
        for fn in filenames:
            if fn in _SHIP_NAMES or fn.endswith(_SHIP_SUFFIX):
                out.append(os.path.join(rel_dir, fn) if rel_dir else fn)
    return sorted(out), pruned


# ══════════════════════════════════════════════════════════════════════════════
# 断言
# ══════════════════════════════════════════════════════════════════════════════


def test_oracle_only_reflects_our_own_file():
    """探针的「已忽略」必须来自本仓这份 .gitignore，不许来自本机全局配置。

    反假绿灯：如果探针漏了 `GIT_CONFIG_GLOBAL=/dev/null`，而跑测的人全局 gitignore
    里恰好有 `*.db`，那么本仓把 `*.db` 删掉之后这份守卫**依然全绿** —— 一条绿灯，
    来自别人机器上不存在的配置。这里用一个绝不可能出现在任何词表里的对照路径
    反向验一次：它必须不被忽略。
    """
    assert os.path.exists(_GITIGNORE), f"本仓没有 .gitignore？{_GITIGNORE}"
    tmp = _oracle(_gitignore_text())
    with open(os.path.join(tmp, ".gitignore"), "r", encoding="utf-8") as fh:
        seeded = fh.read()
    assert seeded == _gitignore_text(), "一次性仓里的规则和本仓这份不是同一份文本"

    control = "zzz_control_path_in_no_wordlist_1a2b3c.qqq"
    assert _ignored([control]) == set(), (
        f"对照路径 {control} 居然被忽略了 —— 说明探针在无条件答「已忽略」，"
        "或者有全局 gitignore 混了进来。这种探针给出的全绿毫无意义。"
    )


def test_oracle_is_falsifiable():
    """把规则抽掉，断言必须翻面（铁律 14：双向可复现才叫可证伪）。

    只验一条就够：把 `.env.*` 那行从喂给探针的文本里拿掉，`.env.prod` 必须立刻
    变成「不被忽略」。翻不了面，说明它的「已忽略」另有出处，前面那些绿灯全都不算。
    """
    probe = ".env.prod"
    assert probe in _ignored([probe]), f"{probe} 本来就该被忽略，前提就不成立"

    stripped = "\n".join(
        ln for ln in _gitignore_text().split("\n") if ln.strip() != ".env.*"
    )
    assert stripped != _gitignore_text(), (
        "没找到 `.env.*` 那一行 —— 这条负向对照压根没改动任何东西，"
        "它的「翻面成功」会是假的。规则挪位置了就把这里同步改掉。"
    )
    assert probe not in _ignored([probe], gitignore_text=stripped), (
        f"抽掉 `.env.*` 之后 {probe} 仍被忽略 —— 那前面「`.env.*` 挡住了它」的绿灯"
        "并不是这条规则的功劳，秘密面的这道闸门实际由别的东西（或什么都没有）撑着。"
    )


def test_secret_shaped_paths_are_ignored():
    """一切长得像秘密的路径，必须进不来。"""
    paths = [p for p, _ in _MUST_BE_IGNORED]
    ignored = _ignored(paths)
    missing = [(p, why) for p, why in _MUST_BE_IGNORED if p not in ignored]
    assert not missing, "以下路径没有被 .gitignore 挡住：\n" + "\n".join(
        f"  · {p}  —— {why}" for p, why in missing
    )


def test_shipped_files_are_not_ignored():
    """一切必须发得出去的文件，不许被规则顺手挡掉。

    这是「补规则」这个动作的反面成本：`.env.*` 一写宽就会连坐 `.env.example`，
    `*.key` 一写宽就可能连坐真要发的东西。所以每加一条挡的，这里就得有一条放的。
    """
    paths = [p for p, _ in _MUST_SHIP]
    ignored = _ignored(paths)
    blocked = [(p, why) for p, why in _MUST_SHIP if p in ignored]
    assert not blocked, "以下必须发布的文件被 .gitignore 挡住了：\n" + "\n".join(
        f"  · {p}  —— {why}" for p, why in blocked
    )


def test_pruned_zones_are_provably_ignored():
    """走盘剪掉的每一个目录，都得能证明它被忽略（铁律 12：不许目录级豁免）。

    两路一起验，缺一路都留缝：

    1. **静态判据**：每条剪枝判据配的代表路径必须被忽略。这一路管「我声明要剪的」。
    2. **实际剪枝**：本次走盘真正剪掉的每一个相对路径，各自再证一遍。这一路管
       「我实际剪掉的」—— 判据一旦比证明宽（第一版的 `data*` vs `data_real/`），
       只有这一路抓得住。

    少了这条断言，剪枝表就退化成一张「我不看的地方」清单 —— 那正是目录级豁免
    本来的样子。
    """
    declared = {}
    for table in (_PRUNE_NAMES, _PRUNE_PREFIXES, _PRUNE_SUFFIXES, _PRUNE_RELPATHS):
        for judge, rep in table.items():
            declared[judge] = rep

    ignored = _ignored(list(declared.values()))
    unproven = {k: v for k, v in declared.items() if v not in ignored}
    assert not unproven, (
        "以下剪枝判据声明要剪，但 .gitignore 并没有忽略它的代表路径 —— "
        "剪掉却证明不了，就是目录级豁免：\n"
        + "\n".join(f"  · 判据 {k!r} → 代表路径 {v}" for k, v in sorted(unproven.items()))
    )

    _, actually_pruned = _walk_shipped_files()
    assert actually_pruned, (
        "本次走盘一个目录都没剪掉？那要么仓是空的，要么剪枝判据全失效了 —— "
        "两种情况下，下面这条「剪掉的都被忽略」都是一句空话。"
    )
    probes = {d: f"{d}/aidumem_prune_probe.txt" for d in sorted(actually_pruned)}
    ignored2 = _ignored(list(probes.values()))
    leaked = {d: p for d, p in probes.items() if p not in ignored2}
    assert not leaked, (
        f"走盘实际剪掉了 {len(actually_pruned)} 个目录，其中以下这些并没有被 "
        ".gitignore 忽略 —— 剪枝判据的射程超出了规则的射程：\n"
        + "\n".join(f"  · {d}/" for d in sorted(leaked))
        + "\n把判据收窄到规则实际覆盖的范围，或者给规则补上对应的一行。"
    )


def test_no_shipped_file_on_disk_is_ignored():
    """全盘走一遍：磁盘上长得像该发布的文件，一个都不许落在忽略规则里（铁律 11：集合比对）。

    前面几条是点名验的（我想到的路径）。这条是集合验的（磁盘上真有的文件），
    专治「新加一条规则，顺手把某个真在用的文件挡了，而我没想到点它的名」。
    """
    walked, _ = _walk_shipped_files()
    assert len(walked) > 100, (
        f"走盘只找到 {len(walked)} 个该发布的文件，太少了 —— 剪枝判据大概率把真源码"
        "也剪掉了。一个什么都没扫到的守卫换来一行绿色，是本仓反复吃过的亏。"
    )
    hit = sorted(_ignored(walked))
    assert not hit, (
        f"磁盘上有 {len(hit)} 个该发布的文件被 .gitignore 挡着：\n"
        + "\n".join(f"  · {h}" for h in hit[:40])
        + ("\n  …" if len(hit) > 40 else "")
    )
