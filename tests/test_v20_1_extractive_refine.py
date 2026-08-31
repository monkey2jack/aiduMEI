"""tests/test_v20_1_extractive_refine.py — v20.1 WP-B 无 LLM 整合升级

四层断言，对应预案验收门槛：

1. **纯函数层** `_extractive_summary`：确定性（两遍逐字节一致）、硬实体
   （数字/版本/日期）保留、值包含去重、显式截断标注、空值兜底。
2. **信息量对照（区分力）**：同一组候选，extractive 摘要保留的事实值
   **严格多于** rule 摘要 —— 把旧路径当对照组，证明这一档不是白加的。
3. **降级链**：llm → extractive → rule 三档 basis 记账正确；提取式失败
   必须发 warning（真实降级要出声）并稳落 rule 档。
4. **apply / rollback 兼容**：三档产物走同一套账本通道，归档与恢复可逆。

附带项：facts 水位阈值配置化（AIDUMEI_FACTS_WATERMARK），默认 800 与
v20.0.1 行为一致；生效值必须能从 /health 读出（配置生效三查：问解析者，
不问文件）；显式值非法时报警进探针、回退默认，绝不安静吞掉。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ducky.refine_memory as refine_memory  # noqa: E402
from ducky.refine_memory import (  # noqa: E402
    _EXTRACTIVE_MAX_POINTS,
    _extractive_summary,
    _rule_summary,
    apply_refinement,
    refine_group,
    rollback_refinement,
)

# Full /health diagnostics now require a valid credential.
def _health_token(monkeypatch):
    token = "test-health-token"
    monkeypatch.setenv("AIDUMEM_API_TOKEN", token)
    return token


def _mk(items):
    """[(key, value), ...] → refine 候选形状。"""
    return [
        {"id": i + 1, "category": "wpb_cat", "fact_key": k, "fact_value": v}
        for i, (k, v) in enumerate(items)
    ]


# ══════════════════════════════════════════════════════════════════
# 1. 纯函数层
# ══════════════════════════════════════════════════════════════════

class TestExtractiveSummary:
    def test_determinism_byte_identical(self):
        items = _mk([(f"键{i}", f"值{i}·端口{8000 + i}") for i in range(20)])
        assert _extractive_summary(items) == _extractive_summary(items)

    def test_hard_entities_preserved(self):
        """数字承载的硬事实（版本/端口/日期）一条都不许在要点内丢。"""
        facts = [
            ("基座版本", "mem0ai 2.0.19"),
            ("服务端口", "8767"),
            ("收口日期", "2026-08-25"),
            ("阈值", "30 天"),
        ]
        summary = _extractive_summary(_mk(facts))
        for _, value in facts:
            assert value in summary, f"硬实体 {value!r} 被摘要丢掉了"

    def test_values_survive_not_just_keys(self):
        """这是与 rule 档的本质差别：值必须在场，不能只剩目录。"""
        summary = _extractive_summary(_mk([("默认分支", "main"), ("语言", "中文")]))
        assert "main" in summary and "中文" in summary

    def test_substring_dedup_absorbs_short_into_long(self):
        items = _mk([("a", "部署完成"), ("b", "部署完成于生产机房 v20 树")])
        summary = _extractive_summary(items)
        assert summary.count("部署完成") == 1, "短值没有被长值吸收"

    def test_truncation_is_explicitly_marked(self):
        """超出要点上限必须写「另 N 条要点略」—— 沉默截断等于谎报全合并。"""
        n = _EXTRACTIVE_MAX_POINTS + 5
        items = _mk([(f"独键{i}", f"独立事实内容第{i}号") for i in range(n)])
        summary = _extractive_summary(items)
        assert "另 5 条要点略" in summary

    def test_within_limit_has_no_truncation_marker(self):
        items = _mk([(f"键{i}", f"事实{i}") for i in range(3)])
        assert "要点略" not in _extractive_summary(items)

    def test_all_empty_values_yield_empty_string(self):
        """产不出要点就返回空串，让调用方降到 rule 档 —— 不产出空壳摘要。"""
        assert _extractive_summary(_mk([("k1", ""), ("k2", "  ")])) == ""


def test_extractive_beats_rule_on_information_content():
    """区分力对照：同一组候选，rule 档丢掉全部事实值，extractive 档保住。

    不比「长度」比「内容」：数一数源事实值有几条出现在摘要里。
    rule 档按构造只拼 fact_key，值保留数应为 0；extractive 档应 > 0 且
    覆盖全部硬实体 —— 两条腿都断言，让对照真的能分出好坏。
    """
    facts = [(f"配置项{i}", f"取值{i}·刻度{i * 111}") for i in range(10)]
    items = _mk(facts)
    extractive = _extractive_summary(items)
    rule = _rule_summary(items)

    kept_by_extractive = sum(1 for _, v in facts if v in extractive)
    kept_by_rule = sum(1 for _, v in facts if v in rule)
    assert kept_by_rule == 0, "rule 档竟然保留了值 —— 对照前提失效，先修这条测试"
    assert kept_by_extractive == len(facts), (
        f"extractive 只保留 {kept_by_extractive}/{len(facts)} 条值"
    )


# ══════════════════════════════════════════════════════════════════
# 2. 降级链与账本（沙箱 facts.db）
# ══════════════════════════════════════════════════════════════════

@pytest.fixture()
def facts_db(monkeypatch, tmp_path):
    """facts 库钉到用例专属临时文件（同 WP-A：不吃收集顺序的连坐）。

    refine 模块的 `_checked` 建表旗标是模块级全局 —— 换了库必须复位，
    否则新库上 refined_memories 压根没建，所有断言死在第一步。
    """
    import ducky.utils as utils

    data_dir = os.environ.get("AIDUMEM_DATA_DIR", "")
    assert "aidumei_test_data_" in data_dir, "测试没跑在沙箱 DATA_DIR 里，立刻停"

    db_path = str(tmp_path / "facts.db")
    monkeypatch.setattr(utils, "FACTS_DB", db_path)
    monkeypatch.setattr(refine_memory, "_checked", False)

    from ducky.schema_bootstrap import ensure_core_schema
    ensure_core_schema(force=True)

    def query(sql: str, params=()):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    return query


def _seed(user: str, n: int = 4, category: str = "wpb_cat"):
    """按 refine 的取数口径播种：具名租户按 source 收窄，所以 source=user。"""
    from ducky.federation.writer import write_fact

    for i in range(n):
        res = write_fact(
            category, f"{user}_键{i}", f"{user}_取值{i}·编号{i * 7 + 100}",
            source=user, user_id=user, dedup=False,
        )
        assert res.get("status") != "error", res


class TestBasisChain:
    def test_no_llm_lands_on_extractive(self, facts_db):
        _seed("wpb_ext")
        res = refine_group("wpb_ext", "wpb_cat", use_llm=False)
        assert res["status"] == "ok", res
        assert res["consolidation_basis"] == "extractive"
        assert res["llm_used"] is False
        # 摘要里必须有值，不能只有目录
        assert "wpb_ext_取值0·编号100" in res["summary"]
        row = facts_db(
            "SELECT consolidation_basis FROM refined_memories WHERE refine_id=?",
            (res["refine_id"],),
        )
        assert row and row[0]["consolidation_basis"] == "extractive", \
            "basis 没记进账本 —— 分桶审计无从谈起"

    def test_extractive_failure_falls_to_rule_with_warning(
            self, facts_db, monkeypatch, caplog):
        """中间档失败：必须出声（warning）、必须稳落 rule 档，链条不许断。"""
        _seed("wpb_rule")

        def _boom(items):
            raise RuntimeError("提取式档人为故障")

        monkeypatch.setattr(refine_memory, "_extractive_summary", _boom)
        with caplog.at_level(logging.WARNING, logger="aiduMEM.refine_memory"):
            res = refine_group("wpb_rule", "wpb_cat", use_llm=False)
        assert res["status"] == "ok", res
        assert res["consolidation_basis"] == "rule"
        assert "涉及：" in res["summary"], "rule 档形状变了"
        assert any("提取式整合失败" in r.message for r in caplog.records), \
            "真实降级没出声 —— 静默失败铁律"

    def test_llm_success_lands_on_llm_basis(self, facts_db, monkeypatch):
        _seed("wpb_llm")
        import ducky.llm_client as llm_client

        monkeypatch.setattr(refine_memory, "REFINE_ENABLED", True)
        monkeypatch.setattr(
            llm_client, "call_llm",
            lambda *a, **kw: '{"summary": "LLM 高层摘要", "reason": "test", "confidence": 0.9}',
        )
        res = refine_group("wpb_llm", "wpb_cat", use_llm=True)
        assert res["status"] == "ok", res
        assert res["consolidation_basis"] == "llm"
        assert res["llm_used"] is True
        assert res["summary"] == "LLM 高层摘要"

    def test_empty_valued_candidates_fall_to_rule(self, facts_db, monkeypatch):
        """提取式产不出要点（空串）时降 rule —— 空壳摘要不许入账。"""
        _seed("wpb_empty")
        monkeypatch.setattr(refine_memory, "_extractive_summary", lambda items: "")
        res = refine_group("wpb_empty", "wpb_cat", use_llm=False)
        assert res["status"] == "ok", res
        assert res["consolidation_basis"] == "rule"


class TestApplyRollbackCompat:
    def test_extractive_product_apply_then_rollback(self, facts_db):
        """三档同走一条账本通道：extractive 产物的归档与恢复必须可逆。"""
        _seed("wpb_cycle")
        res = refine_group("wpb_cycle", "wpb_cat", use_llm=False)
        assert res["status"] == "ok" and res["consolidation_basis"] == "extractive"
        rid, src_ids = res["refine_id"], set(res["source_ids"])

        applied = apply_refinement(rid)
        assert applied["status"] == "ok" and applied["archived"] == len(src_ids)
        archived_ids = {r["id"] for r in facts_db(
            "SELECT id FROM facts WHERE archived=1 AND source=?", ("wpb_cycle",))}
        assert archived_ids == src_ids, "归档的不是那批源事实（集合比对，不是计数比对）"
        refined_rows = facts_db(
            "SELECT fact_value FROM facts WHERE fact_key=?", (f"refined:{rid}",))
        assert refined_rows and "wpb_cycle_取值0·编号100" in refined_rows[0]["fact_value"]

        rolled = rollback_refinement(rid)
        assert rolled["status"] == "ok" and rolled["restored"] == len(src_ids)
        assert facts_db(
            "SELECT 1 FROM facts WHERE archived=1 AND source=?", ("wpb_cycle",)) == []
        assert facts_db(
            "SELECT 1 FROM facts WHERE fact_key=?", (f"refined:{rid}",)) == [], \
            "回滚后精炼摘要行还在 —— 幽灵记忆"


# ══════════════════════════════════════════════════════════════════
# 3. 水位阈值配置化（/health）
# ══════════════════════════════════════════════════════════════════

def _health_probes(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ducky.hot.health import register_health_routes

    app = FastAPI()
    register_health_routes(app)
    body = TestClient(app).get("/health", headers={"Authorization": "Bearer " + _set_health_token(monkeypatch)}).json()
    return body.get("probes", {}), body.get("warnings", [])


class TestWatermarkConfig:
    def test_default_effective_is_800(self, facts_db, monkeypatch):
        monkeypatch.delenv("AIDUMEI_FACTS_WATERMARK", raising=False)
        probes, _ = _health_probes(monkeypatch)
        assert probes.get("facts_watermark_effective") == 800, \
            "默认阈值漂了 —— 承诺过与 v20.0.1 行为逐字节一致"

    def test_env_override_takes_effect_and_alarm_fires(self, facts_db, monkeypatch):
        """生效值问 /health 不问文件：阈值 5、事实 6 条，警报必须响。"""
        monkeypatch.setenv("AIDUMEI_FACTS_WATERMARK", "5")
        _seed("wpb_wm", n=6, category="wpb_wm_cat")
        probes, warnings = _health_probes(monkeypatch)
        assert probes.get("facts_watermark_effective") == 5
        assert probes.get("watermark_warning") is True
        assert any("阈值 5" in w for w in warnings), f"警报文本没带生效阈值: {warnings}"

    def test_alarm_silent_below_threshold(self, facts_db, monkeypatch):
        """负向：不过阈值不许叫 —— 会叫的探针也要证明它会闭嘴。"""
        monkeypatch.setenv("AIDUMEI_FACTS_WATERMARK", "50")
        _seed("wpb_wm_quiet", n=3, category="wpb_wm_cat2")
        probes, _ = _health_probes(monkeypatch)
        assert probes.get("watermark_warning") is False

    def test_invalid_env_reports_error_and_falls_back(self, facts_db, monkeypatch):
        """显式值非法：/health 不许挂、不许哑 —— 报警进探针，回退默认。"""
        monkeypatch.setenv("AIDUMEI_FACTS_WATERMARK", "abc")
        probes, _ = _health_probes(monkeypatch)
        assert probes.get("facts_watermark_effective") == 800
        assert "AIDUMEI_FACTS_WATERMARK" in probes.get("facts_watermark_config_error", ""), \
            "非法配置的报警被吞了"


def _set_health_token(monkeypatch):
    token = "test-health-token"
    monkeypatch.setenv("AIDUMEM_API_TOKEN", token)
    return token