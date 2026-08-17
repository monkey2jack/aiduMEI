"""
tests/test_v19_4_0_noise.py — v19.4.0 审计修复 🟡-A 回归测试
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

生产审计 🟡-A：_is_noise 只认空/纯符号/单字符复读，
「asdfgh jkl 12345 xxxxx qqqq zzzz」这类随机词组合乱码漏网进 LLM 评估。
修复：token 级垃圾判定（键盘连续段/重复字符/数字连续段/纯符号），
全部 token 皆垃圾才判噪声。红线：宁窄勿宽，正常文本绝不误杀。
"""

import os
import sys

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from ducky.governance import _is_junk_token, _is_noise, rule_screen


# ─────────────────────────────────────────────────────────────
# 1. 新增乱码样本必须判噪声
# ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("sample", [
    "asdfgh jkl 12345 xxxxx qqqq zzzz",   # 审计报告原样样本
    "asdfgh",                              # 键盘横敲
    "jkl",                                 # 键盘连续段
    "zzzz",                                # 重复字符
    "12345",                               # 数字连续
    "54321",                               # 数字逆序连续
    "!!! ??? ...",                         # 纯符号 token 组合
    "qwerty uiop",                         # 键盘两行段
    "zxcvbn",                              # 键盘底行
])
def test_random_mash_is_noise(sample):
    assert _is_noise(sample) is True, f"应判噪声: {sample!r}"
    assert rule_screen("general", "k", sample) == ("reject", "rule:noise")


# ─────────────────────────────────────────────────────────────
# 2. 既有噪声样本不回退
# ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("sample", [
    "",
    "。",
    "哈哈哈哈哈哈哈哈",
    "。。。。。。",
])
def test_legacy_noise_still_rejected(sample):
    assert _is_noise(sample) is True


# ─────────────────────────────────────────────────────────────
# 3. 宁窄勿宽：正常文本绝不误杀
# ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("sample", [
    "喜欢喝热拿铁，不加糖",
    "每周三下午开组会",
    "I like coffee",
    "hello world",
    "asdf 是真的吗",          # 含 CJK 一律放行交 LLM
    "version 2.0 released",
    "abc def",               # abc/def 不是键盘连续段
    "test 123",              # 123 只有 3 位连续但 test 是正常词 → 有正常 token
    "My email is foo@bar.com",
    "订单号 20260817001",
])
def test_normal_text_not_noise(sample):
    assert _is_noise(sample) is False, f"误杀正常文本: {sample!r}"
    assert rule_screen("general", "k", sample)[0] != "reject" or \
        rule_screen("general", "k", sample)[1] != "rule:noise"


# ─────────────────────────────────────────────────────────────
# 4. token 级判定单元
# ─────────────────────────────────────────────────────────────

def test_junk_token_units():
    assert _is_junk_token("asdfgh") is True
    assert _is_junk_token("jkl") is True
    assert _is_junk_token("qqqq") is True
    assert _is_junk_token("12345") is True
    assert _is_junk_token("!!!") is True
    assert _is_junk_token("hello") is False
    assert _is_junk_token("abc") is False
    assert _is_junk_token("2026") is False   # 非连续数字
    assert _is_junk_token("a1b2") is False
