"""tests/test_v20_wal_watermark.py — v20：WAL 水位要看得见

生产实测：`facts.db` 主库 3.3MB / WAL **4.1MB**、`text_fts.db` 1.0MB / WAL 4.1MB、
`salience.db` 0.2MB / WAL 4.0MB —— 三个库长期没 checkpoint，36MB 的数据目录里
有 12MB 是没回收的 WAL。

它不影响正确性，所以此前没人看见。但两条后果是真的：
① **崩溃恢复时间随 WAL 线性增长** —— 平时无感，真出事那天最慢；
② **主库 mtime 会骗人** —— 写入都落在 WAL 里，`facts.db` 的 mtime 停在两天前，
   任何按 mtime 判断「最近有没有写入」的运维直觉在这里都是错的。

跑法：cd <仓库根> && .venv/bin/pytest tests/test_v20_wal_watermark.py -v
"""
from __future__ import annotations

import ast
import os

from ducky import wal_watermark as W

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _mk(d, name, main_mb, wal_mb):
    open(os.path.join(d, name), "wb").write(b"x" * int(main_mb * (1 << 20)))
    if wal_mb:
        open(os.path.join(d, name + "-wal"), "wb").write(b"y" * int(wal_mb * (1 << 20)))


def test_production_shape_is_no_longer_flagged(tmp_path):
    """★ 语义反转（v20.2.5 · 用户实测 Y-NEW4）：主库 3.3MB / WAL 4.1MB **不再报警**。

    这条用例原先断言的是「必须报警」，理由是 WAL 比主库还大。**用户实机把这个
    判断推翻了**：那个形态下 `PRAGMA wal_checkpoint(PASSIVE)` 返回 (0, N, N) ——
    数据早已落盘，SQLite 只是不主动回收 WAL 占的空间。也就是说旧判据报的不是
    「有问题」，是「SQLite 就这么工作」，于是 /health 上三条告警常年亮着。

    **告警恒真等于没有告警**：用户学会耸肩放过，真出事时反而没人看。
    所以判据改成绝对阈值 —— WAL 真正的代价是崩溃恢复时间，那跟绝对量成正比。
    """
    _mk(tmp_path, "facts.db", 3.3, 4.1)
    s = W.snapshot(str(tmp_path))
    assert s["alerts"] == [], f"生产常态形态不该再告警：{s}"
    assert s["total_wal_bytes"] > 4 * (1 << 20), "体积信息仍要如实上报，只是不构成告警"


def test_large_wal_is_still_flagged(tmp_path):
    """★ 承重对照：提高阈值**不许把监控废掉**。

    改判据最容易犯的错是「为了压噪声顺手把告警关了」。这条钉住另一端：
    WAL 真的涨到 64MB 以上（崩溃恢复时间已经可观）时必须照报。
    """
    _mk(tmp_path, "facts.db", 20, 80)
    s = W.snapshot(str(tmp_path))
    assert s["alerts"] == ["facts.db"], f"大 WAL 必须仍然告警，否则监控形同虚设：{s}"


def test_healthy_wal_is_not_flagged(tmp_path):
    """负向对照：正常写入下 WAL 远小于主库，不许报警 —— 否则告警会被学会忽略。"""
    _mk(tmp_path, "facts.db", 10, 0.5)
    assert W.snapshot(str(tmp_path))["alerts"] == []


def test_small_databases_do_not_generate_noise(tmp_path):
    """★ 小库的比例天然容易超标：绝对下限挡住噪声。

    没有这条，一个 4KB 主库配 8KB WAL 就会报警 —— 而那毫无意义。
    假红灯淹掉真红灯，和没有告警一样坏。
    """
    _mk(tmp_path, "tiny.db", 0.004, 0.008)
    assert W.snapshot(str(tmp_path))["alerts"] == []


def test_zero_wal_reports_zero_not_none(tmp_path):
    """完全没有 WAL 文件时报 0（测过了，是零），不是 None（没测出来）。"""
    _mk(tmp_path, "a.db", 1, 0)
    s = W.snapshot(str(tmp_path))
    assert s["total_wal_bytes"] == 0 and s["alerts"] == []


def test_unreadable_dir_reports_none_not_zero(tmp_path):
    """目录读不到时报 None —— 「没测出来」不许伪装成「一切正常」。"""
    s = W.snapshot(str(tmp_path / "does-not-exist"))
    assert s["total_wal_bytes"] is None


def test_probe_does_not_open_the_databases():
    """★ 探针只读文件大小，不开库 —— 观测器不该跟被观测者抢锁。

    判据落在语法树上：这个模块里不许出现 sqlite3 连接。
    """
    src = open(os.path.join(_ROOT, "ducky/wal_watermark.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    imports |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert "sqlite3" not in imports, (
        "WAL 探针开了数据库连接 —— 它会跟正在写入的服务抢锁，"
        "一个观测器把被观测者拖慢，比没有观测器糟"
    )


def test_health_exposes_and_warns():
    src = open(os.path.join(_ROOT, "ducky/hot/health.py"), encoding="utf-8").read()
    assert 'probes["wal_total_bytes"]' in src and 'probes["wal_alert_dbs"]' in src
    assert 'probes["wal_total_bytes"] = None' in src, "探针挂掉时报 0 会伪装成正常"
    tree = ast.parse(src)
    ok = any(
        isinstance(n, ast.If)
        and any(isinstance(x, ast.Subscript) and isinstance(x.slice, ast.Constant)
                and x.slice.value == "alerts" for x in ast.walk(n.test))
        and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
                and c.func.attr == "append" and isinstance(c.func.value, ast.Name)
                and c.func.value.id == "warnings" for c in ast.walk(n))
        for n in ast.walk(tree)
    )
    assert ok, "/health 暴露了 WAL 水位但超标时不告警"
