"""ducky.wal_watermark — WAL 水位探针（v20）

生产实测发现：`facts.db` 主库 3.3MB 而 WAL **4.1MB**、`text_fts.db` 1.0MB / WAL
4.1MB、`salience.db` 0.2MB / WAL 4.0MB —— 三个库的 WAL 都胀到 4MB 上下**长期没有
checkpoint**。

为什么这是个该被看见的指标，而不是「反正不影响正确性」：

· **WAL 不回收 = 崩溃恢复时间线性增长。** 平时无感，真出事那天最慢。
· **主库 mtime 会骗人。** 写入都落在 WAL 里，`facts.db` 的 mtime 停在两天前 ——
  任何按 mtime 判断「最近有没有写入」的运维直觉在这里都是错的（这条已记在生产核查
  清单里已经记过这一条）。
· **它和「数据目录体积」这个产品指标直接挂钩**：36MB 的数据目录里有 12MB 是
  没回收的 WAL。

探针只读文件大小，不打开数据库 —— 观测器不该跟被观测者抢锁。
"""
from __future__ import annotations

import os

#: WAL 相对主库的告警比例。超过主库这么多倍就该 checkpoint 了。
#: 取 1.0（WAL 比主库还大）是保守的：正常写入下 WAL 远小于主库。
RATIO_ALERT = 1.0
#: 绝对下限：小库的比例天然容易超标，低于这个体积不报（避免噪声淹掉真信号）。
MIN_BYTES = 1 << 20   # 1 MB


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
