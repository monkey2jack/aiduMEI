"""召回闸门（大仓 Issue #5：弱命中条目可凑分填满结果集）。

判据一律**集合相等**，不写「返回了 A」——后者对「B、D 也混进来」毫无区分力，
而「混进来」正是本 issue 的病形（延续 v20.2.5 的 F-03 教训）。

数字一律**现算**：期望分从生产打分函数跑出来，不写死常量。上游那份计划书
就是栽在这里——它标着「实测」的四个数，用的权重不是仓里的权重
（`vector .45/bm25 .15/rel .15/heat .10` vs 真实 `.35/.25/.10/.15`），
于是「0.3 恰好拦住地板分」这个论证整个落空。
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from ducky import scoring                                    # noqa: E402
from ducky.scoring import (                                  # noqa: E402
    DEFAULT_WEIGHTS,
    last_gate_telemetry,
    reset_gate_telemetry,
    score_and_rank_candidates,
)

NEUTRAL = "随便问点别的内容"
FACT_Q = "他的生日是什么时候"        # 命中 is_fact_seeking_query → base *= 1.35


@pytest.fixture(autouse=True)
def _rerank_off(monkeypatch):
    """默认关掉 rerank —— **本文件第一版就栽在没关它上**。

    第一版的四条断分数的用例在**沙箱里绿、部署机上红**：沙箱没有
    `mem0_config_local.json`，重排服务拿不到凭据 → 降级保分；部署机凭据齐全
    → 真融合 `old*(1-W) + rr*W`，同一条候选的分数就变了
    （实测 0.3641 = 0.6065×0.6 + 0×0.4，与 `RERANK_WEIGHT=0.4` 精确吻合）。

    **一个在某些环境里静默变绿的测试，和它测的那个缺陷是同一种病。**
    所以断分数的用例一律先把这条腿摘掉，让判据只由打分公式决定；
    rerank 在场时的行为另有一组用例专门验（见文件末尾 `TestWithRerankAlive`），
    **两种环境都必须验到**，不是二选一。
    """
    def _no_rerank(*a, **kw):
        return []
    monkeypatch.setattr("ducky.mem0_runtime.rerank", _no_rerank, raising=False)


def _mk(mid, *, vec=0.0, bm25=0.0, rel=0.5, access=1, days=0, ignited=False):
    it = {
        "id": mid,
        "memory": f"{mid} 的正文",
        "score": vec,
        "created_at": time.time() - days * 86400,
        "metadata": {"bm25_score": bm25, "reliability": rel,
                     "access_count": access, "memory_type": "FACTS"},
    }
    if ignited:
        it["_ignited"] = True
    return it


def _ids(out):
    return {r.get("id") for r in out}


def _score_of(item, query=NEUTRAL):
    """现跑生产函数取分——期望值不写死。

    取分时**两道闸门都要临时让开**：留着门槛会截掉分数，留着证据闸门会让
    零证据样本直接返回空 —— 那样测出来的 0.0 是「被拦了」，不是「它值这么多分」。
    两件事混为一谈，用例前提就废了（第一版就栽在这儿）。
    """
    prev_thr = scoring.RECALL_MIN_HYBRID
    prev_env = os.environ.get("AIDUMEI_RECALL_EVIDENCE_GATE")
    scoring.RECALL_MIN_HYBRID = 0.0
    os.environ["AIDUMEI_RECALL_EVIDENCE_GATE"] = "0"
    try:
        out = score_and_rank_candidates(query, [dict(item)], limit=5)
        return out[0]["_hybrid_score"] if out else 0.0
    finally:
        scoring.RECALL_MIN_HYBRID = prev_thr
        if prev_env is None:
            os.environ.pop("AIDUMEI_RECALL_EVIDENCE_GATE", None)
        else:
            os.environ["AIDUMEI_RECALL_EVIDENCE_GATE"] = prev_env


# ── 1/2/3：证据闸门（承重） ────────────────────────────────────────────

def test_zero_evidence_candidates_never_reach_the_result_set():
    """判据 1：向量分与 BM25 分双零的条目一条都不许进结果集。"""
    cands = [_mk("zero_fresh"), _mk("zero_old", days=30),
             _mk("real_a", vec=0.8, bm25=0.5), _mk("real_c", vec=0.4, bm25=0.0)]
    out = score_and_rank_candidates(NEUTRAL, [dict(c) for c in cands], limit=10)
    assert _ids(out) == {"real_a", "real_c"}, (
        f"应当只剩有证据的两条，实得 {_ids(out)} —— 零证据条目仍在靠时效/热度凑分"
    )


def test_zero_evidence_survives_neither_heat_nor_fact_boost():
    """判据 2：**零证据 + 高信任 + 高热度 + 事实类查询**（实测可到 0.54）照样出局。

    这条是上游那份计划书最大误判的直接回归：它以为一道 0.3 的总分门槛就够了。
    实测零证据条目的**上限**（叠上 funnel 的 ignition ×1.5）能到 0.81 ——
    **已经越过「真相关」参照的 0.6065**。所以拦的必须是**证据**，不是**分数**。
    """
    fat_garbage = _mk("garbage", rel=1.0, access=100)
    # 先确认它在没有闸门时确实是个高分：不然这条用例在保护一个不存在的风险
    naked = _score_of(fat_garbage, FACT_Q)
    assert naked > 0.5, f"这条垃圾候选本该拿到高分（实测 {naked}），用例前提不成立"

    out = score_and_rank_candidates(FACT_Q, [dict(fat_garbage),
                                             dict(_mk("real", vec=0.8, bm25=0.5))],
                                    limit=10)
    assert _ids(out) == {"real"}, f"零证据高分条目仍然进来了：{_ids(out)}"


def test_all_zero_evidence_returns_empty_not_padding():
    """判据 3：候选全无证据时返回**空列表**，绝不降级填塞。"""
    out = score_and_rank_candidates(NEUTRAL,
                                    [dict(_mk(f"z{i}")) for i in range(5)], limit=5)
    assert out == [], f"应当空手返回，实得 {len(out)} 条 —— 空手回来好过松散注入"
    telem = last_gate_telemetry()
    assert telem.get("evidence_filtered") == 5, (
        f"滤了 5 条却报 {telem.get('evidence_filtered')} —— 拦了多少必须看得见"
    )


def test_evidence_gate_can_be_turned_off(monkeypatch):
    """逃生门：显式关掉闸门则完整回到旧行为。"""
    monkeypatch.setenv("AIDUMEI_RECALL_EVIDENCE_GATE", "0")
    out = score_and_rank_candidates(NEUTRAL, [dict(_mk("zero"))], limit=5)
    assert _ids(out) == {"zero"}, "关掉之后零证据条目应当回来（否则逃生门是假的）"


def test_unparseable_gate_switch_fails_safe(monkeypatch):
    """开关写错时按**开启**处理 —— 「设了个打错的值」不该悄悄关掉安全闸门。"""
    monkeypatch.setenv("AIDUMEI_RECALL_EVIDENCE_GATE", "yes-please")
    out = score_and_rank_candidates(NEUTRAL, [dict(_mk("zero"))], limit=5)
    assert out == [], "开关值非法时闸门必须仍然生效（fail-closed）"


# ── 4/5/6/7：总分门槛 ─────────────────────────────────────────────────

def test_relevant_candidate_keeps_its_measured_score():
    """判据 4：真相关条目正常返回，分数等于**现跑生产公式**算出来的值。"""
    item = _mk("real", vec=0.8, bm25=0.5)
    expected = (DEFAULT_WEIGHTS["vector"] * 0.8 + DEFAULT_WEIGHTS["bm25"] * 0.5
                + DEFAULT_WEIGHTS["time"] * 1.0 + DEFAULT_WEIGHTS["reliability"] * 0.5
                + DEFAULT_WEIGHTS["heat"] * 0.01)
    out = score_and_rank_candidates(NEUTRAL, [dict(item)], limit=5)
    assert _ids(out) == {"real"}
    assert abs(out[0]["_hybrid_score"] - expected) < 0.002, (
        f"实得 {out[0]['_hybrid_score']}，按仓里权重现算应为 {expected:.4f}"
    )


def test_hybrid_threshold_defaults_to_off():
    """判据 6：`RECALL_MIN_HYBRID` 默认 0.0（关闭）。

    这不是偷懒：本机没有足够的查询分布去定生产阈值，拍脑袋常数会把真记忆
    判成「没有」——原话在 `hot/search.py:_verdict_threshold` 的注释里，
    是本仓 v20.1 就下过的裁决。本次先把观测（`score_histogram`）做出来。
    """
    assert scoring.RECALL_MIN_HYBRID == 0.0


def test_threshold_at_zero_point_three_would_kill_legitimate_partial_hits(monkeypatch):
    """判据 7（**负向对照**）：门槛开到 issue 建议的 0.3，会杀掉合法的部分命中。

    三 token 查询命中其中一个 → `bm25 = 1/3`；实测总分约 0.285，落在 0.3 下方。
    这条用例存在的意义就是证明「默认关」不是懒，是这个值真的会误伤 ——
    没有它，「先观测再定值」只是一句自辩。
    """
    partial = _mk("partial", vec=0.0, bm25=1 / 3)
    s = _score_of(partial)
    assert 0.25 < s < 0.3, f"部分命中条目实测 {s}，用例前提（落在 0.3 下方）不成立"

    monkeypatch.setattr(scoring, "RECALL_MIN_HYBRID", 0.3)
    out = score_and_rank_candidates(NEUTRAL, [dict(partial),
                                              dict(_mk("strong", vec=0.8, bm25=0.5))],
                                    limit=5)
    assert _ids(out) == {"strong"}, (
        f"0.3 的门槛下合法部分命中应当被杀（这正是不默认开的理由），实得 {_ids(out)}"
    )
    assert last_gate_telemetry().get("score_filtered") == 1


def test_ignited_items_are_exempt_from_the_hybrid_threshold(monkeypatch):
    """判据 5：ignited 条目不被总分门槛误杀。

    `recall_funnel.py:194` 在本函数返回**之后**才乘 `IGNITION_BOOST = 1.5`，
    门槛在这里看到的是 boost 前的分。一条 boost 后能到 0.33 的条目会在 0.22
    时被杀掉 —— 那是把「显式的相关性信号」当弱命中处理，方向正好反了。
    """
    from ducky.recall_funnel import IGNITION_BOOST

    weak_ignited = _mk("ign", vec=0.05, ignited=True)
    raw = _score_of(weak_ignited)
    assert raw < 0.3 <= raw * IGNITION_BOOST, (
        f"用例前提要求 boost 前 <0.3、boost 后 ≥0.3；实测 {raw} → {raw * IGNITION_BOOST}"
    )
    monkeypatch.setattr(scoring, "RECALL_MIN_HYBRID", 0.3)
    out = score_and_rank_candidates(NEUTRAL, [dict(weak_ignited)], limit=5)
    assert _ids(out) == {"ign"}, "ignited 条目被门槛误杀了"


# ── 8：遥测与两条链路 ─────────────────────────────────────────────────

def test_gate_telemetry_reports_counts_and_histogram():
    """判据 8：过滤条数与分数直方图都能取到 —— 直方图是下一版定阈值的原料。"""
    reset_gate_telemetry()
    assert last_gate_telemetry() == {}
    score_and_rank_candidates(NEUTRAL,
                              [dict(_mk("z1")), dict(_mk("z2")),
                               dict(_mk("r", vec=0.8, bm25=0.5))], limit=5)
    t = last_gate_telemetry()
    assert t["evidence_filtered"] == 2
    assert t["score_filtered"] == 0
    assert t["evidence_gate"] is True
    assert t["threshold"] == scoring.RECALL_MIN_HYBRID
    assert sum(t["score_histogram"].values()) == 1, "直方图只该统计过了闸门的候选"


def test_both_recall_chains_go_through_the_single_gate():
    """判据 8b：闸门放在打分函数这一个出口，**两条调用链都必须经过它**。

    源码级判据：engine 与 recall_funnel 都调 `score_and_rank_candidates`，
    且都没有绕开它自己算分的旁路。哪天有人加了第三条链而不走这个出口，
    这条会红。
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "ducky"
    callers = set()
    for path in root.rglob("*.py"):
        if path.name == "scoring.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "score_and_rank_candidates":
                callers.add(path.name)
    assert callers == {"engine.py", "recall_funnel.py"}, (
        f"调用打分出口的文件集合变了：{callers} —— 新增的链路必须确认也过闸门，"
        "否则闸门的射程小于缺陷的分布（铁律 12）"
    )


def test_the_three_score_gates_stay_distinguishable():
    """本仓有三道分数相关闸门，轴与层位各不相同 —— **不许被当成重复实现合并掉**。

    ① `AIDUMEM_RECALL_SCORE_FLOOR`：向量分轴 · `/search` 响应层
    ② `RECALL_MIN_HYBRID`：复合总分轴 · 打分层
    ③ `CHAIN_MIN_SCORE` / `MIN_SCORE_TO_PROMOTE`：不在召回链上

    上游那份计划书正是因为漏看了 ①，才得出「全仓无分数闸门」。
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "ducky"
    search_src = (root / "hot" / "search.py").read_text(encoding="utf-8")
    scoring_src = (root / "scoring.py").read_text(encoding="utf-8")
    assert "AIDUMEM_RECALL_SCORE_FLOOR" in search_src, "① 不见了，交叉引用失去着力点"
    assert "AIDUMEI_RECALL_MIN_HYBRID" in scoring_src, "② 不见了"
    assert "AIDUMEM_RECALL_SCORE_FLOOR" in scoring_src, (
        "`scoring.py` 里必须留着对 ① 的交叉引用 —— 少了它，下一个人会把两道"
        "不同轴的闸门当重复实现「统一」掉"
    )


# ── 9/10：实机实测逼出来的那一条（证据闸门在活库上几乎不触发）────────────

def test_score_floor_falls_back_to_the_calibrated_verdict_threshold(monkeypatch):
    """**这条是实机实测逼出来的，也是本次真正解决 Issue #5 的那一刀。**

    单元测试里「零证据」写成 `vec=0`，可**活库里没有 vec=0** —— 向量检索对任何
    候选都会给出一个大于零的相似度。实测（生产库三条样本）：

        查询「复盘召回质量」        → 真相关 0.7165 · 无关 0.4062 · 无关 0.3870
        查询「量子色动力学的渐近自由」 → 三条全无关 0.2862 / 0.2819 / 0.2362

    所以证据闸门（双零出局）在生产上**几乎不触发**，它只在向量腿降级时兜底。
    真正的矛盾在别处：部署方已经用 `AIDUMEI_RECALL_VERDICT_THRESHOLD` 声明了
    「低于这个分不可信」（生产配的 0.46，拿真实分布标定），系统也照此把整批
    结果判成 `not_found`，**却仍然原样返回**。「我知道这批不靠谱」与「我照样
    给你」同时成立 —— 这才是 issue 描述的那个病。

    修法是**回落到那个已标定的数**，不是新造一个默认值：一个部署对「多少分
    算可信」只该有一个说法。
    """
    from ducky.hot import search as S

    monkeypatch.delenv("AIDUMEM_RECALL_SCORE_FLOOR", raising=False)
    monkeypatch.setenv("AIDUMEI_RECALL_VERDICT_THRESHOLD", "0.46")
    assert S._score_floor() == 0.46, "未设下限时应当回落到已标定的置信阈值"

    # 显式写 0 是逃生门，必须仍然能完全关掉过滤
    monkeypatch.setenv("AIDUMEM_RECALL_SCORE_FLOOR", "0")
    assert S._score_floor() == 0.0, "显式 0 必须能关掉过滤（逃生门）"

    # 显式值优先于回落
    monkeypatch.setenv("AIDUMEM_RECALL_SCORE_FLOOR", "0.7")
    assert S._score_floor() == 0.7


def test_calibrated_floor_reproduces_the_two_live_queries(monkeypatch):
    """拿实机实测到的**真实分数**重放两个查询，判据用集合相等。

    这条是 Issue #5 的验收：一个查询里只留真相关那条，另一个查询空手返回。
    分数不是我编的，是 2026-08-29 在生产库上跑出来的。
    """
    from ducky.hot import search as S

    monkeypatch.delenv("AIDUMEM_RECALL_SCORE_FLOOR", raising=False)
    monkeypatch.setenv("AIDUMEI_RECALL_VERDICT_THRESHOLD", "0.46")

    q1 = [{"id": "relevant", "score": 0.7165},
          {"id": "pad_a", "score": 0.4062},
          {"id": "pad_b", "score": 0.3870}]
    st = S.annotate_recall_strength(q1)
    assert {r["id"] for r in q1} == {"relevant"}, (
        f"「有一条真相关 + 两条凑数」应当只留真相关那条，实得 {[r['id'] for r in q1]}"
    )
    assert st["dropped"] == 2

    q2 = [{"id": "p1", "score": 0.2862}, {"id": "p2", "score": 0.2819},
          {"id": "p3", "score": 0.2362}]
    S.annotate_recall_strength(q2)
    assert q2 == [], "全是凑数时必须空手返回 —— 空手回来好过松散注入"

    # 负向对照：关掉过滤，两批都原样返回（证明上面两条断言确实由闸门产生）
    monkeypatch.setenv("AIDUMEM_RECALL_SCORE_FLOOR", "0")
    q3 = [{"id": "p1", "score": 0.2862}, {"id": "p2", "score": 0.2819}]
    S.annotate_recall_strength(q3)
    assert {r["id"] for r in q3} == {"p1", "p2"}, "关掉之后不该再过滤（否则逃生门是假的）"


# ══════════════════════════════════════════════════════════════════════════
# rerank 在场时也必须成立 —— **不是二选一**
#
# 上面那组用 autouse fixture 把 rerank 摘掉，判据只由打分公式决定，可复现。
# 但「摘掉重排再验」如果是唯一的验法，那就只是把假绿灯换了个地方藏：
# 生产上重排是**开着**的，融合后的分才是终态分，门槛卡的正是那个数。
#
# 这一组用**可控替身**把 rerank 打开，断言闸门逻辑在融合后依然正确。
# 替身返回可预测的分，所以判据仍然可复现 —— 可复现与「测到真路径」不矛盾。
# ══════════════════════════════════════════════════════════════════════════

class TestWithRerankAlive:

    @staticmethod
    def _fake_rerank(scores):
        """返回固定重排分的替身。

        签名逐字对齐生产调用点 `do_rerank(query, docs, top_n=...)`，
        返回形状对齐 `{"index": i, "relevance_score": s}` —— 替身比生产窄或宽
        都会制造假绿灯（v20.2.5 F-15 的教训）。
        """
        def _rr(query, docs, top_n=None, **kw):
            return [{"index": i, "relevance_score": scores[i % len(scores)]}
                    for i in range(len(docs))]
        return _rr

    def test_fusion_formula_is_what_the_gate_actually_sees(self, monkeypatch):
        """先钉住融合式本身：门槛卡的是 `old*(1-W) + rr*W`，不是打分公式的原值。

        这条是本组的地基 —— 后面两条都建立在「融合后分数会变」这个事实上。
        数字现算，不写死。
        """
        monkeypatch.setattr("ducky.mem0_runtime.rerank",
                            self._fake_rerank([0.0]), raising=False)
        item = _mk("real", vec=0.8, bm25=0.5)
        base = (DEFAULT_WEIGHTS["vector"] * 0.8 + DEFAULT_WEIGHTS["bm25"] * 0.5
                + DEFAULT_WEIGHTS["time"] * 1.0 + DEFAULT_WEIGHTS["reliability"] * 0.5
                + DEFAULT_WEIGHTS["heat"] * 0.01)
        expected = base * (1 - scoring.RERANK_WEIGHT) + 0.0 * scoring.RERANK_WEIGHT

        out = score_and_rank_candidates(NEUTRAL, [dict(item)], limit=5)
        got = out[0]["_hybrid_score"]
        assert abs(got - expected) < 0.002, (
            f"融合后实得 {got}，按 old*(1-W)+rr*W 现算应为 {expected:.4f} "
            f"（W={scoring.RERANK_WEIGHT}）—— 融合式变了，下面两条的前提就没了"
        )
        assert "_rerank_score" in out[0], "重排真跑过就该留下 _rerank_score 这个凭证"

    def test_evidence_gate_still_fires_before_rerank(self, monkeypatch):
        """证据闸门在 rerank **之前** —— 零证据条目根本不该被送去重排。

        判据用**替身收到的文档数**，不是结果集：后者对「送去重排了但最后被
        排掉」没有区分力，而「省 token」正是把闸门放在重排前的理由之一。
        """
        seen = {}

        def _rr(query, docs, top_n=None, **kw):
            seen["docs"] = list(docs)
            return [{"index": i, "relevance_score": 0.9} for i in range(len(docs))]

        monkeypatch.setattr("ducky.mem0_runtime.rerank", _rr, raising=False)
        cands = [_mk("zero"), _mk("zero2"), _mk("real", vec=0.8, bm25=0.5)]
        out = score_and_rank_candidates(NEUTRAL, [dict(c) for c in cands], limit=10)

        assert _ids(out) == {"real"}, f"零证据条目混进结果集了：{_ids(out)}"
        assert len(seen.get("docs", [])) == 1, (
            f"送去重排的文档有 {len(seen.get('docs', []))} 份，应当只有 1 份 —— "
            "零证据候选被白送去重排了，闸门位置不对（也白烧 token）"
        )

    def test_threshold_is_applied_to_the_fused_score_not_the_raw_one(self, monkeypatch):
        """门槛必须卡在**融合后**：同一条候选，融合前过线、融合后不过线 → 应当被拦。

        这条正是「沙箱绿部署机红」那个缺陷的正面形态：如果门槛卡在融合前，
        它在有重排的环境里就形同虚设。
        """
        item = _mk("borderline", vec=0.8, bm25=0.5)
        raw = _score_of(item)                       # 无重排时的原值
        fused = raw * (1 - scoring.RERANK_WEIGHT)   # 重排给 0 分时的融合值
        thr = (raw + fused) / 2                     # 卡在两者之间：现算，不写死
        assert fused < thr < raw, (
            f"用例前提要求 融合后 {fused:.4f} < 门槛 {thr:.4f} < 融合前 {raw:.4f}"
        )

        monkeypatch.setattr("ducky.mem0_runtime.rerank",
                            self._fake_rerank([0.0]), raising=False)
        monkeypatch.setattr(scoring, "RECALL_MIN_HYBRID", thr)
        out = score_and_rank_candidates(NEUTRAL, [dict(item)], limit=5)
        assert out == [], (
            "融合后已低于门槛却仍被放行 —— 门槛卡在了融合之前，"
            "在开着重排的生产环境里等于没有"
        )

        # 负向对照：把重排分给高，融合后重新过线，必须回来
        monkeypatch.setattr("ducky.mem0_runtime.rerank",
                            self._fake_rerank([1.0]), raising=False)
        out2 = score_and_rank_candidates(NEUTRAL, [dict(item)], limit=5)
        assert _ids(out2) == {"borderline"}, (
            "重排给了高分、融合后过线，却仍被拦 —— 那就不是门槛，是黑名单"
        )


def test_every_score_asserting_test_declares_its_rerank_state():
    """**元守卫**：凡是拿 `score_and_rank_candidates` 的输出做断言的用例，
    必须显式声明 rerank 状态（摘掉它，或注入可控替身）。

    这条守卫的由来：本文件第一版四条用例在**沙箱绿、部署机红** ——
    唯一的变量是重排服务可不可达。一个测试的结论取决于外部服务今天在不在，
    它就不是判据，是天气预报。

    **判据是「有没有声明」，不是「跑起来红不红」** —— 后者只能在恰好没有
    凭据的机器上发现问题，那正是当初漏掉它的原因。声明的方式有两种：
    模块级 autouse fixture，或用例/类内 monkeypatch `mem0_runtime.rerank`。

    豁免：只调用**纯函数**（如 `normalize_score`、`calc_bm25_score`）而不碰
    打分主链的文件不在射程内 —— 它们的输出与重排无关。
    """
    import ast
    import pathlib

    tests_dir = pathlib.Path(__file__).resolve().parent
    offenders = {}
    for path in sorted(tests_dir.glob("test_*.py")):
        src = path.read_text(encoding="utf-8")
        if "score_and_rank_candidates(" not in src:
            continue
        tree = ast.parse(src, filename=str(path))
        # 该文件里有没有「声明过 rerank 状态」的痕迹（模块级 fixture 或任意位点的 patch）
        declares = ("mem0_runtime.rerank" in src) or ("ducky.mem0_runtime.rerank" in src)
        if declares:
            continue
        # 逐个用例看：真的拿打分输出做了断言吗？只是调用不算
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not fn.name.startswith("test_"):
                continue
            body = ast.unparse(fn)
            if "score_and_rank_candidates(" not in body:
                continue
            if "_hybrid_score" in body or "_rerank_score" in body:
                offenders.setdefault(path.name, []).append(fn.name)
    assert not offenders, (
        f"这些用例拿打分输出做断言却没声明 rerank 状态：{offenders} —— "
        "它们会在重排可达的机器上给出与沙箱不同的结论（沙箱绿、部署机红），"
        "而那正是本文件第一版栽过的跟头。请加 autouse fixture 摘掉重排，"
        "或注入可控替身。"
    )
