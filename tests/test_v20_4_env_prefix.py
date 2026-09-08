"""v20.4.0-alpha · P1-10：env 变量前缀混排台账与棘轮。

背景（六方外审 Qwen 独见实锤）：全仓 90+ 个 env 变量混用 `AIDUMEM_` /
`AIDUMEI_` 两套前缀（如数据目录是 AIDUMEM_DATA_DIR，反代逃生阀是
AIDUMEI_TRUST_PROXY），env_registry.py 文首自述这是「作者本人踩了两次」的坑。

env_registry 的既定学说：`AIDUMEM_` 是**冻结兼容旧前缀**，`AIDUMEI_` 是
**当前前缀**。本文件把学说落成棘轮：
1. 现存 AIDUMEM_ 变量冻结进 FROZEN_LEGACY_AIDUMEM —— 新增旧前缀变量 → 红
   （新变量一律走 AIDUMEI_）；
2. 冻结集与 KNOWN_ENV_VARS 同步 —— 冻结集里的名字从真相源消失 → 红（防台账烂掉）；
3. 两个新前缀变量组（P1-7 的密钥三件套）必须用 AIDUMEI_ 前缀。
"""
from ducky.env_registry import FROZEN_LEGACY_AIDUMEM, KNOWN_ENV_VARS


class TestPrefixDoctrine:
    def test_frozen_legacy_set_is_exactly_the_aidumem_subset(self):
        """冻结集必须与真相源里的 AIDUMEM_ 子集完全相等：少登记 → 红，
        多登记（名字已从代码消失）→ 红，新增旧前缀变量 → 红。"""
        actual = {v for v in KNOWN_ENV_VARS if v.startswith("AIDUMEM_")}
        assert actual == FROZEN_LEGACY_AIDUMEM, (
            f"AIDUMEM_ 旧前缀集合漂移：新增旧前缀 {sorted(actual - FROZEN_LEGACY_AIDUMEM)}"
            f"（新变量请用 AIDUMEI_）；台账多记 {sorted(FROZEN_LEGACY_AIDUMEM - actual)}"
        )

    def test_frozen_set_is_nonempty_anchor(self):
        """锚定已知存量，防集合被整体清空后守卫永真。"""
        assert "AIDUMEM_DATA_DIR" in FROZEN_LEGACY_AIDUMEM
        assert "AIDUMEM_API_TOKEN" in FROZEN_LEGACY_AIDUMEM
        assert len(FROZEN_LEGACY_AIDUMEM) >= 30, "冻结集规模异常收缩，疑似被整体改写"

    def test_every_known_var_follows_one_of_two_prefixes(self):
        for v in KNOWN_ENV_VARS:
            assert v.startswith("AIDUMEM_") or v.startswith("AIDUMEI_"), \
                f"{v} 不属于任何一套既定前缀"

    def test_p1_7_key_vars_use_current_prefix(self):
        """P1-7 新增的密钥覆盖变量必须走当前前缀 AIDUMEI_（学说：新变量不进旧前缀）。"""
        for v in ("AIDUMEI_LLM_API_KEY", "AIDUMEI_EMBEDDER_API_KEY", "AIDUMEI_RERANKER_API_KEY"):
            assert v in KNOWN_ENV_VARS
            assert v.startswith("AIDUMEI_")
