"""f0.6+ · WP2 黄金集完整性守卫(push_gate 第五关的行为面)。

如实声明(f0.6+ 期):goldenset_v1.json 仍是种子占位语料(200 条 5 类,
expected_ids 为 fr-001 形态的计划 id)——**尚不具备真实判分能力**,此时
把 evaluate() 接进门禁只会产出假绿/假红。本守卫钉「集合本身的健全性」,
真实语料回填发生在 f0.6++ WP7(从生产 dudu 域真实记忆造金标准),回填后
本守卫的 expected_ids 形状断言自动收紧(种子前缀将被真实 memory_id 淘汰)。
"""
from __future__ import annotations

import json
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parent.parent
GOLDEN = _ROOT / "benchmarks" / "goldenset_v1.json"

_CATEGORIES = {"fact_recall", "temporal", "preference", "negation", "multi_hop"}
_PER_CATEGORY = 40


def _load():
    data = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert data.get("schema") == "goldenset.v1", "schema 漂移——判分依据不明,拒绝"
    return data["cases"]


def test_golden_set_shape_complete():
    cases = _load()
    assert len(cases) == 200, f"黄金集应 200 条,实 {len(cases)}"
    cats: dict[str, int] = {}
    for c in cases:
        cats[c["category"]] = cats.get(c["category"], 0) + 1
    # 负向对照基:五类各 40 是 v1 计划;少一类或多出的种类都该红
    assert set(cats) == _CATEGORIES, f"类别漂移: {sorted(cats)}"
    assert all(v == _PER_CATEGORY for v in cats.values()), f"每类 40 破缺: {cats}"


def test_golden_case_fields_are_machine_checkable():
    """每条的 expected_ids 必须是计划 id 或真实 memory_id 形态——判分器只认 id 集合交并。"""
    cases = _load()
    seen_queries: set[str] = set()
    for i, c in enumerate(cases):
        assert c.get("query"), f"case#{i} 空 query"
        q = c["query"].strip()
        assert q not in seen_queries, f"重复 query#{i}: {q[:30]}"
        seen_queries.add(q)
        ids = c.get("expected_ids")
        assert isinstance(ids, list) and ids, f"case#{i} expected_ids 空"
        assert all(isinstance(x, str) and x for x in ids), f"case#{i} expected_ids 非 id 串"
    # 种子期:统计真实 id 占比,给 f0.6++ 回填留进度尺(尚未要求>0)
    seed = sum(1 for c in cases if all(x.count("-") >= 2 and len(x) >= 12 for x in c["expected_ids"]))
    print(f"[goldenset] 真实形态 id 条数: {seed}/200(f0.6++ 回填目标 200)")


def test_golden_retrieve_fn_contract_smoke():
    """判分器契约冒烟:evaluate() 对一部假检索的输出键齐全——
    防 f0.6++ 接驱动层时才发现口径对不上。负向对照:缺 expected_ids
    的 case 会让 evaluate 抛 KeyError(证据:契约真的在被消费)。"""
    from benchmarks.goldenset import evaluate

    cases = _load()[:5]
    # 假检索=「精确照抄每题答案」→ 应满分;这证明 expected_ids 真在判据里
    fake = lambda q, u, b, k: [{"id": i} for c in cases for i in c["expected_ids"]]  # noqa: E731
    out = evaluate(fake, cases=cases)
    assert {"per_category", "overall_hit_rate", "pollution"} <= set(out)
    assert out["overall_hit_rate"] == 1.0, f"照抄答案竟非满分,判据失真: {out}"
    broken = [dict(c) for c in cases[:1]]
    broken[0].pop("expected_ids")
    try:
        evaluate(fake, cases=broken)
        raise AssertionError("缺 expected_ids 竟不报错——判分器契约 空转")
    except KeyError:
        pass
