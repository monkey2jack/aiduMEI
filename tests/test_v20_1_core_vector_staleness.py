"""tests/test_v20_1_core_vector_staleness.py — v20.1 WP-D CoreMemory 还账

D1 向量召回（本文件测**写入契约**；语义近义召回的端到端验收在生产实机
阶段做 —— 那需要真嵌入服务，本机只能测「点位写没写对」，不冒充测了召回）：
  · put_block 后向量库收到点位：payload 按 mem0 装配契约铺
    （data 必填 / user_id·bank_id 盖戳 / reliability=1.0 / memory_class=core）；
  · 点位 id 确定性：同块同域改十次只占一个点；异域同名块各占各的点；
  · 开关三态：关=不写；非法值=记账可见；嵌入挂了=写入照常成功 + 账本留痕
    （第三副本失败不许拖垮正本）。

D2 陈旧告警分级（告警疲劳的修复本体）：
  · 分级默认：画像/决策 180（semantic 档）、当前项目 30（episodic 档，不放松）；
  · **区分力对照**：31 天的画像不再告警（疲劳源关闭）、31 天的项目照常告警
    （该叫的一声不少）—— 两条并立才证明这是分级，不是消音；
  · env 覆盖（每块 > 全局）+ 非法值出声降档；生效值经 staleness_status 与
    /health 可查（问解析者，不问文件）。

存量回填：dry-run 默认一个字节不写；--apply 仅在沙箱演练 ——
生产执行是数据变更停点，须维护者单独批准（v20.0.1 登记）。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ducky.core_memory as cm  # noqa: E402
from ducky.core_memory import (  # noqa: E402
    _CORE_VECTOR_ENV,
    _STALENESS_ENV_GLOBAL,
    _STALENESS_ENV_PREFIX,
    STALENESS_DAYS,
    backfill_core_vectors,
    core_vector_point_id,
    is_core_vector_index_enabled,
    staleness_threshold_days,
)


class _RecordingVectorStore:
    def __init__(self):
        self.inserts: list[dict] = []

    def insert(self, vectors, payloads=None, ids=None):
        self.inserts.append({"vectors": vectors, "payloads": payloads, "ids": ids})


class _FakeEmbedder:
    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls = 0

    def embed(self, text, action):
        self.calls += 1
        if self.fail:
            raise RuntimeError("embedding provider down (模拟)")
        return [0.1, 0.2, 0.3]


class _FakeMemory:
    def __init__(self, fail_embed: bool = False):
        self.embedding_model = _FakeEmbedder(fail=fail_embed)
        self.vector_store = _RecordingVectorStore()


@pytest.fixture()
def core_env(monkeypatch, tmp_path):
    """core_memory 钉到用例专属临时库 + mem0 换成录音替身。"""
    import ducky.mem0_runtime as runtime
    import ducky.utils as utils

    data_dir = os.environ.get("AIDUMEM_DATA_DIR", "")
    assert "aidumei_test_data_" in data_dir, "测试没跑在沙箱 DATA_DIR 里，立刻停"

    db_path = str(tmp_path / "facts.db")
    monkeypatch.setattr(utils, "FACTS_DB", db_path)
    monkeypatch.setattr(cm, "_initialized", False)
    cm._initialized_scopes.clear()

    fake = _FakeMemory()
    monkeypatch.setattr(runtime, "get_memory", lambda: fake)
    return fake


CONTENT = "生产助手当前在验收 v20.1 的确定性兜底与诚实召回四个工作包"


# ══════════════════════════════════════════════════════════════════
# D1：向量写入契约
# ══════════════════════════════════════════════════════════════════

class TestVectorWrite:
    def test_put_block_writes_vector_point_with_contract_payload(self, core_env):
        cm.init_core_memory()
        cm.put_block("core_current_project", CONTENT,
                     user_id="wpd_alice", bank_id="wpd_bank")
        assert core_env.vector_store.inserts, "put_block 没往向量库写点位"
        rec = core_env.vector_store.inserts[-1]
        payload = rec["payloads"][0]
        # mem0 装配契约：data 缺了整条结果被丢；user_id 提升参与过滤；
        # 其余键落 metadata 供域复筛 / 打分器 / 溯源。
        assert payload["data"] == CONTENT
        assert payload["user_id"] == "wpd_alice"
        assert payload["bank_id"] == "wpd_bank"
        assert payload["memory_class"] == "core"
        assert payload["reliability"] == 1.0
        assert payload["core_block_key"] == "core_current_project"
        assert rec["ids"] == [core_vector_point_id(
            "core_current_project", "wpd_alice", "wpd_bank")]

    def test_point_id_stable_across_rewrites_and_distinct_across_banks(self, core_env):
        """同块同域十次改写一个点位；异域同名块各占各的点。"""
        a1 = core_vector_point_id("core_user_profile", "u", "bank_a")
        a2 = core_vector_point_id("core_user_profile", "u", "bank_a")
        b = core_vector_point_id("core_user_profile", "u", "bank_b")
        assert a1 == a2, "同块同域点位 id 不稳定 —— 改十次堆十个旧点"
        assert a1 != b, "异域同名块共用点位 —— 一次更新会把别域的块盖掉"

        cm.init_core_memory()
        cm.put_block("core_user_profile", "第一版内容长度超过十个字符",
                     user_id="u", bank_id="bank_a")
        cm.put_block("core_user_profile", "第二版内容长度超过十个字符",
                     user_id="u", bank_id="bank_a")
        ids = [r["ids"][0] for r in core_env.vector_store.inserts]
        assert len(set(ids)) == 1, "两次改写产生了两个点位"

    def test_disabled_env_writes_nothing(self, core_env, monkeypatch):
        monkeypatch.setenv(_CORE_VECTOR_ENV, "0")
        cm.init_core_memory()
        cm.put_block("core_key_decisions", CONTENT, user_id="wpd_off")
        assert core_env.vector_store.inserts == [], "开关关着还在写向量"

    def test_invalid_env_value_raises_by_name(self, monkeypatch):
        monkeypatch.setenv(_CORE_VECTOR_ENV, "maybe")
        with pytest.raises(ValueError, match=_CORE_VECTOR_ENV):
            is_core_vector_index_enabled()

    def test_embed_failure_never_breaks_put_block_but_is_ledgered(
            self, core_env, monkeypatch):
        """第三副本失败不许拖垮正本 —— 但必须记账，谁失败谁留名。"""
        core_env.embedding_model.fail = True
        ledgered = []
        import ducky.failure_ledger as ledger
        monkeypatch.setattr(ledger, "feature_failed",
                            lambda feat, exc: ledgered.append(feat))
        cm.init_core_memory()
        result = cm.put_block("core_current_project", CONTENT, user_id="wpd_fail")
        assert result["status"] == "ok", "向量腿失败把正本写入拖垮了"
        assert "core_memory_vector_index" in ledgered, \
            "向量腿失败没记账 —— 绿灯亮着、活没干"


class TestBackfill:
    def test_dry_run_reports_without_writing(self, core_env):
        cm.init_core_memory()
        cm.put_block("core_current_project", CONTENT, user_id="wpd_bf")
        core_env.vector_store.inserts.clear()

        report = backfill_core_vectors(user_id="wpd_bf", bank_id="default",
                                       apply=False)
        assert report["apply"] is False
        assert any("core_current_project" in t for t in report["would_index"])
        assert core_env.vector_store.inserts == [], "dry-run 写了数据 —— 校验破坏基线"

    def test_apply_writes_and_skips_placeholders(self, core_env):
        """全作用域回填：真块入池、占位块跳过且**记入 skipped**（不沉默）。

        占位块住在 init 播种的 default 作用域，真块在具名租户 —— 全作用域
        一把扫下来，两种处置必须分开可见。
        """
        cm.init_core_memory()  # 在 default 作用域播种占位块
        cm.put_block("core_current_project", CONTENT, user_id="wpd_bf2")
        core_env.vector_store.inserts.clear()

        report = backfill_core_vectors(apply=True)
        assert any("wpd_bf2" in t and "core_current_project" in t
                   for t in report["indexed"])
        assert core_env.vector_store.inserts, "apply 没真写"
        # 占位文本不进向量池：搜到「（尚未填写）」毫无价值
        indexed_payloads = [r["payloads"][0]["data"]
                            for r in core_env.vector_store.inserts]
        assert all("尚未填写" not in d for d in indexed_payloads)
        assert report["skipped"], "占位块没有被记入 skipped —— 沉默跳过"


# ══════════════════════════════════════════════════════════════════
# D2：陈旧告警分级
# ══════════════════════════════════════════════════════════════════

def _age(days: float) -> str:
    return (datetime.now() - timedelta(days=days)).isoformat()


def _set_verified(db_path: str, block_key: str, ts: str):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE core_memory SET last_verified_at=?, updated_at=? WHERE block_key_raw=?",
        (ts, ts, block_key))
    n = conn.total_changes
    conn.commit()
    conn.close()
    assert n > 0, f"没有一行被改到（block_key={block_key}）"


class TestTieredThresholds:
    def test_default_tiers_follow_federation_ttl_semantics(self, monkeypatch):
        """分级默认值的依据：semantic 180 / episodic 30（联邦分层 TTL 表）。"""
        monkeypatch.delenv(_STALENESS_ENV_GLOBAL, raising=False)
        assert staleness_threshold_days("core_user_profile") == 180
        assert staleness_threshold_days("core_key_decisions") == 180
        assert staleness_threshold_days("core_current_project") == 30, \
            "最易过期的块被放松了 —— 分级是给依据，不是调大消音"

    def test_unknown_block_falls_back_to_global_constant(self):
        assert staleness_threshold_days("no_such_block") == STALENESS_DAYS

    def test_per_block_env_beats_global_env(self, monkeypatch):
        monkeypatch.setenv(_STALENESS_ENV_GLOBAL, "99")
        monkeypatch.setenv(_STALENESS_ENV_PREFIX + "CORE_USER_PROFILE", "7")
        assert staleness_threshold_days("core_user_profile") == 7
        assert staleness_threshold_days("core_key_decisions") == 99

    def test_invalid_env_warns_and_falls_through(self, monkeypatch, caplog):
        monkeypatch.setenv(_STALENESS_ENV_PREFIX + "CORE_USER_PROFILE", "半年")
        with caplog.at_level(logging.WARNING, logger="aiduMEM.CoreMemory"):
            assert staleness_threshold_days("core_user_profile") == 180
        assert any("无效" in r.message for r in caplog.records), \
            "显式配置无效没出声（铁律 13）"

    def test_alarm_fatigue_fixed_but_volatile_block_still_alarms(
            self, core_env, monkeypatch, tmp_path):
        """★ 告警疲劳修复的区分力对照（本 WP 的靶心）。

        同样 31 天没验证：
          · 画像块（semantic 档）→ 不再告警 —— 疲劳源关闭；
          · 项目块（episodic 档）→ 照常告警 —— 该叫的一声不少。
        只测前者 = 无法排除「告警整个被拆了」；两条并立才是分级。
        """
        import ducky.utils as utils
        db_path = utils.FACTS_DB  # core_env 已钉到临时库
        cm.init_core_memory()
        cm.put_block("core_user_profile", "稳定身份信息内容一二三四五")
        _set_verified(db_path, "core_user_profile", _age(31))
        st = cm.staleness_status()
        assert st["stale"] is False, \
            f"31 天的画像块仍在告警 —— 告警疲劳没修掉：{st}"

        cm.put_block("core_current_project", "当前项目状态内容一二三四五")
        _set_verified(db_path, "core_current_project", _age(31))
        st = cm.staleness_status()
        assert st["stale"] is True, \
            f"31 天的项目块不告警了 —— 这是消音，不是分级：{st}"
        assert st["stale_blocks"] == 1

    def test_effective_thresholds_reported_by_status_and_health(
            self, core_env, monkeypatch):
        """生效值问解析者：status 与 /health 报出的必须是函数算出来的那份。"""
        monkeypatch.setenv(_STALENESS_ENV_PREFIX + "CORE_KEY_DECISIONS", "45")
        cm.init_core_memory()
        st = cm.staleness_status()
        assert st["threshold_days_by_block"]["core_key_decisions"] == 45
        assert st["threshold_days_by_block"]["core_current_project"] == 30
        assert st["threshold_days"] == STALENESS_DAYS  # 旧字段语义不变

        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from ducky.hot.health import register_health_routes
        app = FastAPI()
        register_health_routes(app)
        probes = TestClient(app).get("/health").json()["probes"]
        assert probes.get("core_memory_thresholds", {}).get(
            "core_key_decisions") == 45, "/health 报的不是生效值"
        assert isinstance(probes.get("core_vector_index_enabled"), bool)
