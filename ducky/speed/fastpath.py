"""aiduMEM speed · 短文本快路径（白名单，宁缺毋滥）"""
from __future__ import annotations

import re
from typing import Optional

# ── 短文本快路径（白名单，宁缺毋滥）──
# 仅匹配「明确可结构化」的短句；其余一律走 LLM 全量抽取
_FAST_PATTERNS = [
    # 姓名/昵称
    re.compile(
        r"^(?:我(?:的)?(?:名字|姓名)(?:是|叫)|我叫|名字[=:：]|姓名[=:：])\s*(.+)$"
    ),
    re.compile(r"^(?:我(?:的)?昵称(?:是|叫)|昵称[=:：])\s*(.+)$"),
    # 生日
    re.compile(r"^(?:我(?:的)?生日(?:是)?|生日[=:：])\s*(.+)$"),
    # 偏好（喜欢/不喜欢）
    re.compile(r"^(?:我(?:很)?喜欢|喜欢)\s*(.+)$"),
    re.compile(r"^(?:我(?:不|很不)?喜欢|不喜欢|讨厌)\s*(.+)$"),
    # 简单 key=value / key：value
    re.compile(r"^([A-Za-z0-9_\u4e00-\u9fff]{1,20})\s*[=:：]\s*(.+)$"),
]

_FAST_SKIP = re.compile(
    r"^(好|嗯|哦|行|可以|是的|对|收到|了解|明白|知道了|谢谢|再见|拜拜|"
    r"ok|okay|yes|no|thanks|bye|got it)[!！。.]{0,3}$",
    re.IGNORECASE,
)


def try_fastpath_text(text: str) -> Optional[str]:
    """
    命中白名单则返回规范化事实句；否则 None。
    不做激进推断，宁可 miss 也不能脏写。
    """
    t = (text or "").strip()
    if not t or len(t) > 80:
        return None
    if _FAST_SKIP.match(t):
        return None
    # 排除问句 / 命令
    if any(x in t for x in ("？", "?", "帮我", "请", "怎么", "为什么", "是否")):
        return None

    for i, pat in enumerate(_FAST_PATTERNS):
        m = pat.match(t)
        if not m:
            continue
        if i == 0:
            return f"姓名是{m.group(1).strip()}"
        if i == 1:
            return f"昵称是{m.group(1).strip()}"
        if i == 2:
            return f"生日是{m.group(1).strip()}"
        if i == 3:
            return f"喜欢{m.group(1).strip()}"
        if i == 4:
            return f"不喜欢{m.group(1).strip()}"
        if i == 5:
            k, v = m.group(1).strip(), m.group(2).strip()
            if not k or not v or len(v) > 60:
                return None
            return f"{k}是{v}"
    return None
