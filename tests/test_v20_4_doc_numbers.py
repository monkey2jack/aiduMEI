"""v20.4.0-alpha · P0-3：文档数字自洽守卫。

背景（小猴复核六方外审时撞见，六方全漏）：README 内存口径曾三处算不平 ——
「280 vs 430 差 151」（430−280=150）、「151 = 75 + 122」（合计 197）。
文档是被审判时的证词，证词不能算不平账。

本文件钉三件事：
1. 登记口径（ducky/doc_facts.py）在中文 README 逐字在场；
2. 被否定的旧口径（"151 MB"）在双语 README 都缺席；
3. 守卫自身的变异自证：喂一段含旧口径的假文本必须被抓出来。
"""
import os

from ducky.doc_facts import find_doc_fact_violations

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(name: str) -> str:
    with open(os.path.join(_ROOT, name), encoding="utf-8") as f:
        return f.read()


class TestDocNumbersConsistent:
    def test_readme_facts_match_registry(self):
        violations = find_doc_fact_violations(_read("README.md"), _read("README_EN.md"))
        assert violations == [], "文档数字与登记口径不符：\n  " + "\n  ".join(violations)

    def test_guard_detects_retracted_claim(self):
        """变异自证 A：旧口径 151 MB 混回来，守卫必须抓出来。"""
        bad_zh = "运行内存差 151 MB，详见上文。"
        violations = find_doc_fact_violations(bad_zh, "")
        assert any("151 MB" in v for v in violations), f"旧口径没被抓到：{violations}"

    def test_guard_detects_missing_fact(self):
        """变异自证 B：登记口径被删掉一条，守卫必须抓出来。"""
        violations = find_doc_fact_violations("这是一份什么都没有的 README", "")
        assert len(violations) >= 3, f"缺口径没被抓到：{violations}"
