"""ducky.wal_watermark — WAL 水位探针（v20）

生产实测发现：`facts.db` 主库 3.3MB 而 WAL **4.1MB**、`text_fts.db` 1.0MB / WAL
4.1MB、`salience.db` 0.2MB / WAL 4.0MB —— 三个库的 WAL 都胀到 4MB 上下**长期没有
checkpoint**。

为什么这是个该被看见的指标，而不是「反正不影响正确性」：

· **WAL 不回收 = 崩溃恢复时间线性增长。** 平时无感，真出事那天最慢。
· **主库 mtime 会骗人。** 写入都落在 WAL 里，`facts.db` 的 mtime 停在两天前 ——
  任何按 mtime 判断「最近有没有写入」的运维直觉在这里都是错的（这条已记在生产核查
  清单里）。
· **它和「数据目录体积」这个产品指标直接挂钩**：36MB 的数据目录里有 12MB 是
  没回收的 WAL。

探针只读文件大小，不打开数据库 —— 观测器不该跟被观测者抢锁。
"""
from __future__ import annotations

import os

#: WAL 相对主库的告警比例。超过主库这么多倍就该 checkpoint 了。
#: 取 1.0（WAL 比主库还大）是保守的：正常写入下 WAL 远小于主库。
RATIO_ALERT = 1.0
#: 绝对下限。
#:
#: v20.2.5（用户实测 Y-NEW4）：从 1 MB 提到 64 MB。用户实机实测 —— 三个库的 WAL
#: 各约 4 MB、主库同量级，比例判据于是**恒真**，`/health` 常年亮着三条告警。
#: 而 `PRAGMA wal_checkpoint(PASSIVE)` 返回 (0, N, N)：**数据早已落盘**，
#: SQLite 只是不主动回收 WAL 文件占用的空间。
#:
#: 也就是说旧判据报的不是「有问题」，是「SQLite 就这么工作」。**告警恒真
#: 等于没有告警** —— 用户学会耸肩放过，真出事时反而没人看。
#:
#: 为什么不改成「checkpoint 失败才报」：那要打开数据库，而本模块的设计前提
#: 是「观测器不该跟被观测者抢锁」（见模块 docstring）。绝对阈值能在不开库的
#: 前提下把噪声压掉 —— WAL 真正的代价是崩溃恢复时间，那跟**绝对量**成正比，
#: 跟它是不是比主库大没多大关系。
MIN_BYTES = 64 << 20   # 64 MB


def snapshot(data_dir: str | None = None) -> dict:
    """各库的 WAL 水位。返回 `{"total_wal_bytes", "alerts": [...], "dbs": {...}}`。"""
    if data_dir is None:
        from ducky.utils import DATA_DIR
        data_dir = DATA_DIR
    dbs, alerts, total = {}, [], 0
    try:
        names = sorted(n for n in os.listdir(data_dir) if n.endswith(".db"))
    except OSError:
        return {"total_wal_bytes": None, "alerts": [], "dbs": {}}
    for n in names:
        main = os.path.join(data_dir, n)
        wal = main + "-wal"
        try:
            wsz = os.path.getsize(wal) if os.path.exists(wal) else 0
            msz = os.path.getsize(main)
        except OSError:
            continue
        total += wsz
        dbs[n] = {"main_bytes": msz, "wal_bytes": wsz}
        if wsz >= MIN_BYTES and (msz == 0 or wsz / msz >= RATIO_ALERT):
            alerts.append(n)
    return {"total_wal_bytes": total, "alerts": alerts, "dbs": dbs}
