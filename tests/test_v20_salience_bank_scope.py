"""aiduMEI v20 P0-2 — salience 显著性侧车的记忆库隔离测试

覆盖点（对照权威方案 §3.3 P0-2 证明杠）：
1. salience 表迁移补 (user_id, bank_id) 列，存量行回填 default/default，零丢失、可重入
2. on_memory_added 登记时盖作用域戳；不传 = default 域；非法 bank_id 抛 BankScopeError
3. preserve_heat 路径：不传作用域保留行上原戳（防止回滚把命名库重置回 default），
   显式传入才刷新
4. conflict.detect_conflicts 反义词配对绝不跨库；同库配对为负向对照，
   resolve_conflict_salience 对半衰减
5. 旧库（无作用域列）v19 回退查询仍能检测
6. _canon_uid：改名默认身份（如 dudu）与字面量 'default' 折叠同组，老记忆矛盾不漏检
7. register_salience_for_add 把调用方作用域透传到每一行

注意：ducky.salience 包 __init__ 在 import 时就调 _ensure_db() 建表，
所以必须先钉死 utils.SALIENCE_DB 再 import 任何 ducky.salience 下的模块。
"""
from __future__ import annotations

import os
import tempfile
import time

import pytest

_TMPDIR = tempfile.mkdtemp(prefix="aidumem_v20_saliencescope_")

from ducky import utils

utils.SALIENCE_DB = os.path.join(_TMPDIR, "salience.db")

# —— 钉完路径才允许业务 import（包 __init__ 会立即建表）——
from ducky.bank_contract import BankScopeError  # noqa: E402
from ducky.salience import conflict as conflict_mod  # noqa: E402
from ducky.salience import db as salience_db  # noqa: E402
from ducky.salience.core import on_memory_added  # noqa: E402

# 收集期就把表建到本模块的 tempdir：包 __init__ 的自动建表只在首次 import 时跑，
# 若别的测试模块先 import 过 ducky.salience，上面的 pin 就赶不上那次建表——
# 排在本文件前面的用例会踩到「no such table: salience」。
salience_db._ensure_db()

# v8.3.0 老库形状：有 lane / content_preview，没有作用域列
_V8_SALIENCE_DDL = """
CREATE TABLE salience (
    memory_id TEXT PRIMARY KEY,
    salience REAL NOT NULL DEFAULT 0.5,
    last_access REAL NOT NULL,
    access_count INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    lane TEXT DEFAULT 'general',
    content_preview TEXT DEFAULT ''
)
"""


def _cols():
    conn = utils.get_salience_conn()
    return [r[1] for r in conn.execute("PRAGMA table_info(salience)").fetchall()]


def _row(memory_id: str):
    conn = utils.get_salience_conn()
    return conn.execute(
        "SELECT * FROM salience WHERE memory_id=?", (memory_id,)
    ).fetchone()


@pytest.fixture(autouse=True)
def _fresh_salience(monkeypatch):
    """每条用例都从干净的 v20 表出发；需要老形状的用例自己再 DROP 重建。"""
    monkeypatch.setattr(utils, "SALIENCE_DB", os.path.join(_TMPDIR, "salience.db"))
    conn = utils.get_salience_conn()
    conn.execute("DROP TABLE IF EXISTS salience")
    conn.commit()
    salience_db._ensure_db()
    yield


def test_salience_migration_adds_scope_columns_and_backfills():
    """迁移测试放最前：v8 形状老表 + 存量行 → ensure 后补列、回填 default、可重入。"""
    conn = utils.get_salience_conn()
    conn.execute("DROP TABLE IF EXISTS salience")
    conn.execute(_V8_SALIENCE_DDL)
    now = time.time()
    conn.execute(
        "INSERT INTO salience (memory_id, salience, last_access, access_count, created_at, lane, content_preview) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("legacy-mem", 0.7, now, 3, now, "general", "升级前就在库里的记忆"),
    )
    conn.commit()

    salience_db._ensure_db()

    cols = _cols()
    assert "user_id" in cols and "bank_id" in cols

    row = _row("legacy-mem")
    assert row is not None, "迁移不许弄丢存量行"
    assert (row["user_id"], row["bank_id"]) == ("default", "default")
    assert row["salience"] == pytest.approx(0.7)
    assert row["access_count"] == 3

    # 可重入：再跑一次不报错、不重复、不改数
    salience_db._ensure_db()
    conn = utils.get_salience_conn()
    count = conn.execute("SELECT COUNT(*) FROM salience").fetchone()[0]
    assert count == 1


def test_on_memory_added_stamps_scope_and_defaults():
    on_memory_added("mem-a", content="甲库的一条记忆", lane="general",
                    user_id="user_x", bank_id="bank_a")
    row = _row("mem-a")
    assert (row["user_id"], row["bank_id"]) == ("user_x", "bank_a")

    # 不传作用域 = 字面量 default 域（与存量回填口径一致）
    on_memory_added("mem-plain", content="无作用域写入", lane="general")
    row = _row("mem-plain")
    assert (row["user_id"], row["bank_id"]) == ("default", "default")

    # 非法 bank_id 必须抛 BankScopeError，不许静默落回 default
    with pytest.raises(BankScopeError):
        on_memory_added("mem-bad", content="越权路径", bank_id="../etc")
    assert _row("mem-bad") is None


def test_preserve_heat_keeps_existing_stamp_unless_scope_given():
    on_memory_added("mem-heat", content="初版内容", lane="general",
                    user_id="user_x", bank_id="bank_a")
    conn = utils.get_salience_conn()
    conn.execute("UPDATE salience SET access_count=5 WHERE memory_id=?", ("mem-heat",))
    conn.commit()

    # 不传作用域：保留 bank_a 原戳（self_edit 回滚只有 user_id 可用，
    # 若单传 user 就会把命名库重置回 default——所以约定传任一就两列全盖，
    # 不传就一列都不动）
    on_memory_added("mem-heat", content="回滚后的旧内容", preserve_heat=True)
    row = _row("mem-heat")
    assert (row["user_id"], row["bank_id"]) == ("user_x", "bank_a")
    assert row["access_count"] == 5, "preserve_heat 不许清热度"
    assert row["content_preview"] == "回滚后的旧内容"

    # 显式给作用域：刷新戳，热度依旧保留
    on_memory_added("mem-heat", content="换库后的内容", preserve_heat=True,
                    user_id="user_y", bank_id="bank_b")
    row = _row("mem-heat")
    assert (row["user_id"], row["bank_id"]) == ("user_y", "bank_b")
    assert row["access_count"] == 5


def test_detect_conflicts_never_pairs_across_banks():
    # 跨库反义对：甲库「要」 vs 乙库「不要」——v19 会配对并腰斩两库，v20 必须互不可见
    on_memory_added("a-yao", content="每天要备份数据", lane="general",
                    user_id="user_x", bank_id="bank_a", initial_salience=0.8)
    on_memory_added("b-buyao", content="每天不要备份数据", lane="general",
                    user_id="user_x", bank_id="bank_b", initial_salience=0.8)
    # 同库反义对（负向对照：证明检测器本身活着）
    on_memory_added("a-shua", content="睡前要刷牙", lane="general",
                    user_id="user_x", bank_id="bank_a", initial_salience=0.8)
    on_memory_added("a-bushua", content="睡前不要刷牙", lane="general",
                    user_id="user_x", bank_id="bank_a", initial_salience=0.8)

    conflicts = conflict_mod.detect_conflicts()
    pairs = {frozenset((c["memory_a"], c["memory_b"])) for c in conflicts}
    assert frozenset(("a-yao", "b-buyao")) not in pairs, "跨库反义词绝不许配对"
    assert frozenset(("a-shua", "a-bushua")) in pairs, "同库真矛盾必须检出（负向对照）"
    same_bank = [c for c in conflicts
                 if frozenset((c["memory_a"], c["memory_b"])) == frozenset(("a-shua", "a-bushua"))]
    assert same_bank[0]["bank_id"] == "bank_a"

    # 衰减只落在同库那一对身上，跨库两条毫发无损
    resolved = conflict_mod.resolve_conflict_salience(same_bank)
    assert resolved == 2
    assert _row("a-shua")["salience"] == pytest.approx(0.4)
    assert _row("a-bushua")["salience"] == pytest.approx(0.4)
    assert _row("a-yao")["salience"] == pytest.approx(0.8)
    assert _row("b-buyao")["salience"] == pytest.approx(0.8)


def test_detect_conflicts_v19_fallback_without_scope_columns():
    """老库没有作用域列时退回 v19 查询：全表视作 default/default，检测不失效。"""
    conn = utils.get_salience_conn()
    conn.execute("DROP TABLE IF EXISTS salience")
    conn.execute(_V8_SALIENCE_DDL)
    now = time.time()
    for mid, content in (("old-yao", "周报要写"), ("old-buyao", "周报不要写")):
        conn.execute(
            "INSERT INTO salience (memory_id, salience, last_access, access_count, created_at, lane, content_preview) "
            "VALUES (?, 0.8, ?, 0, ?, 'general', ?)",
            (mid, now, now, content),
        )
    conn.commit()

    conflicts = conflict_mod.detect_conflicts()
    pairs = {frozenset((c["memory_a"], c["memory_b"])) for c in conflicts}
    assert frozenset(("old-yao", "old-buyao")) in pairs
    hit = [c for c in conflicts
           if frozenset((c["memory_a"], c["memory_b"])) == frozenset(("old-yao", "old-buyao"))][0]
    assert (hit["user_id"], hit["bank_id"]) == ("default", "default")


def test_canon_uid_collapses_renamed_default_identity(monkeypatch):
    """部署方把默认身份改名（如 dudu）后，新写入盖 dudu、存量行是 'default'——
    两者必须折叠同组，否则老记忆的矛盾从此漏检（v19.4.2 同源教训）。"""
    monkeypatch.setattr(conflict_mod, "DEFAULT_USER_ID", "dudu")
    conn = utils.get_salience_conn()
    now = time.time()
    rows = [
        ("legacy-default", "周末要加班", "default"),   # 存量行：字面量 default
        ("new-dudu", "周末不要加班", "dudu"),          # 改名后新写入
        ("stranger", "开会不要迟到", "someone_else"),  # 无关用户（负向对照）
        ("legacy-chidao", "开会要迟到", "default"),
    ]
    for mid, content, uid in rows:
        conn.execute(
            "INSERT INTO salience (memory_id, salience, last_access, access_count, created_at, lane, content_preview, user_id, bank_id) "
            "VALUES (?, 0.8, ?, 0, ?, 'general', ?, ?, 'default')",
            (mid, now, now, content, uid),
        )
    conn.commit()

    conflicts = conflict_mod.detect_conflicts()
    pairs = {frozenset((c["memory_a"], c["memory_b"])) for c in conflicts}
    assert frozenset(("legacy-default", "new-dudu")) in pairs, \
        "改名默认身份必须与字面量 default 折叠同组"
    assert frozenset(("stranger", "legacy-chidao")) not in pairs, \
        "非默认身份的其他用户不许折叠进来（负向对照）"


def test_register_salience_for_add_threads_scope():
    from ducky.mem0_runtime import register_salience_for_add

    register_salience_for_add(
        {"results": [
            {"id": "reg-1", "memory": "甲库第一条"},
            {"id": "reg-2", "memory": "甲库第二条"},
        ]},
        user_id="user_x", bank_id="bank_a",
    )
    for mid in ("reg-1", "reg-2"):
        row = _row(mid)
        assert (row["user_id"], row["bank_id"]) == ("user_x", "bank_a")

    # 不传作用域的旧调用形态 = default 域（v19 行为零改动）
    register_salience_for_add({"results": [{"id": "reg-plain", "memory": "默认域"}]})
    row = _row("reg-plain")
    assert (row["user_id"], row["bank_id"]) == ("default", "default")
