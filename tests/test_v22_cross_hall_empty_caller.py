"""v22.0 雷霆审计 A3 · 众神殿空 caller 收紧守卫

背景：`authorize_cross_hall` 此前「caller 为空 → 放行」，意味着任何持共享
token 的参与者只要**省略** caller_user_id 就能读任意殿——v21.1「跨殿默认
隔离」被一个缺省参数拆掉（Qwen F-01 攻击链的最后一公里）。

v22.0 收紧：空 caller 仅对「主人直连」放行——
- UI 会话（session cookie）→ 放行
- 回环无凭据部署（未经鉴权中间件）→ 放行
- API token（bearer）→ 拒绝（agent 必须声明自己是哪座殿）

负向对照：把 bearer 分支删掉（改回空 caller 一律放行）必须让本文件变红。
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

_tmp = tempfile.mkdtemp(prefix="aidumem_v22_hall_")
_DB = os.path.join(_tmp, "facts.db")

import ducky.utils as utils  # noqa: E402
utils.FACTS_DB = _DB

from ducky.pantheon import authorize_cross_hall, HallError  # noqa: E402
from ducky.security.auth import (  # noqa: E402
    clear_request_auth_kind,
    set_request_auth_kind,
)


@pytest.fixture(autouse=True)
def _db():
    utils.FACTS_DB = _DB
    c = sqlite3.connect(_DB)
    c.executescript(
        "DROP TABLE IF EXISTS pantheon_halls; DROP TABLE IF EXISTS hall_grants; "
        "DROP TABLE IF EXISTS facts; PRAGMA user_version=0; "
        "CREATE TABLE facts(id INTEGER PRIMARY KEY, user_id TEXT, bank_id TEXT);")
    from ducky.schema_bootstrap import apply_migrations
    apply_migrations(c)
    c.commit()
    c.close()
    yield


@pytest.fixture(autouse=True)
def _reset_auth_kind():
    yield
    clear_request_auth_kind()


def test_bearer_empty_caller_denied():
    """A3 核心：API token 调用方空 caller 必须被拒。"""
    set_request_auth_kind("bearer")
    with pytest.raises(HallError):
        authorize_cross_hall("victim_hall", "", action="read")


def test_session_empty_caller_allowed():
    """主人直连（UI 会话）空 caller 放行——①定位语义保留。"""
    set_request_auth_kind("session")
    assert authorize_cross_hall("any_hall", "", action="read") is True


def test_loopback_no_auth_empty_caller_allowed():
    """回环无凭据部署（未经鉴权中间件，kind 为空）空 caller 放行。"""
    clear_request_auth_kind()
    assert authorize_cross_hall("any_hall", "", action="read") is True


def test_caller_equals_target_always_allowed():
    """读自己殿：caller==target 恒放行，与凭据类型无关。"""
    set_request_auth_kind("bearer")
    assert authorize_cross_hall("hall_a", "hall_a", action="read") is True


def test_bearer_with_caller_still_needs_grant():
    """bearer 带了 caller 但无借阅 → 仍拒（收紧没放松既有跨殿门槛）。"""
    set_request_auth_kind("bearer")
    with pytest.raises(HallError):
        authorize_cross_hall("victim_hall", "attacker_hall", action="read")


def test_negative_control_bearer_branch_removed_would_pass():
    """负向对照：证明 bearer 分支真有区分力。

    模拟「改回空 caller 一律放行」的旧行为——若那样，bearer 空 caller 会
    返回 True 而非抛错。本用例断言当前实现**不是**旧行为。
    """
    set_request_auth_kind("bearer")
    denied = False
    try:
        authorize_cross_hall("victim_hall", "", action="read")
    except HallError:
        denied = True
    assert denied, "A3 复发：bearer 空 caller 又放行了"
