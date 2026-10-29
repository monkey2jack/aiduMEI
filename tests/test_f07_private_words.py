"""f0.7：私人名词不进仓库（P4 曾把示例句写成真人昵称，外审导出前才扫出来）。

词表只在本机：未入仓的 benchmarks/.scan_words 或环境变量 AIDUMEI_SCAN_WORDS（| 分隔）。
都没有时空过（公开仓/CI 没有词表是正常的；不用 skip，免得新增跳过轴）。
"""
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _words() -> list[str]:
    env = os.environ.get("AIDUMEI_SCAN_WORDS", "")
    words = [w for w in env.split("|") if w.strip()]
    f = REPO / "benchmarks" / ".scan_words"
    if f.exists():
        words += [w.strip() for w in f.read_text(encoding="utf-8").splitlines() if w.strip() and not w.startswith("#")]
    return words


def test_tracked_files_contain_no_private_words():
    words = _words()
    if not words:
        return
    try:
        files = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        return  # 不在 git 工作区（如解压包）
    hits = []
    for rel in files:
        p = REPO / rel
        if rel.endswith("regression_v3.json") or not p.is_file():
            continue  # 私有仓里真实回归集本来就是私人数据，公开导出时整份替换
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        hits += [f"{rel}: {w}" for w in words if w in text]
    assert not hits, "私人名词进了仓库：\n" + "\n".join(hits[:20])
