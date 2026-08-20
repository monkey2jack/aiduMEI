"""
tests/test_release_hygiene.py — 发布卫生守卫（v19.5.0）

守的是 `scripts/release_scan.py` 这道闸门本身。

为什么要给扫描器写测试
──────────────────────
因为**扫描器坏掉的样子，和项目干净的样子，一模一样**：都是「0 命中」。
这类工具的失败是沉默的 —— 它不报错、不崩溃，只是安静地发放一张
「已经检查过了」的凭证。历史上我们就是这么漏掉了发行包这一面：
词表少了一个词，扫描照跑，报告全绿，脏包上了公开索引。

所以这里的测试不问「扫描器能不能找到脏东西」，而是问：
**当扫描器坏掉时，它会不会老老实实地失败？**

覆盖：
1. 词表外置：模块内**没有**任何内置词表兜底（空环境必须拒绝运行）
2. 空词表 / 词表文件缺失 → 拒绝运行，绝不放行
3. 自检三向对照：脏样本报警、干净样本不报、豁免不外溢
4. **自检本身可证伪**：把扫描逻辑打坏，自检必须抓到（否则自检是摆设）
5. 行内豁免只作用于本行，不向文件/目录扩散
6. 豁免从失败计数里消失，但必须仍出现在报告里
7. 计数语义是「出现次数」而非「文件数」（踩过的坑）
8. IPv4 八位组边界：SVG 路径数据不得被当成 IP（假红会淹掉真红）
9. 七个公开面的清单不得被悄悄缩短
"""

import os
import sys

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from scripts import release_scan as rs  # noqa: E402


# ── 1 / 2：词表外置，且空词表必须拒绝运行 ─────────────────────────────

def test_module_has_no_builtin_wordlist():
    """空环境下必须抛异常 —— 证明模块里没有任何内置词表兜底。

    这条同时守着「词表绝不入仓」：只要还有一个能让它跑起来的内置默认值，
    就说明有词写在了代码里。
    """
    with pytest.raises(rs.WordlistMissing):
        rs.load_words({})


def test_empty_wordlist_refuses_to_run():
    """空字符串、纯分隔符、纯空白都算空 —— 不能被糊弄成「有词表」。"""
    for bogus in ("", "|", "||", "  ", " | | "):
        with pytest.raises(rs.WordlistMissing):
            rs.load_words({"AIDUMEI_SCAN_WORDS": bogus})


def test_missing_wordlist_file_refuses_to_run():
    with pytest.raises(rs.WordlistMissing):
        rs.load_words({"AIDUMEI_SCAN_WORDLIST": "/nonexistent/aidumei/words.txt"})


def test_wordlist_file_is_parsed(tmp_path):
    p = tmp_path / "w.txt"
    p.write_text("# 注释行\n甲\n\n  乙  \n", encoding="utf-8")
    assert rs.load_words({"AIDUMEI_SCAN_WORDLIST": str(p)}) == ["乙", "甲"]


def test_wordlist_sources_merge_and_dedupe(tmp_path):
    p = tmp_path / "w.txt"
    p.write_text("甲\n丙\n", encoding="utf-8")
    got = rs.load_words({"AIDUMEI_SCAN_WORDS": "甲|乙",
                         "AIDUMEI_SCAN_WORDLIST": str(p)})
    assert got == ["丙", "乙", "甲"]


# ── 3 / 4：自检三向对照，而且自检本身必须可证伪 ────────────────────────

def test_selftest_passes_on_healthy_scanner():
    rs.selftest(["某个真词"])


@pytest.mark.parametrize("broken", ["瞎子", "疯子", "漏勺"])
def test_selftest_catches_a_broken_scanner(monkeypatch, broken):
    """**本文件里最重要的一条。**

    把扫描逻辑打坏成三种典型故障，自检必须每一种都抓到。
    如果自检抓不到，那它就只是一句「通过」的空话 ——
    一个永远通过的自检，和没有自检完全等价。

      瞎子：什么都不报         → 会把脏包放行（我们真实犯过的错）
      疯子：什么都报           → 假红淹没真红，人开始忽略输出
      漏勺：豁免标记溢出全文件 → 目录级豁免的病灶，加一行注释就能赦免整个文件
    """
    if broken == "瞎子":
        monkeypatch.setattr(rs, "scan_bytes", lambda raw, words: ({}, {}))
    elif broken == "疯子":
        monkeypatch.setattr(rs, "scan_bytes",
                            lambda raw, words: ({w: 1 for w in words}, {}))
    else:  # 漏勺：只要文件里出现过标记，整个文件都算豁免
        real = rs.scan_bytes

        def leaky(raw, words):
            hits, ex = real(raw, words)
            if rs.ALLOW_MARK.encode() in raw:
                for k, n in hits.items():
                    ex[k] = ex.get(k, 0) + n
                hits = {}
            return hits, ex

        monkeypatch.setattr(rs, "scan_bytes", leaky)

    with pytest.raises(rs.SelfTestFailed):
        rs.selftest(["某个真词"])


# ── 5 / 6：行内豁免的射程与可见性 ──────────────────────────────────────

def test_allow_mark_is_scoped_to_its_own_line(tmp_path):
    """标记只赦免它所在的那一行；同文件的其他行照报。"""
    f = tmp_path / "a.txt"
    f.write_text(f"秘密词  # {rs.ALLOW_MARK} 夹具\n秘密词\n秘密词\n", encoding="utf-8")
    found, waived, _, _ = rs.scan_tree(tmp_path, ["秘密词"])
    assert waived["秘密词"]["a.txt"] == 1
    assert found["秘密词"]["a.txt"] == 2


def test_allow_mark_does_not_leak_to_other_files(tmp_path):
    """严禁目录级豁免：一个文件里的标记不得赦免同目录的另一个文件。"""
    (tmp_path / "marked.txt").write_text(
        f"秘密词 # {rs.ALLOW_MARK}\n", encoding="utf-8")
    (tmp_path / "other.txt").write_text("秘密词\n", encoding="utf-8")
    found, _, _, _ = rs.scan_tree(tmp_path, ["秘密词"])
    assert found["秘密词"] == {"other.txt": 1}


def test_waived_hits_stay_visible_in_the_report(tmp_path):
    """豁免从**失败计数**里消失，但绝不从**报告**里消失。

    看不见的豁免会一年年堆积，直到没人知道自己在赦免什么。
    """
    (tmp_path / "a.txt").write_text(f"秘密词 # {rs.ALLOW_MARK}\n", encoding="utf-8")
    found, waived, scanned, skipped = rs.scan_tree(tmp_path, ["秘密词"])
    text, total = rs.format_report("t", found, waived, ["秘密词"], scanned, skipped)
    assert total == 0                      # 不计入失败
    assert "秘密词" in text                 # 但必须看得见
    assert "a.txt" in text
    assert rs.ALLOW_MARK in text


# ── 7：计数语义 ────────────────────────────────────────────────────────

def test_counts_occurrences_not_files(tmp_path):
    """×N 是**出现次数**，不是文件数。

    旧扫描器这里给的是文件数，导致一个文件里出现 5 次被汇报成「×1」。
    汇报口径错了，等于把风险说小了。
    """
    (tmp_path / "a.txt").write_text("秘密词\n秘密词\n别的\n秘密词\n", encoding="utf-8")
    found, _, _, _ = rs.scan_tree(tmp_path, ["秘密词"])
    assert found["秘密词"]["a.txt"] == 3


def test_binary_files_are_skipped_not_called_clean(tmp_path):
    """读不出来的东西要如实计入 skipped —— **跳过不等于干净**。"""
    (tmp_path / "b.bin").write_bytes(b"\x00\x01\x02" + "秘密词".encode("utf-8"))
    (tmp_path / "a.txt").write_text("干净\n", encoding="utf-8")
    _, _, scanned, skipped = rs.scan_tree(tmp_path, ["秘密词"])
    assert scanned == 1 and skipped == 1


# ── 8：IPv4 八位组边界 ────────────────────────────────────────────────

def test_ipv4_octet_bounds_reject_svg_path_data():
    """SVG 的路径数据形如 315.225.69.825，八位组越界，不得报成 IP。

    少了这个边界，一个图标文件就能刷出上百条假红，把真命中淹掉。
    """
    hits, _ = rs.scan_bytes(b"M 315.225.69.825 L 999.888.777.666", [])
    assert not hits


def test_ipv4_reserved_ranges_are_not_leaks():
    """私有网段与 RFC 5737 文档地址本来就是占位符，不算泄露。"""
    sample = b"127.0.0.1 10.1.2.3 192.168.1.1 172.16.0.1 203.0.113.9 198.51.100.7"
    hits, _ = rs.scan_bytes(sample, [])
    assert not hits


def test_ipv4_public_address_is_caught():
    """正向对照：真的公网地址必须报出来，否则上面两条「不报」毫无意义。

    夹具用的是一个众所周知的公共 DNS 地址，与本项目基础设施无关；
    两行都打了行内豁免标记 —— 顺带也是这套机制的活样例。
    """
    hits, _ = rs.scan_bytes(b"connect to 8.8.4.4 now", [])  # release-scan:allow 公共DNS夹具
    assert "公网IPv4:8.8.4.4" in hits  # release-scan:allow 公共DNS夹具


# ── 9：七个公开面的清单不得缩短 ────────────────────────────────────────

def test_seven_surfaces_are_all_declared():
    """六面变七面是 v19.5.0 的实质进步：新增「⑥ 包索引渲染面」——
    元数据渲染出来的项目网页，**不下载就能看见**，是最容易漏的一面。
    这条守卫防止有人日后把清单悄悄改短。
    """
    assert len(rs.SURFACES) == 7
    for key in ("工作区", "提交信息", "标签注释", "发布说明",
                "发行包内容", "包索引渲染面", "对外可见归档"):
        assert any(key in s for s in rs.SURFACES), f"公开面清单缺了：{key}"
