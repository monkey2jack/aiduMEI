"""ducky.doc_facts — 对外文档关键数字的 machine-readable 源（v20.4.0-alpha · P0-3）

**为什么要有这个文件**：README 的内存口径曾自相矛盾 ——「280 vs 430 差 151 MB」
（430−280=150），「151 = onnxruntime 75 + 会话权重 122」（75+122=197）。
六方外审有四家转述了 151，**无一家做减法**。文档是我们被审判时的证词，
证词里的每个数字必须能当庭复算。

做法：关键数字在此登记（数值 + 口径 + 日期），
tests/test_v20_4_doc_numbers.py 用 find_doc_fact_violations() 比对 README 双语：
登记句必须逐字在场、被否定的旧口径必须缺席。改数字先改这里，文档跟着改；
两边对不上，守卫红。
"""

from __future__ import annotations

# ── 登记口径 ────────────────────────────────────────────────────
# 每项：(fact_key, 必须出现的子串, 口径/日期/环境)。
# 数值若重测变动，改这里并同步 README —— 守卫逼你两边一起改。
DOC_FACTS: tuple[tuple[str, str, str], ...] = (
    ("mem_cloud_mb", "约 280 MB",
     "云端档运行内存（2 核 3.5GB 生产口径实测，2026-08）"),
    ("mem_local_mb", "约 430 MB",
     "自动挡/本地档运行内存（同上口径）"),
    ("mem_delta_mb", "150 MB",
     "两档实测常驻差 = 430 − 280 = 150（v20.4.0 修正：旧文误写 151）"),
    ("mem_onnxruntime_mb", "75 MB",
     "onnxruntime 运行库只 import 不加载模型的构成项实测"),
    ("mem_model_session_mb", "约 122 MB",
     "bge-small-zh-v1.5 会话与权重的构成项实测"),
    ("cold_start_sec", "5.2s",
     "冷启动实测"),
    ("search_latency", "0.14~0.23s",
     "/search 单次实测区间"),
)

# 被否定的旧口径：这些子串在 README 双语里**不许出现**。
RETRACTED_CLAIMS: tuple[tuple[str, str], ...] = (
    ("151 MB", "旧内存差口径：430−280=150 不是 151；构成项 75+122=197 也拼不出 151"),
)


def find_doc_fact_violations(readme_zh: str, readme_en: str) -> list[str]:
    """比对双语 README 与登记口径，返回违规清单（空 = 全绿）。

    readme_en 的数值与中文一致但措辞不同：只查「被否定的旧口径缺席」
    与关键数值出现（280/430/150/75/122 五个裸数），不钉英文句式。
    """
    violations: list[str] = []
    for key, needle, provenance in DOC_FACTS:
        if needle not in readme_zh:
            violations.append(f"README.md 缺登记口径 {key}（{needle!r}，{provenance}）")
    for needle, reason in RETRACTED_CLAIMS:
        if needle in readme_zh:
            violations.append(f"README.md 仍含被否定的旧口径 {needle!r}（{reason}）")
        if needle in readme_en:
            violations.append(f"README_EN.md 仍含被否定的旧口径 {needle!r}（{reason}）")
    for number in ("280", "430", "150", "75", "122"):
        if number not in readme_en:
            violations.append(f"README_EN.md 缺关键数值 {number}（与中文登记口径同源）")
    return violations
