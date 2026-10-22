"""f0.6.1 黄金集质量基线（七路外审三路共开的方子：memoh §3.1 / cue §4 / hark 6.5）。

200 条中文对抗查询，5 类各 40 条：事实召回 / 时间推理 / 偏好一致 / 否定撤销 / 多跳关联。
另有 3 个污染指标：矛盾同屏率 / 失效事实泄漏率 / 去重误杀率。

判分只依赖检索层（不依赖答题模型，零 token）：expected memory_ids 命中即得分。
门槛：任一指标相对基线回退 >3% 即红（CI 语义，是否接进 push_gate 属维护者裁决项）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

GOLDEN_PATH = Path(__file__).parent / "goldenset_v1.json"

CATEGORIES = ("fact_recall", "temporal", "preference", "negation", "multi_hop")


def load_golden() -> list[dict]:
    if not GOLDEN_PATH.exists():
        raise FileNotFoundError(f"黄金集缺失: {GOLDEN_PATH}")
    data = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    if data.get("schema") != "goldenset.v1":
        raise ValueError("黄金集 schema 不是 goldenset.v1 —— 拒绝运行（判分依据不明）")
    return data["cases"]


def evaluate(retrieve_fn, cases: list[dict] | None = None) -> dict:
    """retrieve_fn(query, user_id, bank_id, top_k) -> list[dict(id=...)]。

    返回 {per_category: {cat: {"hit_rate": float, "n": int}},
          overall_hit_rate: float,
          pollution: {"contradiction_copresent": float,
                      "expired_leaked": float,
                      "dedup_wrongly_merged": int}}
    """
    cases = cases or load_golden()
    per: dict[str, dict] = {}
    hits_total = 0
    n_total = 0
    for c in cases:
        results = retrieve_fn(c["query"], c.get("user_id", "default"),
                              c.get("bank_id", "default"), c.get("top_k", 5))
        got_ids = {r.get("id") for r in results}
        expected = set(c["expected_ids"])
        hit = len(expected & got_ids) / max(1, len(expected))
        cat = c["category"]
        per.setdefault(cat, {"sum": 0.0, "n": 0})
        per[cat]["sum"] += hit
        per[cat]["n"] += 1
        hits_total += hit
        n_total += 1
    out_per = {k: {"hit_rate": round(v["sum"] / max(1, v["n"]), 4), "n": v["n"]}
               for k, v in per.items()}
    return {
        "per_category": out_per,
        "overall_hit_rate": round(hits_total / max(1, n_total), 4),
        "pollution": _pollution_metrics(cases),
    }


def _pollution_metrics(cases: list[dict]) -> dict:
    """三个污染指标：由 retrieve_fn 的原始结果判定，此处仅定义口径与计数键。

    实际计数需要 retrieve_fn 返回带 metadata 的原始条目，评测驱动层
    （benchmarks/run_golden.py）负责填数。"""
    return {"contradiction_copresent": 0.0, "expired_leaked": 0.0,
            "dedup_wrongly_merged": 0}


def main(argv: list[str]) -> int:
    cases = load_golden()
    cats: dict[str, int] = {}
    for c in cases:
        cats[c["category"]] = cats.get(c["category"], 0) + 1
    print(f"黄金集 v1：{len(cases)} 条")
    for cat in CATEGORIES:
        print(f"  {cat}: {cats.get(cat, 0)}")
    if len(cases) != 200:
        print("⚠️ 数量未达 200 条目标（基线允许渐进补充，发布版必须齐）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
