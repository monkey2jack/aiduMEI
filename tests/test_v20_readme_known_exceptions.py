"""
v20.0 守卫⑤：README 声称记忆域隔离，就必须挂一份「已知例外」清单。

**这条守卫来自本轮自查里唯一一个确认成立的文档缺口（甲10 ②）。**

v20.0 的 README 写着「全量记忆域隔离」，正文写着「每一条路径都必须显式说出自己在
哪个域上工作」，版本表写着「贯通写查删恢复统计反馈账本与毕业链」。而同一时刻仓库里
真实存在三处不覆盖：``core_memory`` 键形状推迟（甲1b）、全库维护作业不按域隔离
（乙1）、存量数据域归属未对账（丙9）。八个例外类词（局限／尚未／本版不／不覆盖／
例外／推迟／已知缺口／下一版）在 README 里**一个都搜不到**。

声称「全量」而不列例外，代价不由我们付 —— 由那个照着 README 上生产、然后发现边界
的人付。

**为什么判据不是「检测绝对化措辞」。**

第一版设计是扫「全量／贯通／每一条路径」这类词，命中就要求例外章节。这是**词表式**
判据，跟这个项目栽过的坑同形：补的是那一个词，没补那个模式 —— 下一版把「全量」换成
「完整」「端到端」「无死角」，守卫当场瞎掉，而 README 的过度承诺一字未减。

所以判据反过来：**无条件要求例外章节存在**。域隔离是 v20 的主题，README 必然要讲；
既然必然讲，就不必先证明它讲得有多绝对 —— 章节永远该在。这样任何措辞变化都逃不掉，
因为守卫根本不看措辞。

**空壳标题也要挡。**只断言标题存在，等于邀请后人留个空章节交差。所以加射程地板：
章节内至少 ``_MIN_EXCEPTION_ROWS`` 行表格数据行。

**例外必须点名真实代码位点。**「我们有一些已知例外」是空话；「``ducky/evolve_mem.py``
按全库扫描」是可核对的事实。所以章节里反引号引用的 ``ducky/*.py`` 必须真的落盘存在
—— 复用守卫④「判据是存在，不是像个路径」的同一条纪律。
"""

import pathlib
import re

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# 两份 README 各自的章节标题。中英分开写死 —— 英文版不该被中文标题满足，反之亦然。
_REQUIRED_SECTIONS = {
    "README.md": "## 已知例外与本版不覆盖",
    "README_EN.md": "## Known Limitations & Not Covered",
}

# 射程地板：当前实测两侧各 3 行例外（甲1b / 乙1 / 丙9）。
# 收窄到 0 会让「章节存在」退化成空壳标题，所以焊成断言。
_MIN_EXCEPTION_ROWS = 3

# 章节内必须点名的源码位点最少条数。当前实测：中文 3 个、英文 3 个。
_MIN_CITED_SOURCES = 3

_TABLE_ROW = re.compile(r"^\|\s*\d+\s*\|")           # 表格数据行：以 | 数字 | 开头
_SRC_REF = re.compile(r"`(ducky/[A-Za-z0-9_/]+\.py)`")  # 反引号里的源码路径


def _read(name: str) -> str:
    path = _REPO_ROOT / name
    assert path.is_file(), f"{name} 不在仓库里"
    return path.read_text(encoding="utf-8")


def _section_body(text: str, heading: str) -> str:
    """取出 heading 到下一个同级标题之间的正文。"""
    start = text.find(heading)
    assert start >= 0, f"找不到章节标题：{heading}"
    rest = text[start + len(heading):]
    nxt = rest.find("\n## ")
    return rest if nxt < 0 else rest[:nxt]


def test_both_readmes_have_known_exceptions_section():
    """域隔离的声称必须与例外清单同时存在 —— 无条件，不看措辞。"""
    for name, heading in _REQUIRED_SECTIONS.items():
        text = _read(name)
        assert heading in text, (
            f"{name} 缺少「已知例外」章节（应含标题 {heading!r}）。"
            "README 讲了域隔离就必须同时列出不覆盖的边界；"
            "把边界留给用户在生产上撞，是这个项目付过学费的失败形态。"
        )


def test_known_exceptions_section_is_not_a_stub():
    """射程地板：章节里必须真的列出例外，不能只留个标题交差。"""
    for name, heading in _REQUIRED_SECTIONS.items():
        body = _section_body(_read(name), heading)
        rows = [ln for ln in body.splitlines() if _TABLE_ROW.match(ln.strip())]
        assert len(rows) >= _MIN_EXCEPTION_ROWS, (
            f"{name} 的「已知例外」章节只有 {len(rows)} 行例外，"
            f"少于地板 {_MIN_EXCEPTION_ROWS}。空壳标题比没有标题更坏 —— "
            "它让读者以为边界已经交代过了。"
        )


def test_known_exceptions_cite_existing_source_files():
    """例外必须点名可核对的代码位点，且那些文件必须真的存在。"""
    for name, heading in _REQUIRED_SECTIONS.items():
        body = _section_body(_read(name), heading)
        cited = sorted(set(_SRC_REF.findall(body)))
        assert len(cited) >= _MIN_CITED_SOURCES, (
            f"{name} 的「已知例外」章节只点名了 {len(cited)} 个源码位点，"
            f"少于地板 {_MIN_CITED_SOURCES}。"
            "「我们有一些已知例外」是空话，「evolve_mem 按全库扫描」才是可核对的事实。"
        )
        missing = [p for p in cited if not (_REPO_ROOT / p).is_file()]
        assert not missing, (
            f"{name} 的「已知例外」章节引用了不存在的源码文件：{missing}。"
            "判据是「存在」，不是「像个路径」。"
        )


def test_extractors_are_not_silently_broken():
    """守卫的第一份工作是抓住它自己的作者：提取器坏掉必须当场红，而不是全绿。"""
    sample = "| 1 | **`core_memory` 键形状** | 表主键仍是单列 `block_key` |"
    assert _TABLE_ROW.match(sample), "表格行提取器坏了 —— 它一坏，空壳检查就永远绿"
    assert _SRC_REF.findall("见 `ducky/evolve_mem.py` 与 `ducky/routes_evolve.py`") == [
        "ducky/evolve_mem.py",
        "ducky/routes_evolve.py",
    ], "源码引用提取器坏了 —— 它一坏，点名检查就永远绿"
    assert _SRC_REF.findall("`block_key`") == [], "提取器把非源码路径也当成了引用"
