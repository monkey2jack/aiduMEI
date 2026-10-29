"""native 内核升级前只读体检(f0.7 P2-3,替换 pre-upgrade-check 里引用的三个已不存在的脚本)。

只读打开 memory.db(mode=ro),不建索引、不补列、不改任何行。输出一行 JSON;有红项退出码 1。
  - integrity_check
  - facts 同键重复组数(新内核启动时会合并进墓碑——提前告知,不算红)
  - 跨租户同 id 条数(新内核按 id 不带租户时会拒绝——提前告知)
  - 缺向量 / 待补嵌入条数
用法:python scripts/native_preflight.py [--data-dir DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys


def preflight(data_dir: str) -> dict:
    path = os.path.join(data_dir, "memory.db")
    if not os.path.exists(path):
        return {"memory_db": "absent", "red": []}
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        q = lambda s: c.execute(s).fetchall()  # noqa: E731
        tables = {r[0] for r in q("SELECT name FROM sqlite_master WHERE type='table'")}
        out = {"memory_db": path, "integrity": q("PRAGMA integrity_check")[0][0],
               "memories": q("SELECT COUNT(*) FROM memories")[0][0] if "memories" in tables else None}
        if "facts" in tables:
            cols = {r[1] for r in q("PRAGMA table_info(facts)")}
            key = ", ".join(k for k in ("agent_id", "user_id", "bank_id", "category", "fact_key") if k in cols)
            out["fact_key_dup_groups"] = q(f"SELECT COUNT(*) FROM (SELECT 1 FROM facts GROUP BY {key}"
                                           " HAVING COUNT(*) > 1)")[0][0]
        if "memories" in tables:
            out["ids_in_multiple_tenants"] = q("SELECT COUNT(*) FROM (SELECT id FROM memories GROUP BY id"
                                               " HAVING COUNT(*) > 1)")[0][0]
            if "memory_vectors" in tables:
                out["missing_vectors"] = q("SELECT COUNT(*) FROM memories m LEFT JOIN memory_vectors v"
                                           " ON v.mid=m.mid WHERE v.mid IS NULL")[0][0]
        out["red"] = [] if out["integrity"] == "ok" else ["integrity"]
        return out
    finally:
        c.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data-dir", default=os.environ.get("AIDUMEM_DATA_DIR") or "data")
    rep = preflight(ap.parse_args(argv).data_dir)
    print(json.dumps(rep, ensure_ascii=False))
    return 1 if rep["red"] else 0


if __name__ == "__main__":
    sys.exit(main())
