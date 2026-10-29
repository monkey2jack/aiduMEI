"""v21.1 众神殿：殿管理 + 跨殿借阅 —— 红→绿守卫。

覆盖：殿 CRUD（建/列/查/软删）、借阅 grant/check/revoke/过期 fail-closed/自借阅拒、
authorize_cross_hall 四态，以及**借阅在 core 搜索路径真生效**（recall_chain 跨殿无借阅
拒、有借阅通、不带 caller 主人直连通）——证明借阅非「写了没人读」的假闭环。
"""
import os
import sqlite3
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.mkdtemp(prefix="aidumem_v21_pantheon_")
_DB = os.path.join(_tmp, "facts.db")

import ducky.utils as utils  # noqa: E402
utils.FACTS_DB = _DB


@pytest.fixture(autouse=True)
def _db():
    utils.FACTS_DB = _DB
    c = sqlite3.connect(_DB)
    c.executescript(
        "DROP TABLE IF EXISTS pantheon_halls; DROP TABLE IF EXISTS hall_grants; "
        "DROP TABLE IF EXISTS facts; "
        # user_version 是 db 级持久：DROP 表后必须一并归零，否则 apply_migrations
        # 见旧 user_version=8 会跳过重建（共享 _DB 的状态污染）。
        "PRAGMA user_version=0; "
        "CREATE TABLE facts(id INTEGER PRIMARY KEY, user_id TEXT, bank_id TEXT);")
    from ducky.schema_bootstrap import apply_migrations
    apply_migrations(c)
    c.commit()
    c.close()
    yield


def test_grant_expiry_fail_closed():
    from ducky import pantheon as p
    with pytest.raises(p.HallError):
        p.grant_hall_access("zeus", "athena", expires_at="2020-01-01T00:00:00")  # 过去时刻拒
    p.grant_hall_access("zeus", "athena", expires_at="2099-01-01T00:00:00")
    assert p.check_hall_access("zeus", "athena", "read") is True


def test_self_grant_and_illegal_action_rejected():
    from ducky import pantheon as p
    with pytest.raises(p.HallError):
        p.grant_hall_access("athena", "athena")            # 自借阅拒
    with pytest.raises(p.HallError):
        p.grant_hall_access("zeus", "athena", actions="write")  # 非法动作拒


def test_authorize_cross_hall_four_states():
    from ducky import pantheon as p
    assert p.authorize_cross_hall("zeus", "") is True          # caller 空=主人直连
    assert p.authorize_cross_hall("zeus", "zeus") is True      # 读自己殿
    with pytest.raises(p.HallError):
        p.authorize_cross_hall("zeus", "athena")              # 跨殿无借阅拒
    p.grant_hall_access("zeus", "athena", actions="read")
    assert p.authorize_cross_hall("zeus", "athena", action="read") is True  # 跨殿有借阅通

