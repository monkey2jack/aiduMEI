"""tests/test_v20_verify_data.py — v20 P0-8：数据在不在，让清单自己回答

用户视角审计五实测（生产机）：`data_manifest.json` 全盘 0 个、LoCoMo 数据文件全盘
0 个、`bench_data` 目录不存在 —— 而报告写着「LoCoMo 已下载 + 已锁哈希」。
审计原话：「要么文件在别的路径，要么报告的数字是拍的不是量的。」

事后逐台核对：**两边都对，测的是两棵树。** 数据在开发机上确实存在且哈希相符
（`locomo10.json` 2.8 MB / `longmemeval_s.json` 278 MB），生产机上确实没有。
所以真正的缺陷不是「谁说谎」，是**「数据在不在」这个问题没有机器可回答的形式** ——
清单只记 `filename`，落地目录只活在 `download.py` 的代码里，只能靠人上机器 `ls`，
而人会记错路径、会看错机器。

整改就是把这个问题变成一条命令：`python -m benchmarks.verify_data`。

跑法：cd <仓库根> && .venv/bin/pytest tests/test_v20_verify_data.py -v
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, str(_REPO_ROOT))

from benchmarks import verify_data as VD  # noqa: E402


def _manifest(files: dict, env_var="AIDUMEI_BENCH_DATA_DIR", default="~/nowhere"):
    m = {"_base_dir": {"env_var": env_var, "default": default}}
    m.update(files)
    return m


# ═══════════════ ① 清单必须带落地目录声明 ═══════════════

def test_the_real_manifest_declares_a_base_dir():
    """★ P0-8 的缺陷本身：清单里没有落地目录声明。"""
    m = VD.load_manifest()
    assert "_base_dir" in m, "清单没有 _base_dir —— 「数据在不在」又变回一个只能靠人回答的问题"
    decl = m["_base_dir"]
    assert decl.get("env_var"), "没声明环境变量名"
    assert decl.get("default"), "没声明默认目录"


def test_manifest_without_base_dir_is_refused(tmp_path):
    """缺声明时**拒绝运行**（退出码 2），不许猜一个目录然后报「数据不在」。"""
    p = tmp_path / "m.json"
    p.write_text(json.dumps({"locomo": {"filename": "x.json"}}), encoding="utf-8")
    with pytest.raises(VD.ManifestError, match="_base_dir"):
        VD.load_manifest(str(p))


# ═══════════════ ② 铁律 14：显式指定但无效 → 报错点名，绝不回退 ═══════════════

def test_explicit_but_broken_dir_raises_instead_of_falling_back(tmp_path):
    """★ 这条是本文件最要紧的一条。

    部署方设了 `AIDUMEI_BENCH_DATA_DIR` 却指向一个不存在的目录 —— 那是**配置错误**。
    悄悄回退默认目录、然后报告「数据不在」，会让「配置写错了」伪装成「数据没下载」，
    把人引向完全错误的排查方向。所以：抛错，并点名那个坏路径。
    """
    m = _manifest({}, default=str(tmp_path))
    bad = str(tmp_path / "definitely-not-here")
    with pytest.raises(VD.ManifestError) as ei:
        VD.resolve_base_dir(m, env={"AIDUMEI_BENCH_DATA_DIR": bad})
    assert bad in str(ei.value), f"报错里没点名那个坏路径：{ei.value}"
    assert "不回退" in str(ei.value), "报错没说明「为什么不回退」，下一个人会以为是 bug"


def test_valid_explicit_dir_wins_over_default(tmp_path):
    real = tmp_path / "real"; real.mkdir()
    m = _manifest({}, default=str(tmp_path / "other"))
    path, source = VD.resolve_base_dir(m, env={"AIDUMEI_BENCH_DATA_DIR": str(real)})
    assert path == str(real) and source == "env"


def test_unset_env_falls_back_to_the_declared_default(tmp_path):
    m = _manifest({}, default=str(tmp_path))
    path, source = VD.resolve_base_dir(m, env={})
    assert path == str(tmp_path) and source == "default"


# ═══════════════ ③ 缺文件要点名，不许只给一个总数 ═══════════════

def test_missing_file_is_named_not_just_counted(tmp_path):
    """★ 「缺 1 个」对运维没有可行动性；「缺 <绝对路径>」才有。"""
    m = _manifest({"locomo": {"filename": "locomo10.json", "sha256": "0" * 64}},
                  default=str(tmp_path))
    rep = VD.verify(m, env={})
    assert rep["all_present"] is False
    assert len(rep["missing"]) == 1
    row = rep["missing"][0]
    assert row["dataset"] == "locomo"
    assert row["path"].endswith("locomo10.json"), f"没给出完整落地路径：{row}"


def test_hash_mismatch_is_distinguished_from_missing(tmp_path):
    """★ 「文件不在」和「文件在但内容不对」是两件事，必须分开报。

    合成一条「数据有问题」，运维会先去下载 —— 而文件其实已经在那儿了，
    真正的问题是它被换过／截断过。
    """
    f = tmp_path / "locomo10.json"
    f.write_text("这不是真的数据集", encoding="utf-8")
    m = _manifest({"locomo": {"filename": "locomo10.json", "sha256": "a" * 64}},
                  default=str(tmp_path))
    rep = VD.verify(m, env={})
    assert not rep["missing"], "文件明明在，却被报成缺失"
    assert len(rep["mismatched"]) == 1
    row = rep["mismatched"][0]
    assert row["expected_sha256"] == "a" * 64
    assert row["actual_sha256"] == hashlib.sha256(f.read_bytes()).hexdigest()


def test_present_and_matching_file_reports_ok(tmp_path):
    body = b'{"ok": true}'
    f = tmp_path / "d.json"; f.write_bytes(body)
    m = _manifest({"ds": {"filename": "d.json",
                          "sha256": hashlib.sha256(body).hexdigest()}},
                  default=str(tmp_path))
    rep = VD.verify(m, env={})
    assert rep["all_present"] is True
    assert rep["ok"][0]["status"] == "ok"


def test_underscore_keys_are_not_treated_as_datasets(tmp_path):
    """`_base_dir` 这类元数据键不许被当成数据集去找文件。"""
    m = _manifest({}, default=str(tmp_path))
    rep = VD.verify(m, env={})
    assert rep["datasets"] == {}, f"元数据键被当成数据集了：{rep['datasets']}"
    assert rep["all_present"] is True


# ═══════════════ ④ 命令行退出码 ═══════════════

def test_cli_exit_codes_distinguish_the_three_outcomes(tmp_path, monkeypatch):
    """0 = 全就位 / 1 = 有缺失 / 2 = 拒绝运行。三种结局必须用不同退出码。

    合成一个退出码，调用方（CI、跑分闸门）就无法区分「数据没下载」和
    「清单本身坏了」—— 而这两种情况该做的事完全不同。
    """
    # ② 有缺失 → 1
    mp = tmp_path / "m.json"
    mp.write_text(json.dumps(_manifest(
        {"locomo": {"filename": "nope.json", "sha256": "0" * 64}},
        default=str(tmp_path))), encoding="utf-8")
    r = subprocess.run([sys.executable, "-c",
                        f"import sys; sys.path.insert(0, {str(_REPO_ROOT)!r});"
                        f"from benchmarks import verify_data as V;"
                        f"V.MANIFEST_PATH = {str(mp)!r};"
                        f"sys.exit(V.main([]))"],
                       capture_output=True, text=True, env={**os.environ,
                                                            "AIDUMEI_BENCH_DATA_DIR": ""})
    assert r.returncode == 1, f"有缺失却给了退出码 {r.returncode}：{r.stdout}{r.stderr}"
    assert "nope.json" in r.stdout, f"没点名缺的那个文件：{r.stdout}"

    # ③ 清单坏 → 2
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    r2 = subprocess.run([sys.executable, "-c",
                         f"import sys; sys.path.insert(0, {str(_REPO_ROOT)!r});"
                         f"from benchmarks import verify_data as V;"
                         f"V.MANIFEST_PATH = {str(bad)!r};"
                         f"sys.exit(V.main([]))"],
                        capture_output=True, text=True)
    assert r2.returncode == 2, f"清单坏却给了退出码 {r2.returncode}"
    assert "拒绝运行" in r2.stderr
