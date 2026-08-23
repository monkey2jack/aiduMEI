"""
v20.0 守卫④：文档里引用的本地文件必须真的存在。

**这条守卫来自一次实测到的假绿灯，不是假想。**

``docs/README_draft.md`` 里有 16 处 ``<img src="docs/screenshots/…">``。那份草稿
自己就在 ``docs/`` 下，相对路径解析出来是 ``docs/docs/screenshots/…`` —— 从写下
的第一天起就是坏链，从 v19.4.0 一直躺到 v20.0，全量测试**一次都没红过**。

v20.0 移除随附截图时补做了负向对照：往 ``README.md`` 里植入一处指向已删除图片的
``<img>``，跑全量 —— **839 passed，没有任何守卫抓到**。这就是缺口本身。

坏链的代价不对称：仓库自测永远是绿的，坏的只有别人打开页面那一刻看到的碎图。
所以它必须由静态守卫接住，而不能指望谁去肉眼复查 README。

**判据是「存在」，不是「像个路径」。**逐条按 md 文件自身所在目录解析，落盘 stat。
外链（``http(s):``、``data:``、``//``、页内锚点 ``#``）不在射程内 —— 那要联网才能验，
不该混进单元测试。

**代码片段里的路径不算引用。**这条守卫刚落地就在自己身上误报了一次: CHANGELOG.md 里
描述本次改动时写了 ``` `<img src="docs/screenshots/…">` ``` 作说明文字，被当成了真引用。
markdown 渲染时反引号里的东西是代码、不会变成图片，判它坏链是错的。误报比漏报更毒 ——
它会逼后人往白名单里塞假条目，名单一脏整条守卫就废了。所以提取前先剥掉围栏代码块与
行内代码，宁可在这一层多花几行，也不开白名单这个口子。

射程地板见 ``_MD_FLOOR`` / ``_REF_FLOOR``：谁把 rglob 收窄、或把提取正则改坏导致
一处也提不出来，守卫会继续全绿而什么都没查。少查看不见，所以把它焊成断言。
"""

import pathlib
import re

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

_SKIP_PARTS = frozenset({
    "__pycache__", ".venv", "venv", "node_modules", ".git",
    "build", "dist", ".eggs", ".pytest_cache",
})

# 射程地板。当前实测：16 个 md、3 处本地引用。留出余量，但收窄到 0 必红。
_MD_FLOOR = 10
_REF_FLOOR = 2

# 两种写法都要认：HTML 的 <img src>，和 markdown 的 ![alt](path)。
# 少认一种，那一种就是下一个坏链的藏身处。
_REF_RE = re.compile(
    r'(?:<img[^>]+src=["\']([^"\']+)["\']|!\[[^\]]*\]\(([^)\s]+)\))'
)

# 不落盘、验不了的引用形态。
_EXTERNAL_RE = re.compile(r'^(?:https?:|data:|mailto:|//|#)')

# 代码片段: 围栏块（``` / ~~~）与行内代码（任意个数反引号）。
# 顺序要紧 —— 先剥围栏，再剥行内，否则围栏里的单反引号会把配对啃乱。
_FENCE_RE = re.compile(r'^(```|~~~).*?^\1', re.MULTILINE | re.DOTALL)
_INLINE_CODE_RE = re.compile(r'(`+)(?:(?!\1).)*?\1', re.DOTALL)


def _strip_code(text: str) -> str:
    """抹掉代码片段，但保留换行 —— 行号语义不变，剩下的才是真会渲染成图片的部分。"""
    def _blank(m: re.Match) -> str:
        return "\n" * m.group(0).count("\n")
    return _INLINE_CODE_RE.sub(_blank, _FENCE_RE.sub(_blank, text))


def _markdown_files() -> list[pathlib.Path]:
    return sorted(
        p for p in _REPO_ROOT.rglob("*.md")
        if not any(part in _SKIP_PARTS for part in p.parts)
    )


def _local_refs() -> list[tuple[pathlib.Path, str]]:
    """返回 [(md 文件, 被引用的本地相对路径), …]。"""
    out: list[tuple[pathlib.Path, str]] = []
    for md in _markdown_files():
        text = _strip_code(md.read_text(encoding="utf-8", errors="ignore"))
        for m in _REF_RE.finditer(text):
            target = m.group(1) or m.group(2)
            if _EXTERNAL_RE.match(target):
                continue
            out.append((md, target))
    return out


def test_scope_is_wide_enough():
    """守卫的射程本身要被守 —— 收窄了必须当场看得出来。"""
    mds = _markdown_files()
    assert len(mds) >= _MD_FLOOR, (
        f"射程只剩 {len(mds)} 个 .md（地板 {_MD_FLOOR}）—— "
        "排除表或遍历起点被收窄了，守卫会继续全绿但什么都没查")

    rels = {p.relative_to(_REPO_ROOT).as_posix() for p in mds}
    for must in ("README.md", "README_EN.md", "CHANGELOG.md"):
        assert must in rels, f"{must} 不在射程内 —— 这个位置必须被查"


def test_reference_extractor_still_finds_references():
    """提取器改坏了会静默交白卷。用地板把这种退化钉住。"""
    refs = _local_refs()
    assert len(refs) >= _REF_FLOOR, (
        f"全仓只提取到 {len(refs)} 处本地引用（地板 {_REF_FLOOR}）—— "
        "提取正则或射程被改坏了，零坏链这个结论不成立")


def test_extractor_recognises_both_syntaxes():
    """两种语法各自都要认得出。少认一种，等于给坏链留一条暗道。"""
    sample = (
        '<img src="a/one.png" width="48%">\n'
        '![alt text](b/two.png)\n'
        '<img src="https://example.com/x.png">\n'
        '![ext](data:image/png;base64,AAAA)\n'
    )
    found = []
    for m in _REF_RE.finditer(sample):
        target = m.group(1) or m.group(2)
        if _EXTERNAL_RE.match(target):
            continue
        found.append(target)
    assert found == ["a/one.png", "b/two.png"], (
        f"提取器认出的是 {found} —— 两种语法必须都认，外链必须都滤掉")


def test_code_snippets_are_not_treated_as_references():
    """文档在**讲**一处引用时，那不是引用。这条来自守卫落地当天的一次真误报。"""
    sample = (
        "写法是 `<img src=\"docs/screenshots/gone.png\">` 这样。\n"
        "```html\n<img src=\"fenced/also-gone.png\">\n```\n"
        '<img src="real/kept.png">\n'
    )
    found = []
    for m in _REF_RE.finditer(_strip_code(sample)):
        target = m.group(1) or m.group(2)
        if _EXTERNAL_RE.match(target):
            continue
        found.append(target)
    assert found == ["real/kept.png"], (
        f"提取器认出的是 {found} —— 行内代码与围栏代码块里的示例路径都不该算引用，"
        "但代码块外面那条真引用必须还认得出（别把剥代码写成把全文剥空）")


def test_no_broken_local_asset_links():
    """本条是主判据：文档引用的每一个本地文件都必须存在。"""
    broken = [
        f"{md.relative_to(_REPO_ROOT).as_posix()} → {target}"
        for md, target in _local_refs()
        if not (md.parent / target).exists()
    ]
    assert not broken, (
        "文档里有指向不存在文件的引用（页面上就是碎图，仓库自测却是绿的）：\n  "
        + "\n  ".join(broken)
        + "\n修法二选一：补上文件，或把引用一并删掉 —— 不许留着。"
    )
