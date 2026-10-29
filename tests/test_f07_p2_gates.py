"""f0.7 P2 门禁/工具回归(外审复现用例)。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def test_p2_1_fragment_recall_no_longer_counts_as_hit():
    """外审 muse P1-08:召回只返回期望里的一个短片段,不算命中(此前双向子串判绿)。"""
    from benchmarks.regression import _hit, _touches
    assert _hit(["aiduMEI 的生产端口是 8767,在 node-a 上"], ["生产端口是 8767"])
    assert not _hit(["端口"], ["aiduMEI 的生产端口是 8767"])
    assert _touches(["端口"], ["aiduMEI 的生产端口是 8767"])          # 禁止类仍按双向,宁严勿松


@pytest.mark.parametrize("name", ["regression_v3_baseline.json", "regression_v3_baseline_native.json"])
def test_p2_1_committed_baselines_are_self_consistent(name):
    from benchmarks.regression import check_baseline
    assert check_baseline(json.loads((ROOT / "benchmarks" / name).read_text(encoding="utf-8"))) == []


def test_p2_1_tampered_baseline_is_rejected():
    """外审 muse P2-01:手改 score 而 pass/n 不变的基线不许当尺子。"""
    from benchmarks.regression import regressions
    base = json.loads((ROOT / "benchmarks" / "regression_v3_baseline_native.json").read_text(encoding="utf-8"))
    base["per_category"]["temporal"]["score"] = 0.0
    assert any("基线不自洽" in x for x in regressions({"per_category": base["per_category"]}, base))


def test_p2_1_push_gate_runs_native_regression():
    gate = (ROOT / "scripts" / "push_gate.sh").read_text(encoding="utf-8")
    assert "AIDUMEI_MEMORY_KERNEL=native" in gate and "regression_v3_baseline_native.json" in gate


def test_p2_3_resync_rejects_naive_since(tmp_path):
    from scripts.native_to_mem0_resync import resync
    with pytest.raises(SystemExit, match="时区"):
        resync(str(tmp_path), "2026-10-27T17:17:00")


def test_p2_3_native_preflight_is_read_only(tmp_path):
    from ducky.memory_kernel.impl_native import NativeMemory
    from scripts.native_preflight import preflight
    m = NativeMemory(path=str(tmp_path / "memory.db"), embedder=None)
    m.add("一条记忆", user_id="u")
    m.conn.close()
    db = tmp_path / "memory.db"
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    rep = preflight(str(tmp_path))
    assert rep["integrity"] == "ok" and rep["memories"] == 1 and rep["red"] == []
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_p2_3_pre_upgrade_check_no_longer_references_missing_scripts():
    sh = (ROOT / "scripts" / "pre-upgrade-check.sh").read_text(encoding="utf-8")
    for gone in ("dedup_facts.py", "decay_scanner.py", "recompute_trust.py"):
        assert f'dry_run "{gone}"' not in sh
    assert "native_preflight.py" in sh
