"""tests/test_v20_1_audit_remediation.py — v20.1 整改轮 R-02 ~ R-17 点名验收

对应《外审结论与收口计划（Rev.2）》第三节清单。每条整改都带能红的断言；
其中 R-02 的优先级用例就是外审 z 变异轮证明的那块护卫真空的补墙 ——
反置硬实体优先级，这里必须有用例变红。
"""
from __future__ import annotations

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ducky.refine_memory as refine_memory  # noqa: E402
from ducky.pattern_extract import extract_patterns  # noqa: E402
from ducky.refine_memory import _EXTRACTIVE_MAX_POINTS, _extractive_summary  # noqa: E402


def _mk(items, category="rem_cat"):
    return [{"id": i + 1, "category": category, "fact_key": k, "fact_value": v}
            for i, (k, v) in enumerate(items)]


# ══════════════════════════════════════════════════════════════════
# R-02 · WP-B 硬切显式化 + 优先级钉死（外审 z P1-03）
# ══════════════════════════════════════════════════════════════════

class TestR02ExtractiveTruncation:
    def test_priority_survives_count_truncation(self):
        """反置优先级变异的必红用例：14 条要点（10 硬实体 + 4 纯文），
        上限 12 —— 被丢的必须是纯文要点，硬实体一条不许少。"""
        soft_names = ["甲", "乙", "丙", "丁"]
        hard_names = ["子", "丑", "寅", "卯", "辰", "巳", "午", "未", "申", "酉"]
        hard = [(f"硬键{n}", f"版本 2.{i}.0 端口 {8000 + i}")
                for i, n in enumerate(hard_names)]
        # 软要点：键与值都不含数字（键里一个数字就会被判成硬实体），
        # 且值互不相同（同值会被包含去重吸收，截断就根本不会发生）。
        soft = [(f"软键{n}", f"纯文字描述{n}无任何数字实体不重复") for n in soft_names]
        summary = _extractive_summary(_mk(soft[:2] + hard + soft[2:]))
        for _, v in hard:
            assert v in summary, f"硬实体要点 {v!r} 被截断挤掉 —— 优先级承诺失守"
        assert "另 2 条要点略" in summary

    def test_length_fallback_never_slices_mid_point(self):
        """480 长度回退按**完整要点**丢弃：正文里绝不出现被拦腰斩断的要点。
        整改前的硬切会留半截尾巴且不计数（z 的静默丢失现场）。"""
        pts = [(f"长键{i}", f"编号{i}·" + "内容甲乙丙丁" * 14) for i in range(9)]  # 单条≈60+
        summary = _extractive_summary(_mk(pts))
        assert "另" in summary and "条要点略" in summary, "长度回退没有显式标注"
        for k, v in pts:
            piece = f"{k}：{v[:80]}"
            in_full = piece in summary
            head_leaked = (not in_full) and (f"{k}：" in summary)
            assert not head_leaked, f"要点 {k} 被拦腰斩断 —— 硬切复活了"

    def test_length_fallback_drops_are_logged(self, caplog):
        pts = [(f"日志键{i}", f"编号{i}·" + "正文戊己庚辛" * 14) for i in range(9)]
        with caplog.at_level(logging.INFO, logger="aiduMEM.refine_memory"):
            _extractive_summary(_mk(pts))
        assert any("长度回退丢弃" in r.message for r in caplog.records), \
            "被丢要点没留名 —— 谁被丢必须可查"


# ══════════════════════════════════════════════════════════════════
# R-03 · LLM 降级日志对称化（外审 z P1-04）
# ══════════════════════════════════════════════════════════════════

def test_r03_llm_failure_logs_warning_not_debug(monkeypatch, caplog, tmp_path):
    import ducky.llm_client as llm_client
    import ducky.utils as utils
    from ducky.schema_bootstrap import ensure_core_schema

    monkeypatch.setattr(utils, "FACTS_DB", str(tmp_path / "facts.db"))
    monkeypatch.setattr(refine_memory, "_checked", False)
    ensure_core_schema(force=True)
    refine_memory.ensure_refine_schema()

    from ducky.federation.writer import write_fact
    for i in range(3):
        write_fact("r03_cat", f"r03_键{i}", f"取值{i}·编号{i}",
                   source="r03_user", user_id="r03_user", dedup=False)

    monkeypatch.setattr(refine_memory, "REFINE_ENABLED", True)
    monkeypatch.setattr(llm_client, "call_llm",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("LLM 宕机模拟")))
    with caplog.at_level(logging.WARNING, logger="aiduMEM.refine_memory"):
        res = refine_memory.refine_group("r03_user", "r03_cat", use_llm=True)
    assert res["consolidation_basis"] == "extractive"
    assert any("LLM 精炼失败" in r.message and r.levelno >= logging.WARNING
               for r in caplog.records), \
        "LLM 档降级没有 WARNING 出声 —— 与 extractive 档不对称（铁律 8）"


# ══════════════════════════════════════════════════════════════════
# R-04 · WP-A 护栏加固：z 的三个对抗样本必须 0 误抽
# ══════════════════════════════════════════════════════════════════

class TestR04AdversarialGuards:
    def test_connective_word_inside_key_is_rejected(self):
        """「X但是Y是Z」形：整词护栏拦住键内任意位置的连词（z 实例①）。"""
        items = extract_patterns("但是这个是旧版, v2 太老")
        assert not [it for it in items if it["kind"] == "kv"], \
            "「但是这个→旧版」kv 误抽复活"

    def test_compound_unit_not_orphaned(self):
        """「3.5 个小时」抽整单位，不许截成「3.5个」（z 实例②）。"""
        items = [it for it in extract_patterns("今天聊了 3.5 个小时",
                                               recorded_at="2026-08-25T08:00:00+00:00")
                 if it["kind"] == "metric"]
        assert items and items[0]["fact_key"] == "3.5个小时", items

    def test_url_span_shields_colon_kv(self):
        """行首 URL 的冒号不再被当键值定义（z 实例③）；link 照常抽。"""
        items = extract_patterns("https://example.com/a,b 这个网址")
        kinds = {it["kind"] for it in items}
        assert "link" in kinds
        assert not [it for it in items if it["kind"] == "kv"], \
            "kv(https → //…) 噪音复活"

    def test_legit_extractions_not_regressed(self):
        """护栏收紧不许伤及正抽：正常键值/指令仍然在。"""
        items = extract_patterns("默认分支是 main。发布前必须跑脱敏扫描")
        kinds = {it["kind"] for it in items}
        assert "kv" in kinds and "instruction" in kinds


# ══════════════════════════════════════════════════════════════════
# R-05 · 抽取截断按重要性（外审 x REC-01 方向）
# ══════════════════════════════════════════════════════════════════

def test_r05_instruction_survives_truncation(monkeypatch, tmp_path):
    """25 条键值淹没 1 条压轴指令：截断后指令必须存活 —— 整改前按句序
    切片，排在最后的指令必死。"""
    import ducky.utils as utils
    from ducky.schema_bootstrap import ensure_core_schema
    from ducky.pattern_extract import extract_and_store, reset_stats

    monkeypatch.setattr(utils, "FACTS_DB", str(tmp_path / "facts.db"))
    ensure_core_schema(force=True)
    reset_stats()

    text = "。".join(f"淹没键{i}=淹没值{i}" for i in range(25)) + "。收尾前必须复查健康探针"
    res = extract_and_store(text, user_id="r05_user", bank_id="default")
    assert res["extracted"] == 20  # 截断确实发生了（前置）

    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "facts.db"))
    try:
        rows = conn.execute(
            "SELECT category FROM facts WHERE user_id='r05_user' "
            "AND category='pattern_instruction'").fetchall()
    finally:
        conn.close()
    assert rows, "压轴指令被 25 条键值挤出局 —— 截断没按重要性来"


# ══════════════════════════════════════════════════════════════════
# R-06 · workspace 命中分支三态字段集补齐（外审 z P2-04）
# ══════════════════════════════════════════════════════════════════

def test_r06_workspace_hit_carries_full_verdict_fieldset(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import ducky.hot.search as hot_search
    import ducky.memory_workspace as ws

    monkeypatch.setattr(
        ws, "ws_lookup",
        lambda *a, **kw: [{"id": "w1", "memory": "热缓存命中", "score": 0.9}])
    monkeypatch.setattr(ws, "ws_feed_from_results", lambda *a, **kw: None)
    monkeypatch.setattr(hot_search, "boost_salience_for_results", lambda *a, **kw: None)
    monkeypatch.setattr(hot_search, "get_memory", lambda: None)

    app = FastAPI()
    hot_search.register_search_routes(app)
    body = TestClient(app).post(
        "/search", json={"query": "q", "user_id": "r06"}).json()
    assert body["recall_verdict"] == "found"
    assert body["verdict_basis"] == "workspace_hit"
    assert body.get("recall_confidence") == pytest.approx(0.9)
    assert "_recall_strength" in body, "workspace 分支缺 _recall_strength"
    assert body.get("_recall_legs") == {"workspace": "hit"}


# ══════════════════════════════════════════════════════════════════
# R-07 / R-13 · /health：watermark 异常显式 unknown；阈值 0 校准提示
# ══════════════════════════════════════════════════════════════════

def _health(monkeypatch=None):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ducky.hot.health import register_health_routes
    app = FastAPI()
    register_health_routes(app)
    return TestClient(app).get("/health").json()


def test_r07_watermark_probe_survives_facts_failure(monkeypatch, tmp_path):
    import ducky.utils as utils
    # 用路径致炸而不是换掉函数对象：get_facts_conn 是各模块 from-import 共享的
    # 函数，补丁窗口内谁首次导入谁就永久绑住假函数（R-16 曾因 /health 惰性
    # 导入 core_memory 被连坐整批红）。路径指向不存在目录下的库文件，
    # connect 必抛 OperationalError，且不产生任何可被绑走的函数身份。
    monkeypatch.setattr(utils, "FACTS_DB", str(tmp_path / "no_such_dir" / "facts.db"))
    probes = _health()["probes"]
    assert probes.get("facts_watermark_effective") == "unknown", \
        "facts 失败时水位探针整体缺席 —— 读取方 KeyError（z P2-03）"
    assert probes.get("watermark_warning") is None


def test_r13_zero_threshold_gets_calibration_hint(monkeypatch):
    monkeypatch.delenv("AIDUMEI_RECALL_VERDICT_THRESHOLD", raising=False)
    probes = _health()["probes"]
    assert "校准" in probes.get("recall_verdict_threshold_hint", ""), \
        "阈值 0.0 没有校准提示（y 变体 + w P1-④）"

    monkeypatch.setenv("AIDUMEI_RECALL_VERDICT_THRESHOLD", "0.35")
    probes = _health()["probes"]
    assert "recall_verdict_threshold_hint" not in probes, \
        "已配置阈值仍出提示 —— 提示变常驻噪音"


# ══════════════════════════════════════════════════════════════════
# R-12 · resource_probe 跨平台（社区审计）
# ══════════════════════════════════════════════════════════════════

class TestR12ResourceProbe:
    def test_snapshot_survives_missing_resource_module(self, monkeypatch):
        """模拟 Windows：resource=None 时 snapshot 不崩、字段诚实 None。"""
        import ducky.resource_probe as rp
        monkeypatch.setattr(rp, "resource", None)
        snap = rp.snapshot()
        assert snap["cpu_seconds"] is None
        assert snap["max_rss_mb"] is None
        assert isinstance(snap["threads"], int)

    def test_module_import_is_guarded_not_bare(self):
        """守住修法本身：顶层裸 import resource 一回来，Windows 收集即崩。"""
        import pathlib
        src = pathlib.Path("ducky/resource_probe.py").read_text(encoding="utf-8")
        import re
        assert not re.search(r"^import resource$", src, re.M), \
            "resource 又变回顶层裸 import —— Windows 测试收集会整体崩掉"
        assert "except ImportError" in src


# ══════════════════════════════════════════════════════════════════
# R-15 · 阈值非法日志点名到块（外审 y P2）
# ══════════════════════════════════════════════════════════════════

def test_r15_invalid_staleness_env_names_the_block(monkeypatch, caplog):
    from ducky.core_memory import staleness_threshold_days
    monkeypatch.setenv("AIDUMEI_CORE_STALENESS_DAYS_CORE_USER_PROFILE", "半年")
    with caplog.at_level(logging.WARNING, logger="aiduMEM.CoreMemory"):
        assert staleness_threshold_days("core_user_profile") == 180
    assert any("core_user_profile" in r.message for r in caplog.records), \
        "非法配置日志没点名块 —— 排错找不到错配源头"


# ══════════════════════════════════════════════════════════════════
# R-16 · 三副本对账巡检（外审 w P1-② 机制 + y 覆盖度）
# ══════════════════════════════════════════════════════════════════

class _RecordingVectorStore:
    def __init__(self):
        self.points = {}

    def insert(self, vectors, payloads=None, ids=None):
        for i, pid in enumerate(ids or []):
            self.points[pid] = (payloads or [{}])[i]

    def get(self, vector_id):
        return self.points.get(vector_id)


class _FakeMemory:
    def __init__(self):
        self.embedding_model = type(
            "E", (), {"embed": staticmethod(lambda t, a: [0.1])})()
        self.vector_store = _RecordingVectorStore()


@pytest.fixture()
def replica_env(monkeypatch, tmp_path):
    import ducky.core_memory as cm
    import ducky.mem0_runtime as runtime
    import ducky.utils as utils

    monkeypatch.setattr(utils, "FACTS_DB", str(tmp_path / "facts.db"))
    monkeypatch.setattr(utils, "TEXT_FTS_DB", str(tmp_path / "text_fts.db"))
    monkeypatch.setattr(cm, "_initialized", False)
    cm._initialized_scopes.clear()
    fake = _FakeMemory()
    monkeypatch.setattr(runtime, "get_memory", lambda: fake)
    # 沙箱里 FTS 库的 memories 表没人建过 —— put_block 的 FTS 腿会静默
    # 失败（账本可见），对账的 healthy 用例需要三腿都真的在。
    from ducky.text_fts import _init_text_fts
    _init_text_fts()
    return cm, fake


class TestR16ReplicaAudit:
    def test_healthy_block_reports_no_gap(self, replica_env):
        cm, fake = replica_env
        cm.put_block("core_current_project", "对账演练内容一二三四五",
                     user_id="r16_user")
        rep = cm.audit_core_replicas(user_id="r16_user")
        assert rep["checked"] == 1
        assert rep["gaps"] == [], f"三副本齐全却报缺腿：{rep}"

    def test_missing_vector_leg_is_reported(self, replica_env):
        cm, fake = replica_env
        cm.put_block("core_current_project", "对账演练内容一二三四五",
                     user_id="r16_gap")
        fake.vector_store.points.clear()  # 模拟向量腿静默失败后的状态
        rep = cm.audit_core_replicas(user_id="r16_gap")
        assert rep["gaps"] and rep["gaps"][0]["vector"] is False, \
            "向量腿缺失没被对账看见 —— 缺腿继续静默（w P1-② 机制）"

    def test_placeholder_blocks_are_excluded(self, replica_env):
        """占位块不进对账 —— 把设计行为算成缺腿会制造新的告警疲劳
        （外审 w 的例证恰好栽在这里，巡检必须免疫同款误判）。"""
        cm, fake = replica_env
        cm.init_core_memory(user_id="r16_ph")  # 只播种占位内容
        rep = cm.audit_core_replicas(user_id="r16_ph")
        assert rep["checked"] == 0 and rep["gaps"] == []


# ══════════════════════════════════════════════════════════════════
# R-17 · 向量腿测试隔离守卫（外审 w P1-③）
# ══════════════════════════════════════════════════════════════════

class TestR17VectorEscapeGuard:
    def test_escaping_local_vector_path_is_rejected_in_sandbox(self):
        from ducky.mem0_runtime import _assert_vector_store_inside_sandbox
        cfg = {"vector_store": {"config": {"path": "/var/lib/prod_qdrant"}}}
        with pytest.raises(RuntimeError, match="沙箱外"):
            _assert_vector_store_inside_sandbox(cfg)

    def test_inside_sandbox_path_passes(self):
        from ducky.mem0_runtime import _assert_vector_store_inside_sandbox
        data_dir = os.environ["AIDUMEM_DATA_DIR"]
        cfg = {"vector_store": {"config": {"path": os.path.join(data_dir, "qdrant")}}}
        _assert_vector_store_inside_sandbox(cfg)  # 不抛即过

    def test_remote_vector_store_without_path_is_out_of_scope(self):
        from ducky.mem0_runtime import _assert_vector_store_inside_sandbox
        _assert_vector_store_inside_sandbox(
            {"vector_store": {"config": {"host": "example.internal"}}})


# ══════════════════════════════════════════════════════════════════
# R-11 · 核心记忆 写入→召回 契约（外审 z P2-07 + 自报）
# ══════════════════════════════════════════════════════════════════
# WP-D 构建轮只测了「点位写没写对」，语义召回全靠生产实机烟测 —— 套件里
# 写入与检索之间的契约（payload 键名、过滤器传参、结果装配）没有任何一条
# 用例钉住。这里只假嵌入器与向量库存储，端点、引擎、打分、复筛全走真路：
# 嵌入器是确定性字符二元组哈希（同短语→高余弦），向量库替身做真余弦排序
# 并按 Qdrant must 语义等值过滤 —— hot/search 若少传/错传 filters，跨租户
# 负向立刻红。

class _BigramEmbedder:
    """确定性伪嵌入：字符二元组哈希入桶 + L2 归一。共享短语→高余弦。"""
    DIMS = 64

    @classmethod
    def embed(cls, text, action=None):
        import hashlib
        import math
        v = [0.0] * cls.DIMS
        t = "".join(str(text).split())
        for i in range(max(len(t) - 1, 0)):
            h = int(hashlib.md5(t[i:i + 2].encode("utf-8")).hexdigest(), 16)
            v[h % cls.DIMS] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]


class _CosineContractMemory:
    """mem0 替身：insert 存点，search 真嵌入查询 + 真余弦 + must 等值过滤。"""

    def __init__(self):
        self.embedding_model = _BigramEmbedder()
        self.vector_store = self
        self.points = {}

    # —— vector_store 面（put_block 的写入腿用） ——
    def insert(self, vectors, payloads=None, ids=None):
        for i, pid in enumerate(ids or []):
            self.points[pid] = (vectors[i], (payloads or [{}])[i])

    def get(self, vector_id):
        got = self.points.get(vector_id)
        return got and got[1]

    # —— Memory 面（引擎向量腿用） ——
    def search(self, query, filters=None, limit=None, top_k=None, **kw):
        qv = self.embedding_model.embed(query, "search")
        out = []
        for pid, (vec, payload) in self.points.items():
            if any(payload.get(k) != v for k, v in (filters or {}).items()):
                continue  # Qdrant must 语义：过滤键必须逐一等值命中
            score = sum(a * b for a, b in zip(qv, vec))
            out.append({
                "id": pid,
                "memory": payload.get("data"),
                "score": round(score, 6),
                "created_at": payload.get("created_at"),
                "user_id": payload.get("user_id"),
                "metadata": {k: v for k, v in payload.items()
                             if k not in ("data", "user_id")},
            })
        out.sort(key=lambda it: it["score"], reverse=True)
        return {"results": out[: (limit or top_k or 10)]}

    def get_all(self, *a, **kw):
        return {"results": []}


@pytest.fixture()
def recall_contract_env(monkeypatch, tmp_path):
    import ducky.core_memory as cm
    import ducky.hot.search as hot_search
    import ducky.mem0_runtime as runtime
    import ducky.utils as utils
    from ducky.schema_bootstrap import ensure_core_schema
    from ducky.text_fts import _init_text_fts

    monkeypatch.setattr(utils, "FACTS_DB", str(tmp_path / "facts.db"))
    monkeypatch.setattr(utils, "TEXT_FTS_DB", str(tmp_path / "text_fts.db"))
    monkeypatch.setattr(cm, "_initialized", False)
    cm._initialized_scopes.clear()
    monkeypatch.delenv("AIDUMEI_CORE_VECTOR_INDEX", raising=False)

    fake = _CosineContractMemory()
    monkeypatch.setattr(runtime, "get_memory", lambda: fake)
    monkeypatch.setattr(hot_search, "get_memory", lambda: fake)
    ensure_core_schema(force=True)
    _init_text_fts()

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    hot_search.register_search_routes(app)
    return cm, fake, TestClient(app)


class TestR11CoreWriteRecallContract:
    CONTENT = "维护者当前主攻确定性抽取与诚实召回两个工作包的收口验收"
    DECOY = "另一位租户在整理园艺笔记与花期日历跟工程毫无关系"

    def _seed(self, cm, fake):
        cm.put_block("core_current_project", self.CONTENT, user_id="r11_user")
        cm.put_block("core_current_project", self.DECOY, user_id="r11_other")
        assert len(fake.points) == 2, "前置失败：向量写入腿没落点，契约无从谈起"

    def test_written_block_comes_back_through_real_search_route(
            self, recall_contract_env):
        cm, fake, client = recall_contract_env
        self._seed(cm, fake)
        body = client.post("/search", json={
            "query": "确定性抽取与诚实召回的收口",
            "user_id": "r11_user"}).json()
        assert body["recall_verdict"] == "found", body.get("verdict_basis")
        texts = [r.get("memory") for r in body["results"]]
        assert self.CONTENT in texts, \
            f"写进去的核心块没被召回 —— 写读契约断裂：{texts}"
        hit = next(r for r in body["results"] if r.get("memory") == self.CONTENT)
        md = hit.get("metadata") or {}
        assert (hit.get("memory_class") or md.get("memory_class")) == "core", \
            "召回结果丢了 memory_class=core 溯源标记"

    def test_scope_filter_is_actually_passed_down(self, recall_contract_env):
        """负向对照的区分力：替身按 must 语义过滤 —— 若 hot/search 忘传
        user_id 过滤器，别人的核心块（园艺内容）就会漏进来，本条变红。"""
        cm, fake, client = recall_contract_env
        self._seed(cm, fake)
        body = client.post("/search", json={
            "query": "园艺笔记与花期日历",
            "user_id": "r11_user"}).json()
        texts = [r.get("memory") for r in body.get("results", [])]
        assert self.DECOY not in texts, "跨租户核心块漏进召回 —— 过滤器没传下去"

    def test_stranger_gets_honest_not_found(self, recall_contract_env):
        cm, fake, client = recall_contract_env
        self._seed(cm, fake)
        body = client.post("/search", json={
            "query": "确定性抽取与诚实召回的收口",
            "user_id": "r11_stranger"}).json()
        texts = [r.get("memory") for r in body.get("results", [])]
        assert self.CONTENT not in texts
        assert body["recall_verdict"] == "not_found", \
            "无数据租户没拿到诚实 not_found（腿全好 + 空结果的三态判定）"
