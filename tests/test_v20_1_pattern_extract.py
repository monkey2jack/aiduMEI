"""tests/test_v20_1_pattern_extract.py — v20.1 WP-A 确定性抽取层

三层断言，对应预案验收门槛：

1. **纯函数层**：七类抽取逐条点名 + 噪音护栏 + 确定性（同输入两遍逐字节一致）
   + 边界（空串 / emoji / 超长）。
2. **落库层**（沙箱 facts.db）：source='pattern_extract' 标记、(user_id, bank_id)
   域戳、跨 bank 负向、回滚锚（按 source 精确清除不伤他人）、开关三态
   （关=不落、非法值=报错点名）、截断可观测。
3. **主链负向对照**（区分力）：mock LLM 空抽取（mem0 落库为零）走真 /add 路由，
   pattern facts 必须在；同场景关掉开关，facts 必须为空——
   **让错的那条路走通，证明对照真的分得出来**。

全部用例跑在 conftest.py 重定向的沙箱 DATA_DIR 里，绝不碰真实数据。
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ducky.pattern_extract import (  # noqa: E402
    ENV_FLAG,
    MAX_FACTS_PER_ADD,
    MAX_VALUE_LEN,
    PATTERN_EXTRACT_SOURCE,
    extract_and_store,
    extract_patterns,
    is_pattern_extract_enabled,
    reset_stats,
    stats,
)

#: 相对日期换算的锚点 —— 测试里写死，永不取 now()。
ANCHOR = "2026-08-25T08:00:00+00:00"

# Full /health diagnostics now require a valid credential.
def _health_token(monkeypatch):
    token = "test-health-token"
    monkeypatch.setenv("AIDUMEM_API_TOKEN", token)
    return token


def _kinds(items):
    return [it["kind"] for it in items]


def _by_kind(items, kind):
    return [it for it in items if it["kind"] == kind]


# ══════════════════════════════════════════════════════════════════
# 1. 纯函数层：七类逐条点名
# ══════════════════════════════════════════════════════════════════

class TestDatetime:
    def test_iso_date(self):
        items = _by_kind(extract_patterns("部署截止日是2026-09-01"), "datetime")
        assert any(it["fact_key"] == "2026-09-01" for it in items)

    def test_cn_date_normalized_to_iso(self):
        items = _by_kind(extract_patterns("2026年8月25日 收口完成"), "datetime")
        assert any(it["fact_key"] == "2026-08-25" for it in items)

    def test_cn_month_day_kept_verbatim(self):
        """无年份的日期不编造年份 —— 原样保留。"""
        items = _by_kind(extract_patterns("9月1日 上线"), "datetime")
        assert any(it["fact_key"] == "9月1日" for it in items)

    def test_relative_date_resolved_against_anchor(self):
        items = _by_kind(
            extract_patterns("明天要给评审人交报告", recorded_at=ANCHOR), "datetime"
        )
        assert any(it["fact_key"] == "2026-08-26" for it in items)

    def test_relative_date_without_anchor_is_silent(self):
        """锚点缺失就不产出 —— 绝不悄悄用当前时间补（确定性底线）。"""
        items = _by_kind(extract_patterns("明天要给评审人交报告"), "datetime")
        assert items == []


class TestVersion:
    def test_v_prefix(self):
        items = _by_kind(extract_patterns("已切换 v20.0.1"), "version")
        assert any(it["fact_key"] == "v20.0.1" for it in items)

    def test_two_dots_without_prefix(self):
        items = _by_kind(extract_patterns("基座是 2.0.19"), "version")
        assert any(it["fact_key"] == "2.0.19" for it in items)

    def test_context_word_accepts_single_dot(self):
        items = _by_kind(extract_patterns("版本升级到 20.1"), "version")
        assert any(it["fact_key"] == "20.1" for it in items)

    def test_plain_decimal_rejected(self):
        """裸小数不是版本号 —— 无 v 前缀、单点、无上下文词，一律不采信。"""
        assert _by_kind(extract_patterns("温度 3.5 左右"), "version") == []


class TestMetric:
    def test_number_with_unit(self):
        items = _by_kind(extract_patterns("单查耗时 120ms"), "metric")
        assert any(it["fact_key"] == "120ms" for it in items)

    def test_date_not_double_counted_as_metric(self):
        """「2026年」不是数量 —— 日期占住的区间 metric 不许重复抽。"""
        items = _by_kind(extract_patterns("2026年8月25日 收口"), "metric")
        assert items == []

    def test_bare_number_without_unit_rejected(self):
        assert _by_kind(extract_patterns("总共 12345 左右"), "metric") == []


class TestLink:
    def test_url(self):
        items = _by_kind(
            extract_patterns("文档在 https://example.com/wiki/abc 上"), "link"
        )
        assert any(it["fact_key"] == "https://example.com/wiki/abc" for it in items)

    def test_unix_path(self):
        items = _by_kind(extract_patterns("配置在 /etc/aidumem/config.json 里"), "link")
        assert any(it["fact_key"] == "/etc/aidumem/config.json" for it in items)

    def test_path_inside_url_not_duplicated(self):
        items = _by_kind(
            extract_patterns("看 https://example.com/a/b/c 就好"), "link"
        )
        assert len(items) == 1


class TestKV:
    def test_eq_form(self):
        items = _by_kind(extract_patterns("重试上限=3次"), "kv")
        assert any(it["fact_key"] == "重试上限" and it["fact_value"] == "3次"
                   for it in items)

    def test_colon_line_form(self):
        items = _by_kind(extract_patterns("默认分支：main"), "kv")
        assert any(it["fact_key"] == "默认分支" and it["fact_value"] == "main"
                   for it in items)

    def test_shi_form(self):
        items = _by_kind(extract_patterns("系统中文名是智慧引擎"), "kv")
        assert any(it["fact_key"] == "系统中文名" and it["fact_value"] == "智慧引擎"
                   for it in items)

    def test_connective_suffix_guard(self):
        """「还是 / 但是」里的「是」不是系动词 —— 键后缀护栏必须拦下。"""
        for text in ("这个方案还是不错的", "天气不错但是有点热"):
            assert _by_kind(extract_patterns(text), "kv") == [], text

    def test_pronoun_key_guard(self):
        assert all(it["fact_key"] != "这个"
                   for it in _by_kind(extract_patterns("这个是临时的"), "kv"))


class TestInstructionPreference:
    def test_instruction_sentence(self):
        items = _by_kind(extract_patterns("发布前必须跑脱敏扫描"), "instruction")
        assert len(items) == 1
        assert items[0]["fact_value"] == "发布前必须跑脱敏扫描"

    def test_preference_sentence(self):
        items = _by_kind(extract_patterns("架构师喜欢结构化表格"), "preference")
        assert len(items) == 1
        assert "喜欢结构化表格" in items[0]["fact_value"]

    def test_plain_sentence_yields_neither(self):
        items = extract_patterns("今晚给代码做一次全面体检")
        assert _by_kind(items, "instruction") == []
        assert _by_kind(items, "preference") == []


class TestPurityAndBounds:
    MIXED = (
        "2026-09-01 前必须完成 v20.1 的部署；"
        "超时=30秒，文档在 https://example.com/doc 。"
        "审读人喜欢简洁的接口，明天先出草稿"
    )

    def test_determinism_byte_identical(self):
        """同一输入两遍逐字节一致 —— 预案验收门槛原文。"""
        a = extract_patterns(self.MIXED, recorded_at=ANCHOR)
        b = extract_patterns(self.MIXED, recorded_at=ANCHOR)
        assert a == b
        assert repr(a) == repr(b)

    def test_mixed_text_covers_multiple_kinds(self):
        kinds = set(_kinds(extract_patterns(self.MIXED, recorded_at=ANCHOR)))
        assert {"datetime", "version", "kv", "link", "preference",
                "instruction"} <= kinds

    def test_empty_input(self):
        assert extract_patterns("") == []
        assert extract_patterns(None) == []  # type: ignore[arg-type]

    def test_emoji_only_input(self):
        assert extract_patterns("🐒🔥🎉") == []

    def test_huge_input_bounded_and_alive(self):
        text = "。".join(f"第{i}条：值{i}" for i in range(2000))
        items = extract_patterns(text)
        assert items, "超长文本不该抽成空"
        assert all(len(it["fact_value"]) <= MAX_VALUE_LEN for it in items)

    def test_intra_call_dedup(self):
        items = extract_patterns("端口=8080。端口=8080")
        assert len(_by_kind(items, "kv")) == 1


# ══════════════════════════════════════════════════════════════════
# 2. 落库层（沙箱 facts.db）
# ══════════════════════════════════════════════════════════════════

@pytest.fixture()
def facts_db(monkeypatch, tmp_path):
    """把 facts 库钉到本用例专属的临时文件，返回查询用连接工厂。

    为什么不直接沿用 conftest 重定向后的 FACTS_DB：套件里存在**模块级**改写
    `utils.FACTS_DB` 的老测试（如 test_federation.py，import 那一刻就永久生效），
    全量跑时谁先被收集谁说了算 —— 靠共享全局的路径断言会被无辜连坐。
    这里用 monkeypatch 显式钉住（用例结束自动还原），彻底不吃收集顺序。

    沙箱两道护栏照旧：env 必须指着 conftest 的隔离目录（验证重定向真发生了），
    库文件必须在 pytest 的 tmp_path 里。
    """
    import ducky.utils as utils

    data_dir = os.environ.get("AIDUMEM_DATA_DIR", "")
    assert "aidumei_test_data_" in data_dir, "测试没跑在沙箱 DATA_DIR 里，立刻停"

    db_path = str(tmp_path / "facts.db")
    monkeypatch.setattr(utils, "FACTS_DB", db_path)

    from ducky.schema_bootstrap import ensure_core_schema
    ensure_core_schema(force=True)

    def query(sql: str, params=()):
        conn = sqlite3.connect(db_path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    return query


class TestStore:
    def test_writes_source_and_scope_stamp(self, facts_db):
        res = extract_and_store("巡检端口=18100", user_id="wpa_alice",
                                bank_id="wpa_bank")
        assert res["status"] == "ok" and res["stored"] >= 1
        rows = facts_db(
            "SELECT category, fact_key, fact_value, source, user_id, bank_id "
            "FROM facts WHERE source=? AND user_id=?",
            (PATTERN_EXTRACT_SOURCE, "wpa_alice"),
        )
        assert rows, "pattern fact 没落库"
        for cat, key, _val, source, uid, bank in rows:
            assert cat.startswith("pattern_")
            assert source == PATTERN_EXTRACT_SOURCE
            assert (uid, bank) == ("wpa_alice", "wpa_bank")

    def test_bank_isolation_negative(self, facts_db):
        extract_and_store("隔离键=隔离值", user_id="wpa_iso", bank_id="bank_a")
        leaked = facts_db(
            "SELECT 1 FROM facts WHERE source=? AND user_id=? AND bank_id=?",
            (PATTERN_EXTRACT_SOURCE, "wpa_iso", "bank_b"),
        )
        assert leaked == [], "甲 bank 的 pattern fact 泄进了乙 bank"

    def test_rollback_anchor_deletes_only_pattern_rows(self, facts_db):
        """回滚锚：按 source 清除只删自己的，别人的一行不许动。"""
        from ducky.federation.writer import write_fact

        extract_and_store("回滚演练=甲", user_id="wpa_rb", bank_id="rb")
        write_fact("general", "回滚演练_他人", "乙",
                   source="llm_probe", user_id="wpa_rb", bank_id="rb")

        import ducky.utils as utils
        conn = sqlite3.connect(utils.FACTS_DB)
        try:
            conn.execute("DELETE FROM facts WHERE source=? AND user_id=? AND bank_id=?",
                         (PATTERN_EXTRACT_SOURCE, "wpa_rb", "rb"))
            conn.commit()
        finally:
            conn.close()

        assert facts_db("SELECT 1 FROM facts WHERE source=? AND user_id=?",
                        (PATTERN_EXTRACT_SOURCE, "wpa_rb")) == []
        assert facts_db("SELECT 1 FROM facts WHERE source='llm_probe' AND user_id=?",
                        ("wpa_rb",)), "回滚把别人的行也删了 —— 这不是回滚是事故"

    def test_disabled_env_stores_nothing(self, facts_db, monkeypatch):
        monkeypatch.setenv(ENV_FLAG, "0")
        res = extract_and_store("关闸键=关闸值", user_id="wpa_off", bank_id="off")
        assert res["status"] == "disabled"
        assert facts_db("SELECT 1 FROM facts WHERE user_id=?", ("wpa_off",)) == []

    def test_invalid_env_value_raises_by_name(self, monkeypatch):
        """显式配置无效必须报错点名 —— 不许静默回退成默认值。"""
        monkeypatch.setenv(ENV_FLAG, "maybe")
        with pytest.raises(ValueError, match=ENV_FLAG):
            is_pattern_extract_enabled()

    def test_truncation_is_counted_and_logged(self, facts_db, caplog):
        reset_stats()
        text = "。".join(f"截断键{i}=截断值{i}" for i in range(MAX_FACTS_PER_ADD + 15))
        import logging
        with caplog.at_level(logging.WARNING, logger="aiduMEM.PatternExtract"):
            res = extract_and_store(text, user_id="wpa_trunc", bank_id="tr")
        assert res["extracted"] == MAX_FACTS_PER_ADD
        assert stats()["truncated"] >= 1
        assert any("截断" in r.message for r in caplog.records), \
            "沉默截断 —— 覆盖了一部分被伪装成全覆盖"


# ══════════════════════════════════════════════════════════════════
# 3. 主链负向对照：LLM 空抽取时，pattern 通路是唯一的事实来源
# ══════════════════════════════════════════════════════════════════

class _EmptyExtractionMemory:
    """模拟 LLM 空抽取：mem0.add 返回空 results —— 预算悬崖的真实形态。"""

    def add(self, messages, user_id=None, metadata=None, **kw):
        return {"results": []}

    def search(self, *a, **kw):
        return {"results": []}


@pytest.fixture()
def add_route_client(monkeypatch, facts_db):
    """最小 /add 路由：mem0 被换成空抽取替身，速度层外设全部旁路。

    旁路的都是与本对照无关的外设（coalesce 后台线程、LLM 提速补丁）；
    被测主链 —— 路由解析、注入防御、pattern_extract 挂载 —— 全是真的。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import ducky.add_speed as add_speed
    import ducky.hot.add as hot_add

    monkeypatch.setattr(hot_add, "get_memory", lambda: _EmptyExtractionMemory())
    monkeypatch.setattr(
        hot_add, "lazy_import_layer1",
        lambda: (lambda mem, msgs, uid, meta, **kw: mem.add(
            msgs, user_id=uid, metadata=meta)),
    )
    monkeypatch.setattr(add_speed, "patch_llm_for_speed", lambda mem: None)
    monkeypatch.setattr(add_speed, "load_speed_cfg", lambda: {})
    monkeypatch.setattr(add_speed, "register_coalesce_flusher", lambda cb: None)
    monkeypatch.setattr(add_speed, "ensure_coalesce_worker", lambda: None)

    app = FastAPI()
    hot_add.register_add_routes(app)
    return TestClient(app)


TEXT_WITH_HARD_FACTS = "2026-09-01 前必须完成部署，负责人偏好用飞书沟通"


def test_llm_empty_extraction_pattern_facts_survive(add_route_client, facts_db):
    """正腿：LLM 哑火（mem0 落库为零），硬事实仍经 pattern 通路存活。"""
    resp = add_route_client.post("/add", json={
        "messages": TEXT_WITH_HARD_FACTS,
        "user_id": "wpa_blind", "bank_id": "route_on",
        "metadata": {"force_sync": True},
    })
    assert resp.status_code == 200, resp.text
    rows = facts_db(
        "SELECT category FROM facts WHERE source=? AND user_id=? AND bank_id=?",
        (PATTERN_EXTRACT_SOURCE, "wpa_blind", "route_on"),
    )
    cats = {r[0] for r in rows}
    assert "pattern_datetime" in cats, "日期丢了 —— pattern 通路没接上主链"
    assert "pattern_instruction" in cats, "指令句丢了"
    assert "pattern_preference" in cats, "偏好句丢了"


def test_llm_empty_extraction_without_pattern_layer_loses_everything(
        add_route_client, facts_db, monkeypatch):
    """对照腿（区分力）：关掉 pattern 层，同样的 LLM 哑火下信息**确实全丢**。

    这条腿走通，上一条的绿才有含金量 —— 否则无法排除
    「facts 是别的通路写进去的」。
    """
    monkeypatch.setenv(ENV_FLAG, "0")
    resp = add_route_client.post("/add", json={
        "messages": TEXT_WITH_HARD_FACTS,
        "user_id": "wpa_blind_off", "bank_id": "route_off",
        "metadata": {"force_sync": True},
    })
    assert resp.status_code == 200, resp.text
    rows = facts_db("SELECT 1 FROM facts WHERE user_id=?", ("wpa_blind_off",))
    assert rows == [], (
        "pattern 层关着 facts 里却有行 —— 对照失去区分力，正腿的绿不算数"
    )


# ══════════════════════════════════════════════════════════════════
# 4. /health 观测面
# ══════════════════════════════════════════════════════════════════

def test_health_exposes_pattern_extract_probe(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ducky.hot.health import register_health_routes

    app = FastAPI()
    register_health_routes(app)
    body = TestClient(app).get("/health", headers={"Authorization": "Bearer " + _set_health_token(monkeypatch)}).json()

    probe = body.get("probes", {}).get("pattern_extract")
    assert probe is not None, "/health 缺 pattern_extract 探针"
    assert isinstance(probe.get("enabled"), bool)
    for key in ("attempted", "extracted", "stored", "store_failed",
                "truncated", "disabled_skips"):
        assert key in probe, f"/health pattern_extract 缺计数 {key}"


def test_health_surfaces_invalid_flag_error(monkeypatch):
    """开关值非法时，/health 探针必须把报警原文亮出来，不许压成安静的 False。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ducky.hot.health import register_health_routes

    monkeypatch.setenv(ENV_FLAG, "maybe")
    app = FastAPI()
    register_health_routes(app)
    probe = TestClient(app).get("/health", headers={"Authorization": "Bearer " + _set_health_token(monkeypatch)}).json()["probes"]["pattern_extract"]
    assert probe["enabled"] is False
    assert ENV_FLAG in probe.get("error", ""), "非法开关值的报警被吞了"


def _set_health_token(monkeypatch):
    token = "test-health-token"
    monkeypatch.setenv("AIDUMEM_API_TOKEN", token)
    return token