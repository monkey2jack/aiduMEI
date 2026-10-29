"""f0.6+++ WP1 · 真实使用回归集 v3 守卫(秒级,不起 app)。

钉三件事:集合本身健全(五类 ≥25、CC 回放 ≥20、无敏感残留);判分器
判据对(假适配器逐类正反例);逐类不降的门真会变红(负向对照)。
全量打分走 push_gate:python -m benchmarks.regression --baseline ...
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from benchmarks import regression as R

DATA = R.load_cases()
CASES = DATA["cases"]


def test_set_shape():
    cats: dict[str, int] = {}
    for c in CASES:
        cats[c["category"]] = cats.get(c["category"], 0) + 1
    assert set(cats) == set(R.CATEGORIES), cats
    assert all(cats[k] >= 25 for k in R.CATEGORIES if k != "cc_replay"), cats
    assert cats["cc_replay"] >= 20 and len(CASES) - cats["cc_replay"] >= 150, cats
    assert len({c["id"] for c in CASES}) == len(CASES)
    for c in CASES:
        assert c["query"].strip(), c["id"]
        if c["category"] in ("fact_recall", "temporal", "update", "cc_replay"):
            assert c.get("expect"), c["id"]
        if c["category"] in ("update", "delete"):
            assert c.get("forbid") and c.get("steps"), c["id"]
        for st in c.get("steps", []):
            assert st["op"] in ("add", "update", "delete", "check"), c["id"]


def test_no_sensitive_residue():
    """脱敏规则(IP/凭据/手机号/内部名)回归:语料不许带出原文标识。

    通用模式写在这里;私有名词表不入仓(f0.7 P2-5),经环境变量 AIDUMEI_SCAN_WORDS('词一|词二')
    或未入仓的 benchmarks/.scan_words(一行一个词)追加。
    """
    import os
    blob = json.dumps(DATA, ensure_ascii=False)
    pat = (r"(?<![\d.])100\.\d+\.\d+\.\d+|prod_owner|"
           r"tskey-|sk-[A-Za-z0-9]{12}|ghp_|(?<!\d)1[3-9]\d{9}(?!\d)|\bo[ucm]_[A-Za-z0-9]{8,}")
    words = os.environ.get("AIDUMEI_SCAN_WORDS", "").split("|")
    local = Path(__file__).resolve().parent.parent / "benchmarks" / ".scan_words"
    if local.exists():
        words += local.read_text(encoding="utf-8").splitlines()
    extra = [re.escape(w.strip()) for w in words if w.strip()]
    if extra:
        pat += "|" + "|".join(extra)
    assert not re.findall(pat, blob)


class _Fake:
    """按「库里有什么就搜得到什么」的理想内核;bad 指定故意坏掉的行为。"""

    def __init__(self, bad: str = ""):
        self.mem: list[str] = []
        self.bad = bad

    def add(self, text, session=""):
        self.mem.append(text)

    def search(self, query, top_k=5, session=""):
        hits = [m for m in self.mem if set(query) & set(m)][:top_k]
        return hits, ("found" if hits and "没有" not in query else "not_found")

    def update(self, old, new):
        self.mem.append(new)
        if self.bad != "update":
            self.mem.remove(old)

    def delete(self, text):
        if self.bad != "delete":
            self.mem.remove(text)


@pytest.mark.parametrize("bad,cat", [("update", "update"), ("delete", "delete")])
def test_judge_catches_broken_kernel(bad, cat):
    c = {"category": cat, "query": "端口", "expect": ["端口是 2"] if cat == "update" else None,
         "forbid": ["端口是 1"],
         "steps": ([{"op": "update", "old": "端口是 1", "text": "端口是 2"}] if cat == "update"
                   else [{"op": "check", "expect": ["端口是 1"]}, {"op": "delete", "text": "端口是 1"}])}
    good, broken = _Fake(), _Fake(bad)
    for f in (good, broken):
        f.add("端口是 1")
    assert R.judge(good, c) is True
    assert R.judge(broken, c) is False


def test_judge_recall_abstain_replay():
    f = _Fake()
    f.add("网关端口是 22012")
    assert R.judge(f, {"category": "fact_recall", "query": "网关端口", "expect": ["网关端口是 22012"]})
    assert not R.judge(f, {"category": "fact_recall", "query": "护照", "expect": ["护照号"]})
    assert R.judge(f, {"category": "abstain", "query": "没有的事"})
    assert R.judge(f, {"category": "cc_replay", "query": "时区", "session": "s2",
                       "steps": [{"op": "add", "text": "时区按上海", "session": "s1"}], "expect": ["时区按上海"]})


def test_gate_turns_red_on_any_category_drop():
    base = {"per_category": {"update": {"n": 2, "pass": 1, "score": 0.5},
                             "delete": {"n": 25, "pass": 25, "score": 1.0}},
            "total": {"n": 27, "pass": 26, "score": 0.963}}   # 基线须自洽(P2-1 自检)
    assert R.regressions({"per_category": {"update": {"score": 0.5}, "delete": {"score": 1.0}}}, base) == []
    assert R.regressions({"per_category": {"update": {"score": 0.6}, "delete": {"score": 0.96}}}, base)
    assert R.regressions({"per_category": {"update": {"score": 0.5}}}, base)   # 类消失也红


def test_committed_baseline_matches_set():
    b = json.loads((R.CASES_PATH.parent / "regression_v3_baseline.json").read_text(encoding="utf-8"))
    assert b["cases_version"] == DATA["version"]
    assert {k: v["n"] for k, v in b["per_category"].items()} == DATA["per_category"]
