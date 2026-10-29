"""f0.6+++ rankfix 守卫:native 下长原话不许压过蒸馏事实(inject limit 3)。

子进程跑 benchmarks.rank_raw_vs_fact(进程内起 app 要独占数据目录,不能和本进程的
ducky 导入混用);同一语料关掉降权(AIDUMEI_RAW_TURN_WEIGHT=1)必须变红 —— 负向对照。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run(weight: str) -> dict:
    env = {**os.environ, "AIDUMEI_MEMORY_KERNEL": "native", "AIDUMEI_RAW_TURN_WEIGHT": weight}
    for k in ("AIDUMEM_DATA_DIR", "AIDUMEM_CONFIG_FILE"):
        env.pop(k, None)
    p = subprocess.run([sys.executable, "-m", "benchmarks.rank_raw_vs_fact"], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=300)
    return json.loads(p.stdout.strip().splitlines()[-1])


def test_distilled_facts_lead_under_native():
    res = _run("0.6")
    assert res["ok"], res
    assert res["pass"] == res["n"] == 10


def test_without_raw_weight_goes_red():
    assert _run("1.0")["ok"] is False


def test_is_raw_turn_markers():
    from ducky.scoring import is_raw_turn
    assert is_raw_turn({"id": "verbatim:12", "metadata": {}})
    assert is_raw_turn({"id": "x", "metadata": {"lane": "raw"}})
    assert is_raw_turn({"id": "x", "metadata": {"memory_type": "VERBATIM"}})
    assert not is_raw_turn({"id": "x", "metadata": {"lane": "distill"}})
