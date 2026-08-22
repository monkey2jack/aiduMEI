"""v20.0：开发依赖的「两半契约」—— 声明的一半必须和另一半对得上。

**这道护栏是为一个真实缺陷立的。** `requirements-dev.txt` 的表头一直写着
「与 pyproject.toml [project.optional-dependencies] dev 保持一致」，
而 pyproject 的 optional-dependencies 里只有 `mcp` / `sync` / `full` ——
**从来没有 dev 这一组**。一句关于「另一半」的话，指着一个不存在的东西，
而且没有任何东西会因此变红：清单文件不参与运行，注释更不会被执行。

这正是本仓反复撞见的同一个病：**一件事被写成两半，只有一半有人维护。**
`sync` extra 是同一个病的上一例（v19.4.2：依赖从未声明，照仓库部署必崩）。
区别只在于，上一次是「另一半根本没写」，这一次是「另一半写了但指错了」。

四条轴，各红在自己那一行：

① pyproject 里到底有没有 dev 组 —— 就是上面那个缺陷本身。
② 两个清单的内容必须逐项相等（**双向**：任何一边多写少写都红）。
③ README 必须告诉读者去哪装开发依赖 —— 否则照 README 走的人跑不了测试。
④ 不许声明 pytest-subtests。这是把一次实测结论焊死，防止 cargo cult：
   测试里的 8 个 subTest 用的是 pytest 9 的**内置** subtests 插件
   （`_pytest/subtests.py`，且 `"subtests" in _pytest.config.default_plugins`），
   不是第三方包。而且实测 pytest 7.4.4 上一个必然失败的 subTest 依旧
   `1 failed` / 退出码 1（只是不分条报），所以 `pytest>=7.0.0` 这个下限
   不会带来假绿灯。往清单里加一个我们并不需要的插件，只会让下一个人
   以为「不装它就有洞」，从而把一个不存在的问题当成真的。
"""

from __future__ import annotations

import os
import re
import tomllib

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQ_DEV = os.path.join(REPO_ROOT, "requirements-dev.txt")
PYPROJECT = os.path.join(REPO_ROOT, "pyproject.toml")
READMES = ("README.md", "README_EN.md")


def _normalize(spec: str) -> str:
    """把一条依赖声明压成可比较的形态：去掉所有空白、统一小写。

    只做这一步，不做完整 PEP 508 解析 —— 两个清单本来就该逐字一致，
    多做归一化反而会把「一边写 >=7.0.0 一边写 >=7」这种不一致抹平。
    """
    return re.sub(r"\s+", "", spec).lower()


def _read_requirements_dev() -> set[str]:
    out: set[str] = set()
    with open(REQ_DEV, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if line.startswith("-"):
                raise AssertionError(
                    f"{REQ_DEV}:{lineno} 出现了 pip 选项行 {line!r}。"
                    "这道护栏只会逐项比对普通依赖声明；请先教会它怎么处理这一行，"
                    "不要让它默默跳过 —— 跳过就等于这一行没人管。"
                )
            out.add(_normalize(line))
    return out


def _read_pyproject_extras() -> dict[str, list[str]]:
    with open(PYPROJECT, "rb") as fh:
        data = tomllib.load(fh)
    return data.get("project", {}).get("optional-dependencies", {})


def test_pyproject_declares_a_dev_extra():
    """① requirements-dev.txt 表头指向的那一组，必须真的存在。"""
    extras = _read_pyproject_extras()
    assert "dev" in extras, (
        "pyproject.toml [project.optional-dependencies] 里没有 dev 组，"
        f"但 requirements-dev.txt 的表头声称与它保持一致。现有的组：{sorted(extras)}。"
        "要么补上 dev 组，要么改掉那句话 —— 不许留着一句指向空气的承诺。"
    )


def test_dev_manifest_halves_match():
    """② 两个清单必须逐项相等，双向。"""
    extras = _read_pyproject_extras()
    declared = {_normalize(s) for s in extras.get("dev", [])}
    listed = _read_requirements_dev()
    only_in_txt = sorted(listed - declared)
    only_in_toml = sorted(declared - listed)
    assert not only_in_txt and not only_in_toml, (
        "开发依赖的两半不一致 ——\n"
        f"  只在 requirements-dev.txt 里：{only_in_txt}\n"
        f"  只在 pyproject dev extra 里：{only_in_toml}\n"
        "改一边就要改另一边，这就是这道护栏存在的全部理由。"
    )


def test_readme_tells_reader_to_install_dev_deps():
    """③ 照 README 走的人必须装得上开发依赖。"""
    missing = []
    for name in READMES:
        with open(os.path.join(REPO_ROOT, name), encoding="utf-8") as fh:
            if "requirements-dev.txt" not in fh.read():
                missing.append(name)
    assert not missing, (
        f"{missing} 没有任何一处提到 requirements-dev.txt。"
        "README 里写着 pytest 命令，却没告诉读者 pytest 从哪来 —— "
        "读者照着做会卡在第一步。"
    )


def test_pytest_subtests_is_not_declared_as_a_dependency():
    """④ 不许声明 pytest-subtests：内置插件已覆盖，且缺它也不会假绿。"""
    offenders = []
    for name in ("requirements.txt", "requirements-dev.txt"):
        path = os.path.join(REPO_ROOT, name)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, 1):
                line = raw.split("#", 1)[0].strip()
                if re.match(r"(?i)^pytest[-_]subtests\b", line):
                    offenders.append(f"{name}:{lineno}")
    for group, specs in _read_pyproject_extras().items():
        for spec in specs:
            if re.match(r"(?i)^pytest[-_]subtests\b", spec.strip()):
                offenders.append(f"pyproject[{group}]")
    assert not offenders, (
        f"{offenders} 声明了 pytest-subtests。subtests 是 pytest 9 的内置插件"
        "（_pytest/subtests.py），不是第三方包；且实测 pytest 7 上 subTest 失败"
        "照样红。加一个不需要的依赖，会让下一个人以为不装它就有洞。"
    )
